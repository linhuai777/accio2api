"""浏览器嗅探 WS 地址 —— `resolve_ws_url()` 的兜底实现。

为什么需要兜底：
    Accio 的 WebSocket 地址形如
      `wss://<实例ID>.agentbay.<上游域>/websocket/connect`
     （域名由上游 `/gateway/domain-config` 下发，不可写死）
    其中 `<实例ID>` 是**该账号云 Agent 沙箱的实例标识**，
    每个账号不同、可能随沙箱重建变化 —— **绝不能写死**。

    站点没有稳定的公开引导接口时，最可靠的办法就是：
    **用该凭证的 cookie 打开 work/app，从网络请求里嗅出 ws 地址**，
    然后缓存复用（沙箱不常变）。

缓存策略：进程内按 account_key 缓存，TTL 10 分钟；连接失败会主动清缓存重嗅。
"""

from __future__ import annotations

import threading
import time

from ..core.config import settings

def _host_hints() -> list[str]:
    """从配置读沙箱域名提示（逗号分隔，小写）。

    单独抽成函数是为了**嗅探 worker 子进程也能复用**同一套配置语义。
    """
    raw = getattr(settings, "sandbox_host_hints", "") or ""
    return [h.strip().lower() for h in raw.split(",") if h.strip()]


# account_key -> (ws_url, ts)
_CACHE: dict[str, tuple[str, float]] = {}
_LOCK = threading.RLock()
_TTL = 600


def _cache_get(account_key: str) -> str:
    with _LOCK:
        v = _CACHE.get(account_key)
        if v and time.time() - v[1] < _TTL:
            return v[0]
    return ""


def _cache_put(account_key: str, url: str):
    with _LOCK:
        _CACHE[account_key] = (url, time.time())


def _cache_clear(account_key: str = ""):
    with _LOCK:
        if account_key:
            _CACHE.pop(account_key, None)
        else:
            _CACHE.clear()


def sniff_ws_url(cookies: dict, *, account_key: str = "",
                 timeout_s: int = 60) -> str:
    """打开 work/app，抓取 websocket 连接的真实 URL。

    🔴 重要：Playwright 的**同步 API 只能在其创建的线程里使用**。
    本函数被 FastAPI 的同步端点调用时运行在 anyio 工作线程里 ——
    直接 `sync_playwright()` 会抛 "It looks like you are using Playwright
    Sync API inside the asyncio loop"。因此这里**必须起独立子进程**。

    Args:
        cookies: 该凭证的 cookie 字典
        account_key: 用于缓存；为空则用 cookies 的哈希
        timeout_s: 最多等待多少秒
    """
    import hashlib

    key = account_key or hashlib.sha256(
        ",".join(sorted(cookies)).encode()).hexdigest()[:16]
    cached = _cache_get(key)
    if cached:
        return cached

    url = _sniff_in_subprocess(cookies, timeout_s)
    if url:
        _cache_put(key, url)
    return url


def _sniff_in_subprocess(cookies: dict, timeout_s: int) -> str:
    """在独立进程里执行嗅探，规避 Playwright 的线程约束。"""
    import json
    import os
    import subprocess
    import sys

    worker = os.path.join(os.path.dirname(__file__), "_sniff_worker.py")
    payload = json.dumps({"cookies": cookies, "timeout_s": timeout_s,
                          "base_url": settings.base_url,
                          # 沙箱域名提示随 payload 下发 —— 子进程不读 .env，
                          # 保证配置只有一处真相（见 core/config.py）
                          "host_hints": _host_hints()})
    try:
        proc = subprocess.run(
            [sys.executable, worker], input=payload, capture_output=True,
            text=True, timeout=timeout_s + 60)
        out = (proc.stdout or "").strip().splitlines()
        for line in reversed(out):
            if line.startswith("WSURL="):
                return line[len("WSURL="):].strip()
    except Exception:
        pass
    return ""


def _sniff_inline(cookies: dict, *, timeout_s: int = 60) -> str:
    """进程内嗅探（仅在确认当前线程可安全使用 Playwright 时调用）。"""
    from playwright.sync_api import sync_playwright

    found: list[str] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=[
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox", "--disable-dev-shm-usage"])
        ctx = browser.new_context(
            viewport={"width": 1440, "height": 900}, locale="zh-CN")
        for k, v in cookies.items():
            try:
                ctx.add_cookies([{"name": k, "value": v,
                                  "domain": ".accio.com", "path": "/"}])
            except Exception:
                pass
        page = ctx.new_page()

        # 判据：路径 `/websocket/connect` 是**主判据**；域名提示是辅助，
        # 从 settings.sandbox_host_hints 读（上游换域名时改 .env 即可）。
        _hints = _host_hints()

        def _is_sandbox_ws(u: str) -> bool:
            if "websocket/connect" in u:
                return True
            return any(h in u for h in _hints)

        def on_ws(ws):
            if _is_sandbox_ws(ws.url):
                found.append(ws.url)

        def on_req(req):
            u = req.url
            if (u.startswith("wss://") or u.startswith("ws://")) \
                    and _is_sandbox_ws(u):
                if u not in found:
                    found.append(u)

        page.on("websocket", on_ws)
        page.on("request", on_req)

        try:
            page.goto(f"{settings.base_url}/work/app",
                      wait_until="domcontentloaded", timeout=45000)
            t0 = time.time()
            while time.time() - t0 < timeout_s and not found:
                page.wait_for_timeout(1500)
        except Exception:
            pass
        finally:
            try:
                browser.close()
            except Exception:
                pass

    if found:
        return found[0].split("?")[0]
    return ""


def invalidate(cookies: dict, account_key: str = ""):
    """连接失败时调用，清缓存以便下次重新嗅探。"""
    import hashlib
    key = account_key or hashlib.sha256(
        ",".join(sorted(cookies)).encode()).hexdigest()[:16]
    _cache_clear(key)
