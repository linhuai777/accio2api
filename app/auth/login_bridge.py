"""网页登录桥（OAuth 授权流形态）—— 用户浏览器整页跳转官方登录，凭证自动入库。

## 为什么要有这个桥

`web_login.py` 的「服务器浏览器」方案：服务器起 headless 浏览器截图推流，
管理员远程操控。三个硬伤——
  1. 手机端管理员没鼠标，滑块基本拖不过去；
  2. 服务器出口 IP 一挂就被 Baxia 拦，截图流再流畅也白搭；
  3. 转发延迟 + 截图压缩，体验割裂。

本桥的形态（OAuth 授权码模式的外观与体验）：

  管理后台「发起授权」→ 得到一条授权链接（+二维码）
  → 用户手机/PC 浏览器打开链接（整页 302 到桥内登录页，
     长得就是官方登录页，地址栏是本站域名）
  → 用户输邮箱、收验证码、拖滑块 —— 自己的真实浏览器、真实网络环境
  → 登录成功的瞬间，服务端**自动**检测登录态 cookie（无人工按钮）
  → 自动 fetch_userinfo 校验 → 自动入库 → 桥会话关闭
  → 用户手机页面自动变成「✅ 授权成功」页；管理后台实时收到通知并刷新

## 与 OAuth 的对应关系（形象类比，非协议级）

  OAuth 授权码模式            本桥
  ─────────────────         ─────
  授权链接 (client_id…)   →  /login-bridge/<bridge_id>
  授权页（登录+同意）      →  官方登录页（桥内反代，用户全程无感知）
  redirect_uri 回跳       →  服务端截获 Set-Cookie（等价物：登录态即授权物）
  code 换 token           →  cookie → fetch_userinfo 校验 → Credential 落盘
  授权完成页              →  桥自动渲染的「授权成功，可关闭页面」

## 与同源策略的关系（为什么必须服务端截 cookie）

登录态 cookie（`xman_t`/`cookie2`/`phoenix_cookie`/`sgcookie`/`xman_f`，
实测 2026-09-23 全部 HttpOnly）落在代理域下，`document.cookie` 读不到
HttpOnly，前端 JS 永远摸不到——抓取只能在服务端：`_CookieCaptor` 在
转发响应时抄录 `Set-Cookie`。

## 风控（Baxia）边界 —— 已实测的事实（2026-09-24 网络轨迹侦察）

* 官方登录页是顶层页（`https://www.accio.com/login`，无跨域 iframe 包裹），
  cookie domain 全 `.accio.com`，反代后 cookie 逻辑等价；
* 风控信号集中在 MTOP 接口与行为流（`acs.h.accio.com/h5/mtop/*` +
  `fireyejs`/`awsc.js` 指纹采集），页面文件本身不带域名校验；
* 静态资源域（`g.alicdn.com`/`aeis.alicdn.com` 等）**直连不代理**：
  无 cookie 参与，跨域加载没问题。

未知数（首次真实使用才算数）：Baxia 是否校验 `Referer`/`Origin` 与官方
域的差异。缓解：转发时把这些头改写为官方域；被拦再升级，别提前猜。

## 安全设计

* **入口令牌**：桥 URL 含 192bit 随机 `bridge_id`，本身即能力凭证；
  用户浏览器无法带 ADMIN_KEY Header，故入口页不设 Header 鉴权，靠
  不可枚举 ID + 10 分钟未使用过期 + 单会话短 TTL 兜底。
* **管理面强制 ADMIN_KEY**：桥的创建/查询/关闭全部 `_admin_guard`。
  入库（finish）同样强制 ADMIN_KEY——但由**服务端 worker** 在检测到
  登录态后自动调用，用户浏览器全程不接触管理凭据。
* **不落盘转发内容**：captor 只存 cookie 名值对（内存，会话关闭即焚），
  不记请求体、不记响应体。
"""

from __future__ import annotations

import re
import secrets
import threading
import time
from email.utils import parsedate_to_datetime
from http import cookies as http_cookies
from urllib.parse import urlsplit, urlunsplit

import httpx
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import HTMLResponse

from ..core.config import settings

UPSTREAM_HOST = "www.accio.com"
_ACCIO_RE = re.compile(r"^([a-z0-9.-]+\.)*accio\.com$", re.IGNORECASE)

# 直连白名单后缀：无 cookie 参与的静态 CDN，用户浏览器直打官方（不代理）
_DIRECT_SUFFIXES = ("alicdn.com", "ucweb.com", "mmstat.com",
                    "aliyun.com", "taobao.com", "alibaba.com")

_BRIDGE_TTL_UNUSED_S = 600    # 创建后 10 分钟没人打开 → 作废
_BRIDGE_TTL_ACTIVE_S = 3600   # 打开后 1 小时 → 作废
_BRIDGE_TTL_DONE_S = 1800     # 入库完成后保留 30 分钟供结果页/后台轮询，随后焚毁
_MAX_BODY = 2 * 1024 * 1024   # 转发请求体上限（登录页请求都很小）

router = APIRouter()

