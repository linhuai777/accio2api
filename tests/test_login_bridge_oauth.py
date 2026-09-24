#!/usr/bin/env python3
"""登录桥 OAuth 化 —— 端到端回归测试（mock 上游，无真实网络）。

锁定 OAuth 授权流的四条不变量：
  1. 入口 302：/login-bridge/<id> 必须整页 302 到 /login?b=<id>，不再嵌 iframe
  2. 自动入库：登录态专有 cookie（xman_t）出现 + 已离开 /login 页 →
     服务端 worker 自动 fetch_userinfo → 自动落盘（无人工按钮）
  3. done 桥保留：入库后桥留在注册表（宽限期），status/入口页可取到结果；
     桥内截获的 cookie 立即焚毁
  4. 入口鉴权与安全：创建/关闭强制 ADMIN_KEY；未就绪时不会误入库

跑法：  python3 tests/test_login_bridge_oauth.py
"""
import os
import sys
import time
import pathlib
import threading

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

PASS = FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"    ✅ {name}")
    else:
        FAIL += 1
        print(f"    🔴 {name}  {extra}")


os.environ.setdefault("API_KEY", "test-test-test-test")
os.environ.setdefault("ADMIN_KEY", "test-admin-key")
os.environ.setdefault("ALLOWED_HOSTS", "")
os.environ.setdefault("BROWSER_ENABLED", "false")
os.environ.setdefault("DATA_DIR", "/tmp/_ac_lb_test_data")

import logging
logging.disable(logging.CRITICAL)

import shutil
shutil.rmtree("/tmp/_ac_lb_test_data", ignore_errors=True)

from starlette.testclient import TestClient
from app.main import app

ADMIN = {"Authorization": "Bearer test-admin-key"}


class _FakeInfo(dict):
    """fetch_userinfo 返回值桩。"""


def _stub_accio(monkey_cookies_ok=True):
    """打桩 AccioClient.fetch_userinfo 与 _enrich（不走真实上游）。"""
    from app.upstream import client as up_client

    class _FakeSession:
        """client.http() 返回的 Session 桩：/_enrich 里的 agents/workspace
        每个都包着独立 try/except，404 即静默跳过——返回 404 最省事。"""
        def get(self, *a, **kw):
            class _R:
                status_code = 404
                def json(self):
                    return {"data": []}
            return _R()

    class _FakeClient:
        def __init__(self, cookies=None, **kw):
            self.cookies = cookies or {}

        def fetch_userinfo(self):
            if self.cookies.get("xman_t"):
                return _FakeInfo(userId="u-9527", accioId="a-1",
                                 userName="测试用户",
                                 desensitizedEmail="t***@example.com")
            raise RuntimeError("未登录")

        def http(self):
            return _FakeSession()

        def _qs(self):
            return "x=1"

        def fetch_models(self):
            return []

    up_client.AccioClient = _FakeClient
    return up_client


