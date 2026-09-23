"""WS 地址嗅探的**独立进程** worker。

为什么要单独一个进程：Playwright 的 sync API 与 asyncio 事件循环互斥，
在 FastAPI 的同步工作线程里直接调用会失败。用子进程彻底隔离。

协议（stdin/stdout JSON）：
    stdin : {"cookies": {...}, "timeout_s": 60, "base_url": "https://..."}
    stdout: 最后一行 "WSURL=<url>"（成功）
"""

import json
import sys
import time


def main():
    try:
        req = json.loads(sys.stdin.read() or "{}")
    except Exception:
        print("WSURL=")
        return

    cookies = req.get("cookies") or {}
    timeout_s = int(req.get("timeout_s") or 60)
    base = req.get("base_url") or "https://www.accio.com"
    # 沙箱域名提示由**父进程**通过 stdin 传入（子进程不读 .env，
    # 保证「配置只有一处真相」）。空则退化为 None —— 此时仅靠路径判据，
    # 依然能工作（`/websocket/connect` 是主判据）。
    hints = [h.lower() for h in (req.get("host_hints") or []) if h]

    found: list[str] = []

    def _is_sandbox_ws(u: str) -> bool:
        if "websocket/connect" in u:
            return True
        return any(h in u.lower() for h in hints)

    def on_ws(ws):
        if _is_sandbox_ws(ws.url) and ws.url not in found:
            found.append(ws.url)

    def on_req(r):
        u = r.url
        if (u.startswith("wss://") or u.startswith("ws://")) \
                and _is_sandbox_ws(u) and u not in found:
            found.append(u)

    try:
        from playwright.sync_api import sync_playwright

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
            page.on("websocket", on_ws)
            page.on("request", on_req)
            try:
                page.goto(f"{base}/work/app", wait_until="domcontentloaded",
                          timeout=45000)
                t0 = time.time()
                while time.time() - t0 < timeout_s and not found:
                    page.wait_for_timeout(1200)
            except Exception:
                pass
            finally:
                try:
                    browser.close()
                except Exception:
                    pass
    except Exception:
        pass

    print("WSURL=" + (found[0].split("?")[0] if found else ""))


if __name__ == "__main__":
    main()
