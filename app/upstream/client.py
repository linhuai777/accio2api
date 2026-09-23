"""Accio 上游客户端 —— 动态发现端点 + WS 流式调用。

⚠ 本项目**不硬编码任何实例地址**。作者调试时看到的
  `wss://<实例ID>.agentbay.<上游域>/...` 属于**某次会话的运行时地址**，
  别人的账号必须自己动态取。

调用链（每一环都动态获取）：
  1. `GET  /gateway/models`
        → 模型清单（代号混淆，动态拉取，不写死）
  2. `POST /gateway/host-im/websocket-capability`
        → capability token
  3. WS   `.../websocket/connect`
        → hello 握手 → `req/sendQuery` → 收 `event/delta` 流

WS 地址从哪来？实测它出现在前端埋点上报里（`chat.session_ws.connect_start`）。
本实现改为**从 WS capability 响应与页面引导接口推导**，并缓存；
若拿不到则回退到「打开 work/app 抓取」。见 `resolve_ws_url()`。
"""

from __future__ import annotations

import json
import re
import ssl
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable, Iterator
from urllib.parse import urlencode, urlsplit

import logging

log = logging.getLogger(__name__)

# ── Cookie 头净化（安全关键）──────────────────────────────────────────
# 🔴 为什么必须有：HTTP 路径走 `requests`/urllib3，后者发现值里有 CRLF 会抛
#    `ValueError: Invalid header value` 拦下；但 **WebSocket 握手走
#    `websocket-client`，它对传入的 `header=[...]` 只做 `headers.extend()`，
#    完全不做 CRLF 校验**。
#    于是 cookie 名/值里塞 `\r\n` 就能在**上游 WS 请求**里注入任意头
#    （Host / X-Forwarded-For / 甚至 `\r\n\r\n` 拆分请求）。实测已复现。
#    本函数是唯一防线：名字必须匹配安全字符集，值禁止控制字符与分号。
_COOKIE_NAME_RE = re.compile(r"^[A-Za-z0-9_\-\.]{1,64}$")
_COOKIE_VAL_BAD = re.compile(r"[\x00-\x1f\x7f;]")


def _sanitize_cookies(cookies: dict) -> dict:
    """剔除非法的 cookie 名/值，返回可安全拼进请求头的副本。"""
    out: dict = {}
    for k, v in (cookies or {}).items():
        k, v = str(k), str(v)
        if not _COOKIE_NAME_RE.match(k):
            log.warning("丢弃非法 cookie 名：%r", k[:32])
            continue
        if _COOKIE_VAL_BAD.search(v):
            log.warning("丢弃含控制字符的 cookie：%s", k)
            continue
        out[k] = v
    return out


def _cookie_header(cookies: dict) -> str:
    """构造安全的 Cookie 头（供 WS 握手使用）。"""
    return "; ".join(f"{k}={v}" for k, v in _sanitize_cookies(cookies).items())


def _cookie_domain() -> str:
    """cookie 作用域跟随实际上游域名，不再硬编码 `.accio.com`。

    旧实现写死域名，导致 `ACCIO_BASE_URL` 一旦指向非 accio 域时，
    cookie 被 `requests` **静默丢弃**（配合裸 `except: pass` 完全无日志），
    表现为「凭证看着正常但一律 401」。
    """
    from urllib.parse import urlsplit
    host = urlsplit(settings.base_url).hostname or "www.accio.com"
    # 取可注册域（末两段），兼容 api.accio.com / www.accio.com 等子域
    parts = host.split(".")
    return "." + ".".join(parts[-2:]) if len(parts) >= 2 else host

import requests
import websocket

from ..core.config import settings
from ..core.proxy import pool as proxy_pool, proxies_for

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


# ── 事件分类 ────────────────────────────────────────────────
def classify_frame(raw: str) -> tuple[str, dict]:
    """把 WS 文本帧分类。返回 (kind, obj)。

    kind ∈ hello_ack | ack | delta | finalize | turn_start | turn_end
           | title | queue | err | other
    """
    try:
        j = json.loads(raw)
    except Exception:
        return "other", {"raw": raw[:400]}
    t = j.get("type", "")
    m = j.get("method", "")
    mapping = {
        "sendQuery.ack": "ack",
        "delta": "delta",
        "finalize": "finalize",
        "turn.start": "turn_start",
        "turn.end": "turn_end",
        "title": "title",
        "queue.state_changed": "queue",
    }
    if t == "hello_ack":
        return "hello_ack", j
    if m in mapping:
        return mapping[m], j
    if t == "err" or j.get("success") is False:
        return "err", j
    return "other", j