import logging as _logging
log_bridge = _logging.getLogger("accio2api.login_bridge")


# ── 会话管理 ─────────────────────────────────────────────────

class _Bridge:
    __slots__ = ("id", "created", "first_hit", "cookies", "cookie_domains",
                 "cookie_httponly", "email", "stage", "done", "lock",
                 "saving", "save_error", "saved_cred", "last_page", "done_at")

    def __init__(self):
        self.id = secrets.token_urlsafe(24)          # 192bit
        self.created = time.time()
        self.first_hit = 0.0
        self.cookies: dict[str, str] = {}            # 服务端截获的 cookie 值
        self.cookie_domains: dict[str, str] = {}     # name → 上游 Domain（小写，无前导点）
        self.cookie_httponly: set[str] = set()       # 上游带 HttpOnly 的 cookie 名
        self.email = ""                              # 从登录页输入框流里顺带识别（best effort）
        self.stage = "created"                       # created → visited → cookies_seen
        self.done = False
        self.done_at = 0.0                           # done 时刻（宽限期计时起点）
        # ── 自动入库 worker 状态（OAuth「code 换 token」等价物）──
        self.saving = False            # worker 正在校验/落盘
        self.save_error = ""           # 最近一次自动入库失败原因（空=无错）
        self.saved_cred: dict = {}     # 入库成功后的凭证公开字段（to_public）
        self.last_page = ""            # 用户浏览器最近请求的官方 path（判断是否已离开 /login）
        self.lock = threading.Lock()

    def snapshot(self) -> dict:
        with self.lock:
            core = [n for n in ("xman_t", "cookie2", "phoenix_cookie",
                                "sgcookie", "xman_f") if n in self.cookies]
            return {
                "bridge_id": self.id,
                "stage": self.stage,
                "created": self.created,
                "first_hit": self.first_hit,
                "cookie_count": len(self.cookies),
                "core_cookies": core,
                "ready": len(core) >= 2,   # 核心件套到位即认为登录态已生成
                "done": self.done,
                "saving": self.saving,
                "save_error": self.save_error,
                "saved_cred": self.saved_cred,
                "last_page": self.last_page,
            }


class _BridgeRegistry:
    def __init__(self):
        self._m: dict[str, _Bridge] = {}
        self._lock = threading.Lock()

    def create(self) -> _Bridge:
        b = _Bridge()
        with self._lock:
            self._purge()
            self._m[b.id] = b
        return b

    def get(self, bridge_id: str) -> _Bridge | None:
        with self._lock:
            self._purge()
            return self._m.get(bridge_id)

    def all(self) -> list[_Bridge]:
        with self._lock:
            self._purge()
            return list(self._m.values())

    def close(self, bridge_id: str) -> bool:
        with self._lock:
            return self._m.pop(bridge_id, None) is not None

    def _purge(self):
        now = time.time()
        dead = [k for k, b in self._m.items()
                if (b.done and b.done_at and now - b.done_at > _BRIDGE_TTL_DONE_S)
                or (b.first_hit and now - b.first_hit > _BRIDGE_TTL_ACTIVE_S)
                or (not b.first_hit and now - b.created > _BRIDGE_TTL_UNUSED_S)]
        for k in dead:
            self._m.pop(k, None)


bridges = _BridgeRegistry()


# ── URL 改写 ─────────────────────────────────────────────────

def _proxify_path(host: str, path: str) -> str:
    """官方 host+path → 本站路径 /lb/<host><path>。"""
    if not path.startswith("/"):
        path = "/" + path
    return f"/lb/{host}{path}"


def _rewrite_text(data: bytes) -> bytes:
    """HTML/JS/CSS 里的官方域绝对 URL → 本站 /lb/<host>/... 相对路径。

    只动 accio.com 系（登录相关），CDN 域保持原样直连。
    HTML 响应额外注入运行时 shim（修 mtop 域名推导，见 _SHIM_JS 注释）。
    """
    txt = data.decode("utf-8", errors="replace")
    txt = re.sub(r"https?://([a-z0-9.-]+\.accio\.com)(?=[/\"'?)#])",
                 r"/lb/\1", txt, flags=re.IGNORECASE)
    txt = re.sub(r"(?P<q>[\"'(=])//([a-z0-9.-]+\.accio\.com)(?=[/\"'?)#])",
                 r"\g<q>/lb/\2", txt, flags=re.IGNORECASE)
    # CSP / X-Frame 由响应头层剥除（_strip_headers），meta 版本这里剥
    txt = re.sub(r"<meta[^>]+http-equiv=[\"']?content-security-policy[^>]*>.*?</meta>",
                 "", txt, flags=re.IGNORECASE | re.DOTALL)
    txt = re.sub(r"<meta[^>]+http-equiv=[\"']?content-security-policy[^>]*>",
                 "", txt, flags=re.IGNORECASE)
    # HTML → 注入 shim（<head> 最前，先于一切页面 JS）
    if "<head" in txt.lower():
        shim = f"<script>{_SHIM_JS}</script>"
        txt = re.sub(r"(<head[^>]*>)",
                     lambda m: m.group(1) + shim,
                     txt, count=1, flags=re.IGNORECASE)
    return txt.encode("utf-8", errors="replace")


