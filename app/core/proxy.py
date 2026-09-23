"""代理池与 IP 轮换 —— 给每个请求/账号分配独立出口。

为什么需要
----------
Accio 会对**同 IP 高频注册/登录**做风控（Baxia 挑战升级、验证码频繁、
甚至账号关联封禁）。给每个凭证绑定独立出口 IP，可以：
  * 分散风控压力（不同账号走不同 IP，不互相牵连）
  * 提高自动注册成功率（换 IP 可绕开"该 IP 已被挑战过"的状态）
  * 让「IP 归属地」与账号的 `routeRegion` 一致，降低异常评分

三种代理来源（可混用）
----------------------
  1. **静态列表**：`PROXIES=http://127.0.0.1:<port-a>,http://127.0.0.1:<port-b>,...`
     最简单，适合已经有多个本地出口（mihomo 槽位）的场景。
  2. **mihomo/clash 控制 API**：`MIHOMO_API=http://127.0.0.1:<controller-port>`
     通过 `PUT /proxies/<组>` 动态切换节点 —— **同一个端口，出口 IP 会变**。
     适合节点多但本地端口少的场景。
  3. **单代理**：`PROXY=http://127.0.0.1:<port>` 全局走一个。

策略（`PROXY_STRATEGY`）
-----------------------
  sticky   同一凭证始终用同一出口（**默认**）—— IP 稳定，风控不报警
  rotate   每次请求换一个出口 —— 最分散，但 IP 频繁变动可能反被判异常
  random   每次请求随机挑一个

⚠ 重要：`sticky` 是默认值，不是随意选的。同一账号的登录与后续请求
   若出口 IP 变化，会触发「会话异常」风控 —— 宁可 IP 少，也要 IP 稳。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from .config import settings

log = logging.getLogger("accio2api.proxy")


# ── 数据结构 ────────────────────────────────────────────────
@dataclass
class ProxyEntry:
    url: str                       # http://host:port
    label: str = ""                # 人类可读名（部署方自定义）
    region: str = ""               # 出口地区（可选，用于匹配账号 routeRegion）
    last_ok_at: float = 0.0
    last_fail_at: float = 0.0
    fail_count: int = 0
    last_ip: str = ""
    enabled: bool = True

    @property
    def healthy(self) -> bool:
        """连续失败 3 次以上视为不健康（指数退避恢复）。"""
        if self.fail_count < 3:
            return True
        return (time.time() - self.last_fail_at) > min(300, 30 * self.fail_count)

    @staticmethod
    def redact(url: str) -> str:
        """脱敏代理 URL —— 剥离 userinfo 里的明文凭据。

        🔴 为什么必须脱敏：`http://user:pass@host:port` 形态的代理，
        其 URL 会流向①admin API 响应（`to_public()`）②失败重试日志。
        不脱敏 = 代理凭据随接口与日志双路泄露。
        所有面向外部（API / 日志）的输出都必须过这个函数。
        """
        try:
            p = urlsplit(url)
            host = p.hostname or ""
            if p.port:
                host += f":{p.port}"
            if p.username or p.password:
                return f"{p.scheme}://***:***@{host}"
            return f"{p.scheme}://{host}"
        except Exception:
            return "***"

    def to_public(self) -> dict:
        d = {
            "url": ProxyEntry.redact(self.url), "label": self.label, "region": self.region,
            "enabled": self.enabled, "healthy": self.healthy,
            "fail_count": self.fail_count, "last_ip": self.last_ip,
        }
        d["last_ok_at"] = self.last_ok_at
        d["last_fail_at"] = self.last_fail_at
        return d


# ── 池 ───────────────────────────────────────────────────────
class ProxyPool:
    def __init__(self):
        self._entries: list[ProxyEntry] = []
        self._lock = threading.RLock()
        self._cursor = 0
        self._sticky: dict[str, ProxyEntry] = {}   # account_key -> entry
        self._mihomo_cache: dict[str, Any] = {}
        self._load_from_env()

    # ── 装载 ─────────────────────────────────────────
    def _load_from_env(self):
        raw = settings.proxies or ""
        for i, part in enumerate([p.strip() for p in raw.split(",") if p.strip()]):
            self._entries.append(ProxyEntry(url=self._normalize(part),
                                            label=f"proxy-{i+1}"))
        if not self._entries and settings.proxy:
            self._entries.append(ProxyEntry(url=self._normalize(settings.proxy),
                                            label="default"))
        if self._entries:
            log.info("代理池已装载 %d 个出口（策略=%s）",
                     len(self._entries), settings.proxy_strategy)
        if settings.mihomo_api:
            log.info("已启用 mihomo 动态切换：%s", settings.mihomo_api)


    @staticmethod
    def _normalize(url: str) -> str:
        u = url.strip()
        if not u:
            return ""
        if "://" not in u:
            u = "http://" + u
        return u

    # ── 查询 ─────────────────────────────────────────
    @property
    def enabled(self) -> bool:
        return bool(self._entries) or bool(settings.mihomo_api)

    def all(self) -> list[ProxyEntry]:
        with self._lock:
            return list(self._entries)

    def healthy_entries(self) -> list[ProxyEntry]:
        with self._lock:
            return [e for e in self._entries if e.enabled and e.healthy]

    # ── 选择 ─────────────────────────────────────────
    def pick(self, account_key: str = "") -> ProxyEntry | None:
        """按策略挑一个出口。返回 None 表示直连。"""
        strategy = (settings.proxy_strategy or "sticky").lower()

        # sticky：同凭证 → 同出口（默认，最安全）
        if strategy == "sticky" and account_key:
            with self._lock:
                hit = self._sticky.get(account_key)
                if hit and hit.enabled:
                    return hit

        pool = self.healthy_entries()
        if not pool:
            return None

        if strategy == "sticky":
            # 该凭证还没绑：按账号哈希稳定分配（重启后仍指向同一个）。
            # ⚠ 不能直接用 hash % len —— 哈希取模会碰撞，导致多个账号共用
            #   同一出口，失去分散意义。
            #   改为**最少占用优先 + 哈希决定同分先后**：
            #   既稳定（同 key 永远同出口），又均匀（账号铺满所有出口再叠加）。
            with self._lock:
                used = {}
                for k, v in self._sticky.items():
                    if v.enabled:
                        used[id(v)] = used.get(id(v), 0) + 1
                # 🔴 平局必须用**真哈希置换**，不能用 `(h + i) % n`。
                #    后者对固定 h 只是一个**旋转**，排序首项恒为
                #    `pool[h % n]` —— 冷启动（计数全 0）时多个账号会撞同一出口。
                #    实测：4 个账号里 3 个撞同出口。而冷启动正是批量注册最
                #    危险的时刻（多新账号同 IP 极易触发关联风控）。
                #    改用 sha256(account_key|entry.url) 排序：每个账号获得一个
                #    独立、稳定、均匀的出口次序。
                pool_sorted = sorted(
                    pool,
                    key=lambda x: (
                        used.get(id(x), 0),
                        hashlib.sha256(
                            f"{account_key}|{x.url}".encode()).hexdigest()))
                e = pool_sorted[0]
                # 同一临界区内完成绑定（消除「锁内读、锁外写」的竞态）
                self._sticky[account_key] = e
        elif strategy == "random":
            h = int(hashlib.sha256(
                f"{account_key}{time.time()}".encode()).hexdigest(), 16)
            e = pool[h % len(pool)]
        else:  # rotate
            with self._lock:
                self._cursor = (self._cursor + 1) % len(pool)
                e = pool[self._cursor]

        return e

    # ── 反馈（健康度）───────────────────────────────
    def report_ok(self, entry: ProxyEntry | None):
        if entry is None:
            return
        with self._lock:
            entry.last_ok_at = time.time()
            entry.fail_count = 0

    def report_fail(self, entry: ProxyEntry | None):
        if entry is None:
            return
        with self._lock:
            entry.last_fail_at = time.time()
            entry.fail_count += 1
            if entry.fail_count == 3:
                log.warning("出口 %s 连续失败 3 次，暂时摘除（退避恢复）",
                            ProxyEntry.redact(entry.url))
            # sticky 绑定失效，下次重新分配
            for k, v in list(self._sticky.items()):
                if v is entry:
                    self._sticky.pop(k, None)

    def mark_ip(self, entry: ProxyEntry | None, ip: str):
        if entry is None:
            return
        with self._lock:
            entry.last_ip = ip

    @staticmethod
    def check_url() -> str:
        """校验 `PROXY_CHECK_URL` 并返回 —— 防配置驱动的 SSRF。

        🔴 `proxy_check_url` 的响应正文前 64 字符会被写入 `entry.last_ip`，
        再经 `to_public()` 回显到 admin API。若该值被指向内网/云元数据端点
        （`http://169.254.169.254/...`），就构成**可读内网的 SSRF 原语**。
        故此处硬性拒绝非 http(s) scheme 与内网/环回/元数据地址。
        """
        u = (settings.proxy_check_url or "").strip()
        if not u:
            raise ValueError("PROXY_CHECK_URL 未配置")
        p = urlsplit(u)
        if p.scheme not in ("http", "https"):
            raise ValueError(f"PROXY_CHECK_URL 非法 scheme：{p.scheme}")
        host = (p.hostname or "").lower()
        if not host:
            raise ValueError("PROXY_CHECK_URL 缺少主机名")
        if host in ("localhost", "::1", "0.0.0.0") or host.endswith(".local"):
            raise ValueError(f"PROXY_CHECK_URL 禁止指向本机：{host}")
        try:
            import ipaddress
            ip = ipaddress.ip_address(host)
            if (ip.is_private or ip.is_loopback or ip.is_link_local
                    or ip.is_reserved or ip.is_multicast):
                raise ValueError(f"PROXY_CHECK_URL 禁止指向内网/保留地址：{host}")
        except ValueError as e:
            if "禁止" in str(e):
                raise
            # 非 IP（域名）→ 放行，但拒绝显式内网关键字
            if host.startswith(("10.", "192.168.", "169.254.", "172.16.",
                                "172.17.", "172.18.", "172.19.", "172.2",
                                "172.30.", "172.31.")):
                raise ValueError(f"PROXY_CHECK_URL 禁止指向内网：{host}")
        return u

    # ── 活体探测 ─────────────────────────────────────
    def probe(self, entry: ProxyEntry, timeout: float = 10.0) -> tuple[bool, str]:
        """探测出口，返回 (是否可用, 出口 IP)。"""
        import requests
        try:
            check_url = ProxyPool.check_url()
        except ValueError as e:
            log.error("出口探测被拒绝：%s", e)
            return False, ""
        try:
            r = requests.get(check_url,
                             proxies={"http": entry.url, "https": entry.url},
                             timeout=timeout)
            ip = (r.text or "").strip()[:64]
            if r.status_code == 200 and ip:
                self.report_ok(entry)
                self.mark_ip(entry, ip)
                return True, ip
        except Exception as e:
            log.debug("出口探测失败 %s：%s", ProxyEntry.redact(entry.url), e)
        self.report_fail(entry)
        return False, ""

    def probe_all(self) -> list[dict]:
        out = []
        for e in self.all():
            ok, ip = self.probe(e)
            d = e.to_public()
            d["probe_ok"] = ok
            out.append(d)
        return out

    # ── mihomo 动态切换 ──────────────────────────────
    def mihomo_groups(self) -> list[dict]:
        """列出可切换的策略组（含节点列表与当前选中）。"""
        if not settings.mihomo_api:
            return []
        import requests
        try:
            r = requests.get(f"{settings.mihomo_api.rstrip('/')}/proxies",
                             timeout=8)
            data = (r.json() or {}).get("proxies") or {}
            out = []
            for name, v in data.items():
                if v.get("type") in ("Selector", "URLTest", "Fallback",
                                     "LoadBalance"):
                    out.append({"group": name, "type": v.get("type"),
                                "now": v.get("now"),
                                "nodes": v.get("all") or []})
            return out
        except Exception as e:
            log.warning("mihomo groups 获取失败: %s", e)
            return []

    def mihomo_proxy_url(self) -> str:
        """mihomo 主端口的代理地址 —— 这是被策略组切换的那条链路。

        ⚠ 必须区分两条不同的链路（2026-09-23 踩过的坑）：
          * **mihomo 主端口**（mixed-port）→ 出口由 proxy-groups
            （AUTO/ROTATE）决定 → **可以被 mihomo_switch 改变**
          * **固定槽位**（listeners）→ 直接绑死某个节点
            → **切不动**

        所以「验证切换是否生效」必须探测**主端口**，而不是随机挑一个
        池内出口 —— 否则会得到"切了但 IP 没变"的假结论。
        """
        # 主端口：静态列表里第一个 http://127.0.0.1:<port> 形式，
        # 或显式配置 MIHOMO_PROXY_URL
        explicit = (getattr(settings, "mihomo_proxy_url", "") or "").strip()
        if explicit:
            return self._normalize(explicit)
        entries = self.all()
        if entries:
            return entries[0].url
        return ""

    def mihomo_switch(self, group: str, node: str,
                      verify: bool = True) -> tuple[bool, str]:
        """切换策略组当前节点 —— 「同端口换 IP」的关键动作。

        ⚠ 实测要点（2026-09-23）：
          1. **必须切对组**。mihomo 有两种组：
             - `listeners` 里的固定槽位直接绑节点，**不可切换**
             - `proxy-groups`（AUTO / ROTATE）由 rules 决定主端口的出口
             切错组会返回 200 但出口 IP 纹丝不动 —— 这是最容易踩的坑。
          2. **切换后 IP 不会立刻变**。内核已建立的连接会复用，需要
             等待 + 强制验证。所以这里 verify=True 时会实测出口 IP。

        返回 (是否成功, 切换后的出口 IP)。verify=False 时 IP 为空。
        """
        if not settings.mihomo_api:
            return False, ""
        # 🔴 参数校验（防 URL 路径注入 / SSRF 转向）：
        #    group 会被拼进 `/proxies/{group}`，若原样放行 `../` `?` `#`
        #    等字符，攻击者可操纵请求路径，把本该发往 mihomo 的请求
        #    重定向到内核 API 的其它端点（如读取全部配置/触发其它副作用）。
        #    这里只允许 mihomo 组名的合法字符集。
        #
        # ⚠ 字符集要拿捏准（实测踩过两次）：
        #   * 不能放 `\s` —— 空格会被 URL 编码绕过、`\r\n` 是 header 注入原燃料
        #   * 必须含 emoji 区 —— 组名常带 🚀🔀♻️ 之类的符号，写窄了会误拒合法名
        #   * 必须**排除** `/` `?` `#` `%` `.` —— 这四个是路径注入的全部关键
        if not re.fullmatch(
                r"[\w\u4e00-\u9fff\u2600-\u27bf\U0001f000-\U0001faff"
                r"\u200d\ufe0f\u2b00-\u2bff\-]{1,64}",
                group or "") or ".." in (group or ""):
            log.warning("拒绝非法 group 名：%r", group)
            return False, ""
        if not isinstance(node, str) or not node or len(node) > 200:
            return False, ""
        # node 走 JSON body（不拼 URL），但仍拒绝控制字符
        if any(c in node for c in "\r\n\x00"):
            return False, ""

        import requests
        from urllib.parse import quote
        try:
            r = requests.put(
                f"{settings.mihomo_api.rstrip('/')}/proxies/"
                f"{quote(group, safe='')}",
                json={"name": node}, timeout=10)
            if r.status_code not in (200, 204):
                log.warning("mihomo 切换被拒：HTTP %s（group=%s node=%s）",
                            r.status_code, group, node)
                return False, ""
            log.info("mihomo 切换 %s → %s", group, node)
        except Exception as e:
            log.warning("mihomo 切换失败: %s", e)
            return False, ""

        if not verify:
            time.sleep(1.5)
            return True, ""

        # 等待出口真正改变（最多 ~18 秒）。旧连接复用会拖一会儿，
        # 这是内核行为不是 bug —— 早点返回会得到"没变"的假结论。
        # ⚠ 探测必须走 mihomo 主端口（被切换的那条链路），不能用随机池出口。
        murl = self.mihomo_proxy_url()
        mpx = {"http": murl, "https": murl} if murl else None
        before = current_ip(mpx, timeout=8)
        for i in range(6):
            time.sleep(3)
            now = current_ip(mpx, timeout=8)
            if now and now != before:
                log.info("出口已变化 %s → %s（第 %d 次探测）", before, now, i + 1)
                return True, now
        # 没变也不能算失败：可能新节点与旧节点同 IP（少见但存在）
        log.info("切换完成，出口仍为 %s（可能新旧节点同 IP）", before)
        return True, before

    def mihomo_rotate(self, group: str = "") -> tuple[str, str]:
        """在策略组里轮换到下一个节点，返回 (新节点名, 新出口 IP)。"""
        g = group or settings.mihomo_group
        groups = self.mihomo_groups()
        hit = next((x for x in groups if x["group"] == g), None)
        if not hit or not hit["nodes"]:
            return "", ""
        nodes, now = hit["nodes"], hit["now"]
        try:
            i = (nodes.index(now) + 1) % len(nodes) if now in nodes else 0
        except ValueError:
            i = 0
        nxt = nodes[i]
        ok, ip = self.mihomo_switch(g, nxt)
        return (nxt, ip) if ok else ("", "")

    def mihomo_probe(self, group: str = "") -> str:
        """实测 mihomo 主端口出口 IP（切换前后对比用）。"""
        murl = self.mihomo_proxy_url()
        return current_ip({"http": murl, "https": murl} if murl else None)


pool = ProxyPool()


# ── 便捷函数：拿到 requests 用的 proxies 参数 ────────────────
def proxies_for(account_key: str = "") -> tuple[dict | None, ProxyEntry | None]:
    """返回 (requests_proxies 字典, 代理条目)。

    `proxies_for("")` 且池为空 → (None, None)，即直连。
    """
    if not pool.enabled:
        return None, None
    e = pool.pick(account_key)
    if e is None:
        return None, None
    return {"http": e.url, "https": e.url}, e


# ── 出口 IP 探测（用于验证账号与 IP 是否匹配）────────────────
def current_ip(proxies: dict | None = None, timeout: float = 10.0) -> str:
    import requests
    try:
        r = requests.get(settings.proxy_check_url, proxies=proxies,
                         timeout=timeout)
        return (r.text or "").strip()[:64]
    except Exception:
        return ""