# 占位噪声：Accio 在真正回答前会推这些"思考中"文本，**不是正文**，必须丢
PLACEHOLDER_TEXTS = {"Thinking...", "Thinking…", "思考中...", "思考中…",
                     "Thinking", "思考中"}


def delta_to_openai_chunk(evt: dict, model: str, resp_id: str) -> dict | None:
    """`event/delta` → OpenAI `chat.completion.chunk`。"""
    p = evt.get("payload") or {}
    content = p.get("contentDelta") or ""
    reasoning = p.get("reasoningDelta") or ""
    if content.strip() in PLACEHOLDER_TEXTS:
        content = ""
    if not content and not reasoning:
        return None
    delta: dict = {}
    if reasoning:
        delta["reasoning_content"] = reasoning
    if content:
        delta["content"] = content
    return {
        "id": resp_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
    }


def finalize_to_usage(evt: dict) -> dict | None:
    """`event/finalize.payload.executionUsage` → OpenAI `usage`。"""
    u = (evt.get("payload") or {}).get("executionUsage") or {}
    if not u:
        return None
    return {
        "prompt_tokens": u.get("prompt_tokens", 0),
        "completion_tokens": u.get("completion_tokens", 0),
        "total_tokens": u.get("total_tokens", 0),
        "completion_tokens_details": {
            "reasoning_tokens": u.get("reasoning_tokens", 0)},
        "prompt_tokens_details": {
            "cached_tokens": u.get("cache_read_input_tokens", 0)},
    }


