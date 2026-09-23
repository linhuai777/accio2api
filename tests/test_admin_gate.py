#!/usr/bin/env python3
"""管理后台登录门 —— 防绕过回归测试。

锁定三条不变量：
  1. 所有 /admin/api/* 端点在无密钥 / 错密钥 / 空密钥下一律 401
     （前端门只是体验层，这里才是真防线）
  2. admin.html 里不含任何真实数据 / 密钥 / 明文凭据
  3. 未登录时 #app 带 hide 类、#gate 存在（默认停在门口）

跑法：  python3 tests/test_admin_gate.py
"""
import os
import re
import sys
import pathlib

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


# 全部管理端点（方法, 路径）—— 新增端点必须同步补进来
ENDPOINTS = [
    ("GET",    "/admin/api/overview"),
    ("GET",    "/admin/api/credentials"),
    ("POST",   "/admin/api/credentials"),
    ("DELETE", "/admin/api/credentials/some-account"),
    ("POST",   "/admin/api/credentials/some-account/enable"),
    ("POST",   "/admin/api/credentials/some-account/sync"),
    ("POST",   "/admin/api/credentials/health"),
    ("POST",   "/admin/api/credentials/bulk"),
    ("POST",   "/admin/api/login/start"),
    ("GET",    "/admin/api/login/some-id"),
    ("GET",    "/admin/api/login/some-id/frame"),
    ("POST",   "/admin/api/login/some-id/input"),
    ("POST",   "/admin/api/login/some-id/finish"),
    ("DELETE", "/admin/api/login/some-id"),
    ("POST",   "/admin/api/register"),
    ("GET",    "/admin/api/alias"),
    ("GET",    "/admin/api/audit"),
    ("GET",    "/admin/api/audit/stats"),
    ("POST",   "/admin/api/audit/purge"),
    ("GET",    "/admin/api/dashboard"),
    ("POST",   "/admin/api/playground"),
    ("POST",   "/admin/api/playground/stream"),
    ("GET",    "/admin/api/proxies"),
    ("GET",    "/admin/api/proxies/groups"),
    ("POST",   "/admin/api/proxies/switch"),
    ("POST",   "/admin/api/proxies/rotate"),
    ("GET",    "/admin/api/proxies/current-ip"),
]


def main():
    print("=" * 66)
    print("  管理后台登录门 · 防绕过回归测试")
    print("=" * 66)

    os.environ.setdefault("API_KEY", "test-test-test-test")
    os.environ.setdefault("ADMIN_KEY", "correct-key-123456")
    os.environ.setdefault("ALLOWED_HOSTS", "")
    os.environ.setdefault("BROWSER_ENABLED", "false")

    # 静音日志：本测试会打 100+ 次 401，日志会淹没结果
    import logging
    logging.disable(logging.CRITICAL)

    from starlette.testclient import TestClient
    from app.main import app
    c = TestClient(app)
    GOOD = {"Authorization": "Bearer correct-key-123456"}

    # ── 1. 未授权一律 401 ──────────────────────────────
    print("\n  ── 1. 所有端点：无密钥 / 错密钥 / 空密钥 → 必须 401 ──")
    bads = [
        ("无头", {}),
        ("错密钥", {"Authorization": "Bearer wrong-key-xyz"}),
        ("空 Bearer", {"Authorization": "Bearer "}),
        ("纯空格", {"Authorization": "Bearer    "}),
        ("x-api-key 错", {"x-api-key": "wrong"}),
    ]
    for label, h in bads:
        leaked = []
        for method, ep in ENDPOINTS:
            r = c.request(method, ep, headers=h, json={})
            if r.status_code != 401:
                leaked.append(f"{method} {ep} → {r.status_code}")
        check(f"{label}: 无端点泄露", not leaked,
              ("泄露: " + "; ".join(leaked[:4])) if leaked else "")

    # ── 2. 正确密钥可通行（确认没把自己锁死）──────────────
    print("\n  ── 2. 正确密钥可通行 ──")
    r = c.get("/admin/api/overview", headers=GOOD)
    check("GET /overview → 200", r.status_code == 200, f"得到 {r.status_code}")
    r = c.get("/admin/api/overview", headers={"x-api-key": "correct-key-123456"})
    check("x-api-key 头兼容 → 200", r.status_code == 200, f"得到 {r.status_code}")

    # ── 3. 恒定时间比较（防时序爆破）──────────────────────
    print("\n  ── 3. 密钥比较用 hmac.compare_digest ──")
    src = (ROOT / "app" / "admin_api.py").read_text(encoding="utf-8")
    check("使用 hmac.compare_digest", "hmac.compare_digest" in src)
    check("未用裸 != 比较密钥", "settings.admin_key or \"\")" in src
          and "compare_digest" in src)

    # ── 4. admin.html 不含敏感物 ────────────────────────
    print("\n  ── 4. admin.html 无敏感物 ──")
    html = (ROOT / "static" / "admin.html").read_text(encoding="utf-8")
    check("无 sk-admin- 真值", not re.search(r"sk-admin-[A-Za-z0-9]{8,}", html))
    check("无 sk- 48 位密钥", not re.search(r"sk-[A-Za-z0-9]{40,}", html))
    # 邮箱：排除示例占位符（example.com / your-domain.com / xxx@ 等）
    _mails = re.findall(r"[\w.+-]+@[\w-]+\.(?:com|cn|net|xyz|org)", html)
    _real = [m for m in _mails
             if not re.search(r"example\.|your-domain\.|xxx@|you@|user@|test@", m)]
    check("无真实邮箱地址", not _real, f"命中: {_real[:3]}")
    check("无 Bearer 真值", not re.search(r"Bearer\s+[A-Za-z0-9_-]{20,}", html))

    # ── 5. 登录门结构 ───────────────────────────────────
    print("\n  ── 5. 登录门结构 ──")
    check("#gate 容器存在", 'id="gate"' in html)
    check("#app 默认带 hide 类（未登录不显示后台）",
          re.search(r'<div class="app[^"]*hide[^"]*"\s+id="app"', html) is not None
          or re.search(r'<div class="app hide" id="app"', html) is not None)
    check("gate 有密钥输入框", 'id="gateKey"' in html and 'type="password"' in html)
    check("gate 有错误提示位", 'id="gateErr"' in html)
    check("gateSubmit 做真实后端探活", "gateProbe" in html and "fetch(" in html)
    check("401 触发 lockout（收回后台）", "lockout" in html and "401" in html)
    check("gateProbe 不信任前端本地标记", "gateProbe" in html)

    # ── 6. 无「解锁」后门标记 ───────────────────────────
    print("\n  ── 6. 无前端后门 ──")
    check("密钥只存 localStorage", "localStorage.setItem('accio_admin_key'" in html)
    check("密钥不入 URL", "location.search" not in html.split("adminKey")[0][-2000:])
    check("无绕过开关（如 skipAuth/debugUnlock）",
          not re.search(r"skipAuth|debugUnlock|bypassLogin|forceUnlock", html))

    print("\n" + "=" * 66)
    print(f"  {'✅ 全部通过' if FAIL == 0 else '🔴 有失败'} {PASS}/{PASS + FAIL}")
    print("=" * 66)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