def _location_to_local(value: str) -> str:
    """上游 Location 头 → 本站路径（保持在桥内）。"""
    s = urlsplit(value)
    if s.netloc:
        if _ACCIO_RE.match(s.netloc):
            return _proxify_path(s.netloc.lower(), s.path or "/") + (
                f"?{s.query}" if s.query else "")
        return value            # 跳去非官方域（如支付页）→ 原样
    return value


# ── 运行时 shim ──────────────────────────────────────────────
# 官方 JS（mtop.js）从 location.hostname 推导 MTOP API 域：
#   www.accio.com → mainDomain "accio.com" → "//acs.h.accio.com/h5/..."
# 代理域下 hostname 不是 accio.com，推导崩坏：
#   127.0.0.1 → mainDomain "0.1" → "//acs.h.0.1/h5/..." → XHR Invalid URL
#   你的域名 → mainDomain "xiao...com" → "//acs.h.你的域名/..." → 上游 404/NXDOMAIN
# 因此在所有代理 HTML <head> 最前注入补丁：
#   ① 劫持 XHR.open / fetch：把推导坏掉的 MTOP URL 改写为
#      /lb/acs.h.accio.com/h5/...（官方真实接口域，走桥）；
#   ② 兜住 window.location 相关的域判断（防御性，官方还有别处 split hostname）。

_SHIM_JS = """
(function(){
  var M_HOST = 'acs.h.accio.com';
  var L_HOST = 'login.accio.com';
  // 把坏推导的 MTOP / 登录域 URL 修成桥内路径；其余 URL 不动
  function fix(u){
    if (typeof u !== 'string') return u;
    // ── MTOP 接口域：//acs.h.<badmain>/h5/mtop... ──
    var m = u.match(/^\\/\\/[a-z0-9.-]*acs\\.h\\.[^/]+(\\/h5\\/.*)$/) ||
            u.match(/^https?:\\/\\/[a-z0-9.-]*acs\\.h\\.[^/]+(\\/h5\\/.*)$/);
    if (m) return '/lb/' + M_HOST + m[1];
    // ── 登录子域：//login.<badmain>/... （captcha token 等整页 API）──
    //    官方 hostname=www.accio.com → mainDomain=accio.com → login.accio.com；
    //    代理域 hostname=127.0.0.1 → mainDomain=0.1 → login.0.1（DNS 不存在）。
    m = u.match(/^\\/\\/login\\.[^/]+(\\/.*)$/) ||
        u.match(/^https?:\\/\\/login\\.[^/]+(\\/.*)$/);
    if (m) return '/lb/' + L_HOST + m[1];
    // 已是相对路径且指向我们桥的（/lb/...）不动
    return u;
  }
  var oOpen = XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open = function(method, url){
    var args = [].slice.call(arguments);
    args[1] = fix(url);
    return oOpen.apply(this, args);
  };
  var oFetch = window.fetch;
  if (oFetch) {
    window.fetch = function(input, init){
      try {
        if (typeof input === 'string') input = fix(input);
        else if (input && input.url) input.url = fix(input.url);
      } catch(e) {}
      return oFetch.call(this, input, init);
    };
  }
  // 动态 <script src> / <img src> 等由 MutationObserver 兜底改写
  try {
    var mo = new MutationObserver(function(muts){
      muts.forEach(function(mu){
        mu.addedNodes && mu.addedNodes.forEach(function(n){
          if (!n || n.nodeType !== 1) return;
          ['src','href','action'].forEach(function(attr){
            var v = n.getAttribute && n.getAttribute(attr);
            if (!v) return;
            var m2 = v.match(/^\\/\\/[a-z0-9.-]*acs\\.h\\.[^/]+(\\/.*)$/) ||
                     v.match(/^https?:\\/\\/[a-z0-9.-]*acs\\.h\\.[^/]+(\\/.*)$/);
            if (m2) { n.setAttribute(attr, '/lb/' + M_HOST + m2[1]); return; }
            m2 = v.match(/^\\/\\/login\\.[^/]+(\\/.*)$/) ||
                 v.match(/^https?:\\/\\/login\\.[^/]+(\\/.*)$/);
            if (m2) n.setAttribute(attr, '/lb/' + L_HOST + m2[1]);
          });
        });
      });
    });
    mo.observe(document.documentElement || document, {childList:true, subtree:true});
  } catch(e) {}
})();
"""


# ── Cookie 截获 ──────────────────────────────────────────────