# ── 上游客户端 ──────────────────────────────────────────────
@dataclass
class AccioClient:
    """绑定到**一个凭证**的 Accio 客户端。"""
    user_id: str = ""
    accio_id: str = ""
    cookies: dict[str, str] = field(default_factory=dict)
    agent_id: str = ""
    project_path: str = ""
    account_key: str = ""        # 用于 sticky 代理绑定
    _device_fp: str = ""         # 设备指纹 uniqueId（延迟生成并持久化）
    _proxy: Any = None           # 本次使用的代理条目

    _session: requests.Session | None = None
    _ws_url: str = ""
    _ws_url_at: float = 0.0
    _capability: str = ""
    _lock: Any = None

    # ── HTTP 层 ──────────────────────────────────────
    def http(self) -> requests.Session:
        if self._session is None:
            s = requests.Session()
            s.trust_env = False
            # 🔴 代理：每个凭证按 sticky 策略绑定固定出口。
            #    同账号出口 IP 变化会触发 Accio 的会话异常风控 —— 见 core/proxy.py
            px, entry = proxies_for(self.account_key)
            if px:
                s.proxies.update(px)
                self._proxy = entry
            for k, v in _sanitize_cookies(self.cookies).items():
                try:
                    s.cookies.set(k, v, domain=_cookie_domain(), path="/")
                except Exception as e:
                    log.debug("cookie 写入失败 %r: %s", k, e)
            s.headers.update({
                "User-Agent": UA,
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                "Origin": settings.base_url,
                "Referer": f"{settings.base_url}/work/app",
                "Content-Type": "application/json",
            })
            self._session = s
        return self._session

    def _qs(self) -> str:
        return urlencode({"source": "ACCIO_WEB",
                          "version": settings.client_version})

    def device_id(self) -> str:
        """设备指纹 —— Accio 用它做风控与埋点归属。

        形如 `6Vk6I6kDMTkCAS9UesT6VeYc`（22 位）；同设备应保持一致，
        每次变更会被视为新设备。这里持久化在 data_dir。
        """
        if self._device_fp:
            return self._device_fp
        import secrets
        f = settings.data_dir / ".device_id"
        if f.exists():
            self._device_fp = f.read_text().strip()
        else:
            self._device_fp = secrets.token_urlsafe(16)[:22]
            f.write_text(self._device_fp)
        return self._device_fp

    # ── 1b. Agent 发现（建会话的前置依赖）─────────────
    def fetch_agents(self) -> list[dict]:
        """拉取账号下的 agent 列表。

        ⚠ **关键前置依赖**（2026-09-23 实证）：
          `POST /gateway/conversation` 要求 body 带 `agentId`，
          缺失时服务端返回 `400 {"ok":false,"error":"agentId is required"}`。

          而 agentId **无法凭空构造** —— 它是服务端下发的 DID 标识
          （形如 `DID-XXXXXXXX-XXXXXXXXXXXXXXXXXXXXXXXXXX`）。
          必须先经本接口取得，再传给 `create_conversation()`。

          此接口同时响应 `/gateway/agent`（单数），语义一致。
        """
        r = self.http().get(f"{settings.base_url}/gateway/agents?{self._qs()}",
                            timeout=30)
        r.raise_for_status()
        return (r.json() or {}).get("data") or []

    def resolve_agent_id(self, preferred: str = "") -> str:
        """确定要使用的 agentId。

        优先级：显式指定 > 配置项 > 列表首个。
        结果**缓存到实例**，避免每次对话都多一次请求。
        """
        if self.agent_id:
            return self.agent_id
        if preferred:
            self.agent_id = preferred
            return self.agent_id
        try:
            agents = self.fetch_agents()
            if agents:
                # 优先选名称含「助手」的默认 agent，否则取第一个
                pick = next((a for a in agents
                             if "助手" in (a.get("name") or "")), agents[0])
                self.agent_id = pick.get("id") or ""
        except Exception:
            pass
        if not self.agent_id:
            raise RuntimeError(
                "无法解析 agentId（/gateway/agents 未返回可用 agent）。"
                "建会话必填此字段，请检查凭证 cookie 是否完整。")
        return self.agent_id

    # ── 1. 模型清单（动态拉取）────────────────────────
    def fetch_models(self) -> list[dict]:
        r = self.http().get(f"{settings.base_url}/gateway/models?{self._qs()}",
                            timeout=30)
        r.raise_for_status()
        out = []
        for prov in (r.json().get("data") or []):
            for m in (prov.get("modelList") or []):
                out.append({
                    "id": m.get("modelCode"),
                    "name": m.get("modelDisplayName"),
                    "provider": prov.get("provider"),
                    "context_window": m.get("contextWindow"),
                    "multimodal": m.get("multimodal"),
                    "is_default": m.get("isDefault"),
                    "raw": m,
                })
        return out

    # ── 2. 账号信息 ──────────────────────────────────
    def fetch_userinfo(self) -> dict:
        r = self.http().post(f"{settings.base_url}/gateway/auth/userinfo",
                             json={"source": "ACCIO_WEB",
                                   "version": settings.client_version}, timeout=30)
        r.raise_for_status()
        return (r.json() or {}).get("data") or {}

    # ── 3. 建会话 ────────────────────────────────────
    def create_conversation(self, agent_id: str = "",
                            first_query: str = "") -> str:
        """创建会话，返回 conversationId。

        ⚠ 实测要点（2026-09）：
          * 会话 ID 在 `data.id`（形如 `CID-<数字>U<时间戳>-...`），
            **不是** `conversationId` —— 猜错会拿到空串，后续全链路静默失败。
          * 请求体需要 `uniqueId`（设备指纹）、`enableGenerativeUI`、
            `from: "desktop"`；缺了仍返回 201 但行为可能不同。
        """
        aid = agent_id or self.resolve_agent_id()    # 必填，缺失则 400
        r = self.http().post(
            f"{settings.base_url}/gateway/conversation",
            json={"source": "ACCIO_WEB", "version": settings.client_version,
                  "uniqueId": self.device_id(),
                  "path": self.project_path, "name": "New conversation",
                  "agentId": aid, "sessionModel": "auto",
                  "enableGenerativeUI": False,
                  "firstUserQuery": (first_query or "")[:200],
                  "from": "desktop"}, timeout=30)
        r.raise_for_status()
        d = r.json() or {}
        data = d.get("data") or {}
        return (data.get("id") or data.get("conversationId")
                or f"CID-{self.user_id}")

    # ── 4. WS capability ─────────────────────────────
    def fetch_capability(self) -> str:
        r = self.http().post(
            f"{settings.base_url}/gateway/host-im/websocket-capability",
            json={"source": "ACCIO_WEB", "version": settings.client_version},
            timeout=30)
        r.raise_for_status()
        self._capability = (r.json() or {}).get("capability", "")
        return self._capability

    # ── 5. 解析 WS 地址（动态）────────────────────────
    def resolve_ws_url(self) -> str:
        """动态解析 WS 地址。

        实测：前端在埋点上报里带上完整 wsUrl。生产实现不应依赖埋点，
        改用**引导接口 + 页面抓取**两条路：
          a) 若站点提供 gateway 侧的 ws 引导接口，直接取；
          b) 否则回退：无头浏览器打开 work/app，从网络请求里嗅出 ws 地址。
        b 路需要 BROWSER_ENABLED。
        """
        if self._ws_url and (time.time() - self._ws_url_at) < 600:
            return self._ws_url

        # (a) 尝试引导接口（不同版本字段可能不同，逐个试）
        for path, field in (
            ("/gateway/host-im/websocket-url", "url"),
            ("/gateway/chat/session/ws", "wsUrl"),
        ):
            try:
                r = self.http().post(f"{settings.base_url}{path}",
                                     json={"source": "ACCIO_WEB",
                                           "version": settings.client_version},
                                     timeout=15)
                if r.status_code == 200:
                    v = (r.json().get("data") or {}).get(field) or r.json().get(field)
                    if v and str(v).startswith("ws"):
                        self._ws_url, self._ws_url_at = str(v), time.time()
                        return self._ws_url
            except Exception:
                continue

        # (b) 浏览器嗅探（回退）
        if settings.browser_enabled:
            try:
                from ..auth.browser_sniff import sniff_ws_url
                u = sniff_ws_url(self.cookies)
                if u:
                    self._ws_url, self._ws_url_at = u, time.time()
                    return u
            except Exception:
                pass
        raise RuntimeError(
            "无法解析 Accio WebSocket 地址（引导接口与浏览器嗅探均失败）")

    # ── 6. 发问 + 收流 ───────────────────────────────
    def stream_query(self, query: str, *, model: str = "auto",
                     conversation_id: str = "",
                     language: str = "zh",
                     on_event: Callable[[str, dict], None] | None = None,
                     ) -> Iterator[tuple[str, dict]]:
        """发一条提问，逐帧 yield (kind, obj)。调用方负责组装 SSE。"""
        conv = conversation_id or self.create_conversation(
            first_query=query)
        cap = self.fetch_capability()
        ws_url = self.resolve_ws_url()
        if cap:
            sep = "&" if "?" in ws_url else "?"
            ws_url = f"{ws_url}{sep}capability={cap}"

        cookie_hdr = _cookie_header(self.cookies)
        # ⚠ WebSocket 必须走**同一个出口**，否则 HTTP 探测走 A、WS 连接走 B
        #   会被判定为「会话劫持」风控。
        px, _entry = proxies_for(self.account_key)
        ws_kwargs: dict = {}
        if px:
            # websocket-client 的 http_proxy_* 参数
            host = px["https"].split("://", 1)[-1]
            ws_kwargs = {
                "http_proxy_host": host.rsplit(":", 1)[0],
                "http_proxy_port": int(host.rsplit(":", 1)[1]),
                "proxy_type": "http",
            }
        ws = websocket.create_connection(
            ws_url, timeout=settings.upstream_timeout_s,
            header=[f"Cookie: {cookie_hdr}", f"User-Agent: {UA}",
                    f"Origin: {settings.base_url}"],
            sslopt={"cert_reqs": ssl.CERT_NONE},
            suppress_origin=True,
            **ws_kwargs,
        )
        try:
            ws.send(json.dumps({"type": "hello", "version": "1.0.0",
                                "clientId": "", "clientType": "web",
                                "capabilities": []}))
            frame = self._build_send_query(query, conv, model, language)
            ws.send(json.dumps(frame, ensure_ascii=False))
            t0 = time.time()
            while time.time() - t0 < settings.upstream_timeout_s:
                try:
                    raw = ws.recv()
                except Exception:
                    break
                if not raw:
                    continue
                kind, obj = classify_frame(raw)
                if on_event:
                    try:
                        on_event(kind, obj)
                    except Exception:
                        pass
                yield kind, obj
                if kind in ("turn_end", "err"):
                    break
        finally:
            try:
                ws.close()
            except Exception:
                pass

    def _build_send_query(self, query: str, conversation_id: str,
                          model: str, language: str) -> dict:
        rid = f"AI_AccioWork_{self.accio_id}_{int(time.time()*1000)}"
        return {
            "type": "req", "method": "sendQuery",
            "params": {
                "conversationId": conversation_id,
                "chatType": "direct",
                "uniqueId": f"msg-{int(time.time()*1000)}",
                "question": {"query": query},
                "path": self.project_path,
                "skills": [],
                "pluginBindingVersion": 2,
                "model": model,
                "selectModelName": "自动",
                "bypassSandbox": True,
                "turnModeOverride": "queue",
                "ingress": {
                    "type": "desktop", "platform": "pcApp",
                    "entry": "sendWsMessage", "requestId": rid,
                    "channel": "desktop", "chatType": "direct",
                    "role": "req", "messageType": "user",
                    "agentId": self.resolve_agent_id(), "model": model,
                    "clientId": f"msg-{int(time.time()*1000)}",
                },
                "source": "ACCIO_WEB", "atIds": [],
                "turnId": "turn1",
                "traceId": uuid.uuid4().hex,
                "requestId": rid, "messageId": rid,
                "spanId": uuid.uuid4().hex[:16],
                "targetAgentList": [{"agentId": self.resolve_agent_id(), "isTL": True}],
                "language": language,
            },
        }