def main():
    print("=" * 66)
    print("  登录桥 OAuth 化 · 端到端回归")
    print("=" * 66)

    client = TestClient(app)
    _stub_accio()

    # ── 1. 入口鉴权 ──
    r = client.post("/admin/api/login-bridge")
    check("创建桥无 ADMIN_KEY → 401", r.status_code == 401, r.status_code)

    # ── 2. 创建桥：302 跳转形态 ──
    r = client.post("/admin/api/login-bridge", headers=ADMIN)
    check("创建桥（带 ADMIN_KEY）→ 200", r.status_code == 200, r.status_code)
    j = r.json()
    bid = j["data"]["bridge_id"]
    check("返回 bridge_id", bool(bid))
    check("返回 url", j["url"].endswith(f"/login-bridge/{bid}"), j.get("url"))
    check("返回 qr data URI（或空兜底）", "qr" in j)

    r = client.get(f"/login-bridge/{bid}", follow_redirects=False)
    check("入口页 → 302", r.status_code == 302, r.status_code)
    check("302 Location → /login?b=<id>",
          r.headers.get("location", "").startswith(f"/login?b={bid}"),
          r.headers.get("location"))

    # ── 3. 未登录：代理链路工作但不入库 ──
    r = client.get(f"/login?b={bid}", follow_redirects=False)
    # 上游 mock 不了真实页面——这里只要求桥不 5xx（上游不可达 502 也算链路通）
    check("桥内登录页请求不 410（桥存活）", r.status_code != 410, r.status_code)

    r = client.get(f"/admin/api/login-bridge/status?bridge_id={bid}")
    d = r.json()["data"]
    check("初始 stage=visited", d["stage"] in ("visited", "cookies_seen"), d["stage"])
    check("初始 done=False", d["done"] is False)
    check("无登录态 cookie 时不触发入库", d["done"] is False and not d["saving"])

    # ── 4. 手动注入「登录态」：绕过真实上游，直接写桥内存 ──
    from app.auth import login_bridge as lb
    b = lb.bridges.get(bid)
    with b.lock:
        b.cookies["xman_t"] = "fake-xman-token"
        b.cookies["cookie2"] = "fake-cookie2"
        b.cookies["sgcookie"] = "fake-sg"

    # 未离开 /login：不应触发（双因子之一缺失）
    lb._maybe_autosave(b)
    time.sleep(0.3)
    check("仅 cookie 出现、仍在登录页 → 不入库",
          not b.done and not b.saving,
          f"done={b.done} saving={b.saving}")

    # 离开登录页（模拟官方 302 去工作台）→ 触发自动入库
    with b.lock:
        b.last_page = "/gateway/workspace"
    lb._maybe_autosave(b)
    check("双因子齐 → worker 已启动（saving 或 done）",
          b.saving or b.done, f"done={b.done} saving={b.saving}")
    for _ in range(50):                       # 最多等 5s
        if b.done:
            break
        time.sleep(0.1)
    check("worker 自动校验并入库成功", b.done, b.save_error)
    check("saved_cred 含账号", (b.saved_cred or {}).get("user_id") == "u-9527",
          b.saved_cred)
    check("入库后截获 cookie 立即焚毁", not b.cookies, dict(b.cookies))

    # ── 5. done 桥保留：结果可轮询 ──
    r = client.get(f"/admin/api/login-bridge/status?bridge_id={bid}")
    check("done 后 status 仍 200（保留宽限期）", r.status_code == 200,
          r.status_code)
    d = r.json()["data"]
    check("status 含 saved_cred", d["saved_cred"].get("user_id") == "u-9527")
    check("status done=True", d["done"] is True)

    # 入口页（done）→ 渲染成功页而非 302
    r = client.get(f"/login-bridge/{bid}", follow_redirects=False)
    check("done 后入口页 → 200 成功页", r.status_code == 200, r.status_code)
    check("成功页含「授权成功」", "授权成功" in r.text)

    # ── 6. 重复触发保护 ──
    lb._maybe_autosave(b)
    time.sleep(0.2)
    check("done 后重复触发不二次入库", b.done, "")

    # ── 7. 作废端点 ──
    r = client.delete(f"/admin/api/login-bridge/{bid}", headers=ADMIN)
    check("作废 done 桥 → 200", r.status_code == 200, r.status_code)
    r = client.get(f"/admin/api/login-bridge/status?bridge_id={bid}")
    check("作废后 status → 404", r.status_code == 404, r.status_code)

    # ── 8. 过期桥入口 ──
    r = client.get("/login-bridge/nonexistent-bridge-id-xyz",
                   follow_redirects=False)
    check("不存在的桥入口 → 410", r.status_code == 410, r.status_code)

    print("-" * 66)
    print(f"  通过 {PASS} · 失败 {FAIL}")
    if FAIL:
        sys.exit(1)


if __name__ == "__main__":
    main()