def _capture_set_cookies(bridge: _Bridge, set_cookie_values: list[str]) -> None:
    """从上游 Set-Cookie 抄录 name→value（入库用）+ Domain/HttpOnly 标志（回种用）。"""
    now = time.time()

    def flags(raw: str) -> tuple[str, bool]:
        dom, ho = "", False
        for part in raw.split(";")[1:]:
            k, _, v = part.strip().partition("=")
            if k.lower() == "domain":
                dom = v.lower().lstrip(".")
            elif k.lower() == "httponly":
                ho = True
        return dom, ho

    with bridge.lock:
        for raw in set_cookie_values:
            dom, ho = flags(raw)
            c = http_cookies.SimpleCookie()
            try:
                c.load(raw)
            except Exception:
                first = raw.split(";")[0]
                if "=" in first:
                    n, _, v = first.partition("=")
                    n, v = n.strip(), v.strip()
                    if n and v:
                        bridge.cookies[n] = v
                        if dom:
                            bridge.cookie_domains[n] = dom
                        if ho:
                            bridge.cookie_httponly.add(n)
                continue
            for name, morsel in c.items():
                expires = morsel["expires"]
                if expires:
                    try:
                        if parsedate_to_datetime(expires).timestamp() < now:
                            bridge.cookies.pop(name, None)
                            bridge.cookie_domains.pop(name, None)
                            bridge.cookie_httponly.discard(name)
                            continue
                    except Exception:
                        pass
                v = morsel.value or ""
                if v:
                    bridge.cookies[name] = v
                    if dom:
                        bridge.cookie_domains[name] = dom
                    if ho:
                        bridge.cookie_httponly.add(name)
                else:
                    bridge.cookies.pop(name, None)     # 删除型 Set-Cookie
                    bridge.cookie_domains.pop(name, None)
                    bridge.cookie_httponly.discard(name)


def _replant_cookies(bridge: _Bridge) -> list[str]:
    """把截获的官方 cookie 回种到本代理域（服务端中转态 → 浏览器态）。

    官方页 JS（mtop 签名 / baxia 指纹）要在**浏览器里**读到 `_m_h5_tk`、
    `cna` 等 cookie 才能初始化；HttpOnly 票据也一并种上，保证后续请求
    原样回传。HttpOnly 标志**逐 cookie 按上游原样**：JS 需要读的
    （如 `_m_h5_tk`）绝不能加，否则 mtop 签名直接挂。

    Domain 属性一律不设（host-only 到当前代理域）；上游 Domain=.accio.com
    在代理侧本就聚合为一份。

    返回值：Set-Cookie 头**列表**（每条一个 cookie，RFC 6265 不允许合并）。
    """
    parts: list[str] = []
    with bridge.lock:
        items = sorted(bridge.cookies.items())
        ho_names = set(bridge.cookie_httponly)
    for name, val in items:
        v = val.replace('"', "%22")
        flag = "; HttpOnly" if name in ho_names else ""
        parts.append(f"{name}={v}; Path=/; Max-Age=86400; SameSite=Lax{flag}")
    return parts


# ── 内部工具 ─────────────────────────────────────────────────

_HOP_REQ = {"connection", "keep-alive", "proxy-authenticate",
            "proxy-authorization", "te", "trailer", "transfer-encoding",
            "upgrade", "host", "content-length", "expect"}

_HOP_RESP = {"connection", "keep-alive", "proxy-authenticate",
             "proxy-authorization", "te", "trailer", "transfer-encoding",
             "upgrade", "content-encoding", "content-length",
             "content-security-policy", "content-security-policy-report-only",
             "x-frame-options", "set-cookie", "strict-transport-security"}


def _parse_lb(path: str) -> tuple[str, str]:
    """/lb/<host>[/<rest>] → (host, /rest)。host 非 accio.com 系 → 空。"""
    m = re.match(r"^/lb/([^/]+)(/.*)?$", path, re.IGNORECASE)
    if not m:
        return "", ""
    host = m.group(1).lower()
    rest = m.group(2) or "/"
    if not _ACCIO_RE.match(host):
        return "", ""
    return host, rest


def _bridge_from_request(request: Request) -> _Bridge | None:
    """归属当前 bridge：优先 ?b= 参数，否则取唯一活跃桥。"""
    bid = request.query_params.get("b")
    if bid:
        return bridges.get(bid)
    live = [b for b in bridges.all() if b.first_hit and not b.done]
    if len(live) == 1:
        return live[0]
    # 多桥并发时靠 cookie 里的桥标记兜底
    bid = request.cookies.get("_lb_bridge")
    return bridges.get(bid) if bid else None


# ── 透明路径表：SPA 路由必须原样（React Router 读 location.pathname）──
# /login → www.accio.com/login；/gateway/* → www.accio.com/gateway/*
# 其余未匹配路径 → 404（不吞我们自己的路由）
_TRANSPARENT_PREFIXES = (
    "/login",            # 登录页（含子路径 /login/xxx）
    "/gateway/",         # 业务 API
    "/h5/",              # mtop 备用路径
)


def _transparent_target(path: str) -> str | None:
    """本站路径 → 官方 www.accio.com 路径（透明代理）；不匹配返回 None。"""
    for p in _TRANSPARENT_PREFIXES:
        if path == p or path.startswith(p):
            if p == "/login" and path != "/login" and not path.startswith("/login/"):
                continue
            return path
    return None


def _mark_visited(bridge: _Bridge, path: str = "") -> None:
    if not bridge.first_hit:
        bridge.first_hit = time.time()
        bridge.stage = "visited"
    if path:
        bridge.last_page = path


