"""速率限制 —— 按客户端标识做滑动窗口。

为什么要自己写而不上 slowapi：
    1. 本服务是**单进程自托管网关**，不需要跨进程共享计数（用了也多此一举）；
    2. 少一个依赖 = 少一条供应链攻击面（本项目的安全定位是"越少的依赖越好"）；
    3. 需求很窄 —— 只需要「N 秒内最多 M 次」，二十行搞定。

三条限流线（各自独立计数）：
    · inference   —— `/v1/*`   推理接口，按 **API key** 计数（多租户场景下
                     一个客户端打爆不该影响别人）
    · admin       —— `/admin/api/*` 管理接口，按 **客户端 IP** 计数
                     （暴力猜管理密钥的成本直接抬到不可行）
    · admin_login —— `/admin/api/login/start` 会拉起真实浏览器，单列一条
                     更严的线，防"开浏览器 DoS"

设计取舍：
    * **内存计数**：进程重启即清零。这是**刻意**的 —— 自托管场景下不接受
      为限流引入 Redis；重启清零不会造成安全缺口（攻击者无法触发重启）。
    * **默认值宽松**：限流是防「脚本失控/暴力枚举」，不是防正常用户。
      正常聊天远达不到 120 次/分。
    * **可完全关闭**：`RATE_LIMIT_ENABLED=false` 一行关闭，便于内网/调试。
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections import defaultdict, deque

from .config import settings

log = logging.getLogger("accio2api.ratelimit")


class SlidingWindow:
    """滑动窗口计数器 —— 比固定窗口平滑，不会在窗口边界双倍放量。

    内存占用：每个 key 只存「窗口内的命中时间戳」，上限 = limit 个 float。
    超过 limit 的旧戳会被挤出，所以**不会无限增长**（无内存泄漏风险）。
    """

    __slots__ = ("limit", "window", "_hits", "_lk")

    def __init__(self, limit: int, window: float):
        self.limit = max(0, int(limit))
        self.window = float(window)
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        # 中间件经 asyncio.to_thread 调度，可能**多线程并发**进入 check()。
        # 读-改-写（清旧戳 → 判长 → append）非原子，无锁会少量放行超额
        # 请求（限额被突破）。这里用轻量互斥锁兜住。
        self._lk = threading.Lock()

    def check(self, key: str) -> tuple[bool, float]:
        """返回 (是否放行, 需等待秒数)。

        `limit <= 0` 表示不限流 —— 与项目其它配置项（`0 = 不限制`）语义一致。
        """
        if self.limit <= 0:
            return True, 0.0
        now = time.monotonic()
        cut = now - self.window
        with self._lk:
            q = self._hits[key]
            while q and q[0] <= cut:      # 挤掉窗口外的旧时间戳
                q.popleft()
            if len(q) >= self.limit:
                # 最早一次命中滑出窗口后即可重试
                return False, max(0.0, q[0] + self.window - now)
            q.append(now)
            return True, 0.0

    def prune(self, max_keys: int = 50_000) -> None:
        """兜底清理：防止伪造大量不同 key（如随机 IP）把字典撑爆。

        正常情况不会触发 —— 只有 key 数量异常多时才做一次全量清扫。
        """
        with self._lk:
            if len(self._hits) <= max_keys:
                return
            now = time.monotonic()
            cut = now - self.window
            for k in list(self._hits.keys()):
                q = self._hits[k]
                while q and q[0] <= cut:
                    q.popleft()
                if not q:
                    del self._hits[k]


class RateLimiter:
    """三条限流线的容器。"""

    def __init__(self, *, enabled: bool,
                 inference_rpm: int, admin_rpm: int, login_per_5min: int):
        self.enabled = enabled
        self.inference = SlidingWindow(inference_rpm, 60.0)
        self.admin = SlidingWindow(admin_rpm, 60.0)
        self.login = SlidingWindow(login_per_5min, 300.0)
        self._ops = 0

    # ── 三个查询入口 ────────────────────────────────────────────

    def allow_inference(self, ident: str) -> tuple[bool, float]:
        if not self.enabled:
            return True, 0.0
        return self._guarded(self.inference, ident)

    def allow_admin(self, ident: str) -> tuple[bool, float]:
        if not self.enabled:
            return True, 0.0
        return self._guarded(self.admin, ident)

    def allow_login(self, ident: str) -> tuple[bool, float]:
        if not self.enabled:
            return True, 0.0
        return self._guarded(self.login, ident)

    def _guarded(self, w: SlidingWindow, ident: str) -> tuple[bool, float]:
        ok, wait = w.check(ident)
        self._ops += 1
        if self._ops % 1000 == 0:      # 每千次操作做一次兜底清理
            w.prune()
        return ok, wait


# ── 路由分类 ────────────────────────────────────────────────────

_ADMIN_LOGIN_PATH = "/admin/api/login/start"


def classify(path: str) -> str | None:
    """判断路径属于哪条限流线；返回 None 表示不限流。"""
    if path == _ADMIN_LOGIN_PATH:
        return "login"
    if path.startswith("/v1/"):
        return "inference"
    if path.startswith("/admin/api/"):
        return "admin"
    return None


def _peer_ip(request) -> str:
    """socket 对端地址（不可被客户端伪造）。"""
    try:
        return request.client.host if request.client else "unknown"
    except Exception:
        return "unknown"


def _in_cidr(ip: str, spec: str) -> bool:
    """ip 是否落在 spec（单 IP 或 CIDR）内。解析失败一律 False。"""
    spec = spec.strip()
    if not spec:
        return False
    try:
        import ipaddress
        a = ipaddress.ip_address(ip)
        if "/" in spec:
            return a in ipaddress.ip_network(spec, strict=False)
        return a == ipaddress.ip_address(spec)
    except Exception:
        return spec == ip  # 退化：字面比较（如 "::1" 解析失败时）


def client_ip(request) -> str:
    """取客户端 IP（用于管理接口限流）。

    ⚠️ 安全：X-Forwarded-For / X-Real-IP **完全由客户端控制**，无条件采信
    会让攻击者每次伪造一个新值 → 每个请求落在独立计数桶 → 限流形同虚设
    （管理密钥可被无限次爆破）。

    因此：**仅当**直连的 socket 对端落在 `TRUSTED_PROXIES`（可信反代）内，
    才采信转发头；否则一律使用 socket 对端地址。`TRUSTED_PROXIES` 留空
    （默认）= 不信任任何转发头。
    """
    peer = _peer_ip(request)
    trusted = (getattr(settings, "trusted_proxies", "") or "")
    if not trusted:
        return peer
    if not any(_in_cidr(peer, s) for s in trusted.split(",")):
        return peer                      # 直连来源不可信 → 忽略转发头
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[0].strip()[:64]
    real = request.headers.get("x-real-ip", "")
    if real:
        return real.strip()[:64]
    return peer


async def middleware(request, call_next):
    """ASGI 中间件主逻辑（由 main.py 挂载）。

    429 响应遵循 OpenAI 错误格式 + `Retry-After` 头 —— 客户端
    （如 New API / Cherry Studio）拿到后能自动退避重试。
    """
    from fastapi.responses import JSONResponse

    # 防御：若应用未完成启动（lifespan 未挂载 limiter），
    # 不应让**每个**请求都 500 —— 降级为「不限制」并告警。
    limiter: RateLimiter | None = getattr(request.app.state, "ratelimiter", None)
    if limiter is None:
        log.warning("限流器未初始化（app.state.ratelimiter 缺失），本请求不限流")
        return await call_next(request)
    kind = classify(request.url.path)
    if kind is None:
        return await call_next(request)

    if kind == "inference":
        # 按 API key 计数（未带 key 时退化为 IP —— 无鉴权部署也不至于裸奔）
        auth = request.headers.get("authorization", "")
        ident = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        if not ident:
            ident = "ip:" + client_ip(request)
        else:
            # 不把密钥原文当字典键（防内存转储泄露），用短哈希
            import hashlib
            ident = "k:" + hashlib.sha256(ident.encode()).hexdigest()[:16]
    elif kind == "admin":
        ident = client_ip(request)
    else:  # login
        ident = client_ip(request)

    fn = {"inference": limiter.allow_inference,
          "admin": limiter.allow_admin,
          "login": limiter.allow_login}[kind]
    allowed, wait = await asyncio.to_thread(fn, ident)

    if not allowed:
        log.warning("限流命中 kind=%s ident=%s path=%s",
                    kind, ident[:24], request.url.path)
        return JSONResponse(
            {"error": {"message": "rate limit exceeded, slow down",
                       "type": "rate_limit_error",
                       "code": "rate_limit_exceeded"}},
            status_code=429,
            headers={"Retry-After": str(max(1, int(wait + 0.999)))})

    return await call_next(request)