# 登录态专有 cookie（web_login.py 实测同款判定）：
# 未登录的登录页也会带 18 个 accio cookie（_m_h5_tk / cna / tfstk 等风控
# cookie），只看「有 cookie」会一路误判。必须出现登录成功才会有的名字。
LOGIN_MARKERS = ("xman_t", "munb", "_nk_", "sgcookie")


def _maybe_autosave(bridge: _Bridge) -> None:
    """登录态检测 + 自动入库（OAuth「回调换 token」等价物）。

    触发条件（与 web_login.py 同款双因子，缺一不可）：
      ① 桥内截获了登录态专有 cookie（xman_t / munb / _nk_ / sgcookie 任一）
      ② 用户浏览器已离开登录页（last_page 不再是 /login*，官方登录成功
         后 302 去首页/工作台）——防止登录中途的风控 cookie 误触发。

    全程无人工按钮：worker 线程做校验+落盘（fetch_userinfo 需上游网络
    往返，不能卡代理请求线程）。重复触发由 saving/done 双标志挡住。
    """
    with bridge.lock:
        if bridge.done or bridge.saving:
            return
        has_marker = any(m in bridge.cookies for m in LOGIN_MARKERS)
        left_login = bool(bridge.last_page) and not bridge.last_page.startswith("/login")
        if not (has_marker and left_login):
            return
        bridge.saving = True
        bridge.save_error = ""

    def _worker():
        from ..core.credentials import Credential, pool as _pool
        from ..upstream.client import AccioClient

        try:
            with bridge.lock:
                cookies = dict(bridge.cookies)
            client = AccioClient(cookies=cookies)
            info = client.fetch_userinfo()          # 校验 + 拉身份（上游网络往返）
            uid = str(info.get("userId") or info.get("uid") or "")
            if not uid:
                raise RuntimeError("无法解析用户身份（userId 为空）")

            from ..admin_api import _enrich
            cred = Credential(
                user_id=uid,
                accio_id=str(info.get("accioId") or ""),
                nickname=str(info.get("userName") or info.get("nickname") or ""),
                email=str(info.get("email") or info.get("desensitizedEmail")
                          or bridge.email),
                cookies=cookies,
            )
            _enrich(cred, client)
            _pool.save(cred)
            with bridge.lock:
                bridge.saved_cred = dict(cred.to_public())
                bridge.done = True
                bridge.done_at = time.time()
                bridge.save_error = ""
            # 不 close：桥保留在注册表中（done 态），供用户页/后台轮询取结果。
            # 桥内截获的 cookie 立即清空——凭证已落盘，会话内敏感物即刻焚毁。
            with bridge.lock:
                bridge.cookies.clear()
                bridge.cookie_domains.clear()
            bridge.cookie_httponly.clear()
            log_bridge.info("登录桥 %s… 自动入库成功：%s",
                            bridge.id[:8], cred.email or cred.nickname or uid)
        except Exception as e:                    # noqa: 校验失败 → 回到待检测态重试
            with bridge.lock:
                bridge.saving = False
                bridge.save_error = str(e)[:200]
            log_bridge.warning("登录桥 %s… 自动入库失败：%s",
                               bridge.id[:8], str(e)[:200])

    threading.Thread(target=_worker, name=f"lb-autosave-{bridge.id[:8]}",
                     daemon=True).start()


def _resolve_upstream_url(host: str, path: str, query: str) -> str:
    return urlunsplit(("https", host, path, query, ""))


def _resp_with_cookies(content: bytes, status: int, headers: dict,
                       ctype: str, set_cookies: list[str]) -> Response:
    """构造带多条 Set-Cookie 的响应（每条独立头，RFC 6265）。

    Starlette Response 只接受单值 header dict；这里先建再逐条 append。
    """
    resp = Response(content=content, status_code=status,
                    headers=headers, media_type=ctype or None)
    for sc in set_cookies:
        resp.headers.append("set-cookie", sc)
    return resp


# ── 代理端点 ─────────────────────────────────────────────────

@router.api_route("/lb/{host_path:path}", methods=[
    "GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])
async def lb_proxy(host_path: str, request: Request):
    """通用反代端点：/lb/<accio-host>/<path> → https://<host>/<path>。"""
    host, rest = _parse_lb("/lb/" + host_path)
    if not host:
        raise HTTPException(404, detail={"error": {"message": "unknown bridge path"}})
    return await _do_proxy(host, rest, request)


# ── 透明代理端点（SPA 路由保持官方 pathname）────────────────
# /login → https://www.accio.com/login
# /gateway/xxx → https://www.accio.com/gateway/xxx
# React Router 读 location.pathname，/lb/www.accio.com/login 这种前缀
# 会让它匹配不到路由渲染官方 404 组件——所以登录页与 API 用透明路径。

@router.api_route("/login", methods=[
    "GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])
@router.api_route("/login/{rest:path}", methods=[
    "GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])
@router.api_route("/gateway/{rest:path}", methods=[
    "GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])
async def transparent_proxy(request: Request, rest: str = ""):
    return await _do_proxy(UPSTREAM_HOST, "/login" if not rest and
                           request.url.path == "/login" else
                           request.url.path, request)


async def _do_proxy(host: str, rest: str, request: Request):
    """代理主逻辑（/lb/ 与透明路径共用）。"""
    bridge = _bridge_from_request(request)
    if not bridge:
        raise HTTPException(410, detail={"error": {
            "message": "登录桥不存在或已过期 —— 请回到管理页重新创建"}})

    # 直连域（CDN）不该出现在这里；硬兜底直接 302 到官方
    if any(host.endswith(s) for s in _DIRECT_SUFFIXES):
        return Response(status_code=302, headers={
            "Location": _resolve_upstream_url(host, rest, request.url.query)})

    _mark_visited(bridge, request.url.path)

    # ── 构造转发头 ──
    fwd: dict[str, str] = {}
    for k, v in request.headers.items():
        lk = k.lower()
        if lk in _HOP_REQ or lk in ("accept-encoding", "cookie"):
            continue
        # sec-fetch-* 描述的是「浏览器→代理」的关系，代理→上游的关系
        # 应由我们重造：官方场景下页面(www)调接口(acs.h)是 same-site。
        # 直接剥掉，让上游看到的是一个干净的服务端请求（httpx 默认不发）。
        if lk.startswith("sec-fetch-") or lk == "origin" or lk == "referer":
            continue
        fwd[k] = v
    fwd["host"] = host

    # ── Referer / Origin 重建（MTOP 风控白名单校验这两项）──
    # 官方拓扑：页面 www.accio.com/login 里的 JS 调 acs.h.accio.com，
    # 浏览器发 Origin: https://www.accio.com
    #           Referer: https://www.accio.com/login
    # 即两者永远是「页面域」，不是接口域。代理侧页面 URL 是
    # /lb/<page-host>/<path>，从这里反推页面域。
    page_host, page_path = UPSTREAM_HOST, "/login"
    raw_ref = request.headers.get("referer", "")
    if raw_ref:
        rs = urlsplit(raw_ref)
        if rs.path.startswith("/lb/"):
            ph, pp = _parse_lb(rs.path)
            if ph:
                page_host, page_path = ph, pp
        elif _ACCIO_RE.match(rs.netloc or ""):
            page_host, page_path = rs.netloc.lower(), rs.path or "/"
    fwd["referer"] = f"https://{page_host}{page_path}"
    # POST/PUT 等带 body 的请求浏览器才会带 Origin；重建时统一给页面域
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        fwd["origin"] = f"https://{page_host}"

    # ── 用户浏览器带来的 cookie：直接转发官方 cookie（若有截获值）──
    with bridge.lock:
        ck_map = dict(bridge.cookies)
    if ck_map:
        fwd["cookie"] = "; ".join(f"{k}={v}" for k, v in sorted(ck_map.items()))

    body = await request.body() if request.method in ("POST", "PUT", "PATCH") else None
    if body and len(body) > _MAX_BODY:
        raise HTTPException(413, detail={"error": {"message": "body too large"}})

    url = _resolve_upstream_url(host, rest, request.url.query)
    try:
        async with httpx.AsyncClient(
                timeout=httpx.Timeout(30.0, connect=10.0),
                follow_redirects=False) as cli:
            r = await cli.request(request.method, url, headers=fwd,
                                  content=body if body else None,
                                  params=None)
    except httpx.HTTPError as e:
        raise HTTPException(502, detail={"error": {
            "message": f"上游请求失败：{type(e).__name__}: {str(e)[:160]}"}})

    # ── 截获 Set-Cookie ──
    raw_sc = r.headers.get_list("set-cookie") if hasattr(r.headers, "get_list") \
        else [v for k, v in r.headers.items() if k.lower() == "set-cookie"]
    if raw_sc:
        _capture_set_cookies(bridge, raw_sc)
        with bridge.lock:
            if bridge.stage != "cookies_seen" and any(
                    n in bridge.cookies for n in
                    ("xman_t", "cookie2", "phoenix_cookie")):
                bridge.stage = "cookies_seen"

    # ── 自动入库检测（登录成功瞬间，无人工按钮）──
    #    放在 Set-Cookie 截获之后：worker 看到的是本响应刚写全的 cookie。
    _maybe_autosave(bridge)

    # ── 构造响应 ──
    resp_headers = {}
    for k, v in r.headers.items():
        lk = k.lower()
        if lk in _HOP_RESP:
            continue
        if lk == "location":
            resp_headers[k] = _location_to_local(v)
            continue
        resp_headers[k] = v

    content = r.content
    ctype = r.headers.get("content-type", "")
    if ("text/html" in ctype or "javascript" in ctype or
            "json" in ctype or "css" in ctype):
        content = _rewrite_text(content)

    # 给浏览器种桥标记 + 回种截获的官方 cookie（浏览器态补全，JS 依赖）
    # ⚠️ Set-Cookie 不可合并：每个 cookie 必须是独立的响应头（RFC 6265）。
    sc_list = [f"_lb_bridge={bridge.id}; Path=/; SameSite=Lax; Max-Age=3600"]
    sc_list.extend(_replant_cookies(bridge))
    return _resp_with_cookies(content, r.status_code, resp_headers,
                              ctype, sc_list)


# ── 入口跳转与授权结果页（OAuth 形态）────────────────────────
#
# 旧版：入口页嵌 iframe 代理官方首页 —— 手机端 iframe 嵌官方登录页
#       在 iOS Safari / 部分安卓 WebView 下会被弹层/风控组件打穿，
#       且「我已登录完成」按钮要求用户手动点，体验断裂。
# 新版（对齐 OAuth 授权码模式的用户旅程）：
#   /login-bridge/<id>  → 302 整页跳到桥内官方登录页（同域顶层页，非 iframe）
#   用户登录成功瞬间     → 服务端 worker 自动校验+入库（_maybe_autosave）
#   桥内页面（轮询中）   → 自动变成「授权成功」页（_WAIT_HTML），零点击
#   done 后再访问入口    → 直接渲染成功页（用户刷新/回跳也不穿帮）

_WAIT_HTML = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>等待授权结果…</title>
<style>
 body{font-family:system-ui,-apple-system,"PingFang SC",sans-serif;margin:0;
      background:#0f1115;color:#e8eaf0;display:flex;align-items:center;
      justify-content:center;min-height:100vh}
 .box{max-width:520px;padding:40px 28px;text-align:center}
 h1{font-size:22px;margin:0 0 10px}
 p{color:#9aa3b2;font-size:14px;line-height:1.7;margin:0 0 6px}
 .ok{color:#4ade80}.bad{color:#f87171}
 .spin{width:34px;height:34px;margin:0 auto 22px;border-radius:50%;
       border:3px solid #232833;border-top-color:#2f6bff;
       animation:r 1s linear infinite}
 @keyframes r{to{transform:rotate(360deg)}}
 .step{font-size:13px;color:#9aa3b2;margin-top:18px}
</style></head><body><div class="box">
<div class="spin" id="sp"></div>
<h1 id="h">正在等待授权结果…</h1>
<p id="p">如果你在另一个标签页完成了登录，这里会自动收到结果，无需任何操作。</p>
<p class="step" id="s">已捕获 0 个 cookie</p>
<script>
const BR="__BRIDGE_ID__", API="__API_BASE__";
let failStreak=0;
async function poll(){
  try{
    const r=await fetch(`${API}?bridge_id=${BR}`,{cache:"no-store"});
    const j=await r.json(); const d=j.data||j;
    failStreak=0;
    const s=document.getElementById("s");
    if(d.done && d.saved_cred && d.saved_cred.account_key){
      document.getElementById("sp").style.display="none";
      document.getElementById("h").textContent="✅ 授权成功";
      document.getElementById("h").className="ok";
      document.getElementById("p").innerHTML=
        `账号 <b style="color:#e8eaf0">${(d.saved_cred.nickname||d.saved_cred.email||d.saved_cred.user_id||"").toString().replace(/</g,"&lt;")}</b>`+
        `<br>凭证已自动保存到系统，本页面可以关闭了。`;
      s.textContent="";
      clearInterval(t); return;
    }
    if(d.saving){
      document.getElementById("h").textContent="正在校验登录态并保存…";
      s.textContent="已捕获 "+d.cookie_count+" 个 cookie，正在验证有效性";
      return;
    }
    if(d.save_error){
      s.innerHTML=`<span class="bad">校验暂未通过（${d.save_error.toString().replace(/</g,"&lt;")}），将继续重试</span>`;
      return;
    }
    if(d.ready){
      document.getElementById("h").textContent="已检测到登录，正在确认…";
      s.textContent="已捕获 "+d.cookie_count+" 个 cookie（核心 "+d.core_cookies.length+" 件）";
      return;
    }
    s.textContent="已捕获 "+d.cookie_count+" 个 cookie";
  }catch(e){
    if(++failStreak>20){ document.getElementById("s").textContent="连接中断，请检查网络后刷新本页"; }
  }
}
const t=setInterval(poll,2500); poll();
</script></div></body></html>"""


@router.get("/login-bridge/{bridge_id}")
def bridge_entry(bridge_id: str, request: Request):
    b = bridges.get(bridge_id)
    if not b:
        return HTMLResponse(
            "<meta charset=utf-8><body style='font-family:system-ui;padding:40px'>"
            "<h2>链接已失效</h2><p>授权链接 10 分钟未使用自动作废，"
            "请回管理后台重新发起。</p></body>",
            status_code=410)
    # 已完成 → 直接渲染成功页（用户刷新/回跳不穿帮）
    with b.lock:
        done, cred = b.done, dict(b.saved_cred)
    if done:
        html = _WAIT_HTML.replace("__BRIDGE_ID__", b.id).replace(
            "__API_BASE__", "/admin/api/login-bridge/status")
        return HTMLResponse(html)
    _mark_visited(b, "/login")
    # OAuth 形态核心：整页 302 跳到桥内官方登录页（顶层页，非 iframe）。
    # 登录页内的所有子请求带 ?b= 桥标记（_lb_bridge cookie 兜底），
    # 服务端逐响应截获 Set-Cookie；登录成功瞬间自动校验入库。
    from fastapi.responses import RedirectResponse
    return RedirectResponse(f"/login?b={b.id}", status_code=302)


# ── 管理端 API（强制 ADMIN_KEY）─────────────────────────────

def _guard(request: Request):
    from ..admin_api import _admin_guard  # noqa: 仍在 app 包内相对可用
    _admin_guard(request)


@router.post("/admin/api/login-bridge")
def bridge_create(request: Request):
    """创建登录桥，返回用户要打开的 URL（+ 二维码 data URI）。

    二维码服务端生成（qrcode 包，纯本地无网络请求）：管理后台 <img>
    直接收 data URI，省掉浏览器端 QR 库依赖与额外鉴权请求。
    """
    _guard(request)
    b = bridges.create()
    base = str(request.base_url).rstrip("/")
    url = f"{base}/login-bridge/{b.id}"
    qr_uri = ""
    try:
        import io as _io
        import qrcode as _qrcode
        img = _qrcode.make(url)
        buf = _io.BytesIO()
        img.save(buf, format="PNG")
        qr_uri = ("data:image/png;base64,"
                  + __import__("base64").b64encode(buf.getvalue()).decode())
    except Exception as e:                     # noqa: qrcode 未装 → 仅链接可用
        log_bridge.warning("二维码生成失败（不影响链接使用）：%s", e)
    return {"data": b.snapshot(), "url": url, "qr": qr_uri,
            "message": "已创建，把 URL 或二维码发给要登录的人（手机浏览器直接打开）"}


@router.get("/admin/api/login-bridge")
def bridge_list(request: Request):
    _guard(request)
    return {"data": [b.snapshot() for b in bridges.all()]}


@router.get("/admin/api/login-bridge/status")
def bridge_status(request: Request, bridge_id: str = ""):
    """入库前状态查询（入口页轮询用；凭 bridge_id 即可，不需 ADMIN_KEY，
    因为 ID 本身是能力凭证，且只回状态不回 cookie 值）。"""
    b = bridges.get(bridge_id)
    if not b:
        raise HTTPException(404, detail={"error": {"message": "桥不存在或已过期"}})
    return {"data": b.snapshot()}


@router.post("/admin/api/login-bridge/finish")
async def bridge_finish(request: Request):
    """登录完成后入库：bridge_id 校验 → cookie 校验（fetch_userinfo）→ 凭证落盘。

    ⚠️ 入库是敏感动作，双因子：既要 bridge_id（能力凭证）又要 ADMIN_KEY
    （管理凭证）——防止拿到桥 URL 的人（不限于管理员）把任意会话固化入库。
    """
    from ..admin_api import _admin_guard
    _admin_guard(request)
    from ..core.credentials import Credential, pool as _pool
    from ..upstream.client import AccioClient

    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    bid = str(body.get("bridge_id") or request.query_params.get("bridge_id") or "")
    b = bridges.get(bid)
    if not b:
        raise HTTPException(404, detail={"error": {"message": "桥不存在或已过期"}})

    with b.lock:
        cookies = dict(b.cookies)
    core = [n for n in ("xman_t", "cookie2", "phoenix_cookie", "sgcookie") if n in cookies]
    if len(core) < 2:
        raise HTTPException(400, detail={"error": {
            "message": f"登录态未生成（核心 cookie 仅 {len(core)} 个），请先完成登录",
            "type": "bridge_not_ready"}})

    client = AccioClient(cookies=cookies)
    try:
        info = client.fetch_userinfo()
    except Exception as e:
        raise HTTPException(502, detail={"error": {
            "message": f"登录态校验失败：{str(e)[:200]}"}})
    uid = str(info.get("userId") or info.get("uid") or "")
    if not uid:
        raise HTTPException(502, detail={"error": {"message": "无法解析用户身份"}})

    from ..admin_api import _enrich
    cred = Credential(
        user_id=uid,
        accio_id=str(info.get("accioId") or ""),
        nickname=str(info.get("userName") or info.get("nickname") or ""),
        email=str(info.get("email") or info.get("desensitizedEmail") or b.email),
        cookies=cookies,
    )
    _enrich(cred, client)
    _pool.save(cred)
    b.done = True
    b.done_at = time.time()
    with b.lock:
        b.saved_cred = dict(cred.to_public())
        b.cookies.clear()
        b.cookie_domains.clear()
    b.cookie_httponly.clear()
    # 桥保留在注册表（done 态）供结果轮询；_purge 会按 done 宽限期回收
    return {"data": dict(cred.to_public(), saved=True),
            "message": "登录成功，凭证已保存"}


@router.delete("/admin/api/login-bridge/{bridge_id}")
def bridge_close(bridge_id: str, request: Request):
    _guard(request)
    if not bridges.close(bridge_id):
        raise HTTPException(404, detail={"error": {"message": "桥不存在"}})
    return {"message": "已关闭，截获的 cookie 已丢弃"}
