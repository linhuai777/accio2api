#!/usr/bin/env python3
"""密钥管理逻辑回归测试。

锁定 2026-09-23 的设计变更：
  · ADMIN_KEY 必填，不再自动生成到 data/.admin_key
  · 没设 ADMIN_KEY → 拒绝启动，且错误信息含生成命令
  · 旧部署遗留的 data/.admin_key 仍能读取（向后兼容），但打警告
  · 显式设置时绝不偷偷写文件
  · SECRET_KEY 仍自动生成（它只在本机加解密用，不需要人记）

跑法：  python3 tests/test_secret_management.py
"""
import os
import subprocess
import sys
import tempfile
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent

PASS = FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"    ✅ {name}")
    else:
        FAIL += 1
        print(f"    🔴 {name}  {extra}")


def boot(env_extra: dict, data_dir: str):
    """在干净子进程里加载 config，返回 (成功?, admin_key, stderr)。"""
    code = (
        "import sys; sys.path.insert(0, %r)\n"
        "from app.core.config import settings\n"
        "print('KEY=' + repr(settings.admin_key))\n" % str(ROOT)
    )
    env = {k: v for k, v in os.environ.items()
           if k not in ("ADMIN_KEY", "SECRET_KEY", "DATA_DIR", "PYTHONPATH")}
    env["DATA_DIR"] = data_dir
    env.update(env_extra)
    p = subprocess.run([sys.executable, "-c", code], capture_output=True,
                       text=True, env=env, cwd=str(ROOT), timeout=60)
    key = ""
    for line in p.stdout.splitlines():
        if line.startswith("KEY="):
            key = line[4:]
    return p.returncode == 0, key, (p.stderr or "") + (p.stdout or "")


def main():
    print("=" * 66)
    print("  密钥管理回归测试")
    print("=" * 66)

    # ── 1. 没设 ADMIN_KEY → 拒绝启动 ──────────────────────
    print("\n  ── 1. ADMIN_KEY 必填 ──")
    d1 = tempfile.mkdtemp(prefix="accio-sec1-")
    ok, key, err = boot({"SECRET_KEY": "x" * 40}, d1)
    check("无 ADMIN_KEY 时拒绝启动", not ok, "竟然启动了")
    check("错误信息提到 ADMIN_KEY", "ADMIN_KEY" in err)
    check("错误信息给出生成命令", "token_urlsafe" in err or "secrets" in err)
    check("未在 data/ 下生成 .admin_key",
          not (pathlib.Path(d1) / ".admin_key").exists())

    # ── 2. 显式设置 → 正常启动且不写文件 ──────────────────
    print("\n  ── 2. 显式 ADMIN_KEY ──")
    d2 = tempfile.mkdtemp(prefix="accio-sec2-")
    ok, key, err = boot({"ADMIN_KEY": "sk-admin-explicit-value-123",
                         "SECRET_KEY": "y" * 40}, d2)
    check("正常启动", ok, err[-200:])
    check("用的是设置的值", "sk-admin-explicit-value-123" in key, key[:50])
    check("未偷偷生成 data/.admin_key",
          not (pathlib.Path(d2) / ".admin_key").exists())

    # ── 3. 旧部署遗留文件 → 向后兼容 + 警告 ────────────────
    print("\n  ── 3. 向后兼容（老部署）──")
    d3 = pathlib.Path(tempfile.mkdtemp(prefix="accio-sec3-"))
    (d3 / ".admin_key").write_text("sk-admin-legacy-value-xyz", encoding="utf-8")
    os.chmod(d3 / ".admin_key", 0o600)
    ok, key, err = boot({"SECRET_KEY": "z" * 40}, str(d3))
    check("仍能启动（兼容老部署）", ok, err[-200:])
    check("读到遗留文件里的值", "legacy-value-xyz" in key, key[:60])
    check("打出迁移警告", "遗留" in err or "WARNING" in err)

    # ── 4. SECRET_KEY 仍自动生成 ──────────────────────────
    print("\n  ── 4. SECRET_KEY 自动生成（保持不变）──")
    d4 = pathlib.Path(tempfile.mkdtemp(prefix="accio-sec4-"))
    ok, key, err = boot({"ADMIN_KEY": "sk-admin-sample-123456"}, str(d4))
    check("无 SECRET_KEY 也能启动", ok, err[-200:])
    sk = d4 / ".secret_key"
    check("自动生成 data/.secret_key", sk.exists())
    if sk.exists():
        check("长度 ≥32", len(sk.read_text().strip()) >= 32)

    # ── 5. 弱 SECRET_KEY 仍被拒 ───────────────────────────
    print("\n  ── 5. 弱 SECRET_KEY 拒绝 ──")
    d5 = tempfile.mkdtemp(prefix="accio-sec5-")
    ok, key, err = boot({"ADMIN_KEY": "sk-admin-sample-123456",
                         "SECRET_KEY": "changeme"}, d5)
    check("短 SECRET_KEY 被拒", not ok)
    check("错误信息说明原因", "太短" in err or "32" in err)

    # ── 6. 模板与文档一致性 ───────────────────────────────
    print("\n  ── 6. 模板 / 文档不再承诺自动生成 ADMIN_KEY ──")
    ex = (ROOT / ".env.example").read_text(encoding="utf-8")
    check(".env.example 有 ADMIN_KEY 条目", "ADMIN_KEY" in ex)
    # 只检查 ADMIN_KEY 自己那段，别误伤 SECRET_KEY 的「仍自动生成」说明
    _ak_zone = ex
    if "ADMIN_KEY" in ex:
        _start = ex.index("# ADMIN_KEY") if "# ADMIN_KEY" in ex else ex.index("ADMIN_KEY")
        _ak_zone = ex[_start: ex.index("\n\n", _start) if "\n\n" in ex[_start:] else len(ex)]
    check(".env.example 的 ADMIN_KEY 段不再说自动生成",
          "自动生成" not in _ak_zone or "不再自动生成" in _ak_zone,
          f"该段: {_ak_zone[:120]}")
    check(".env.example 提示 ADMIN_KEY 必填", "必填" in ex)

    dc = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    check("docker-compose 强制 ADMIN_KEY（:? 语法）", "${ADMIN_KEY:?" in dc)

    rm = (ROOT / "README.md").read_text(encoding="utf-8")
    check("README 不再指向 data/.admin_key",
          "用 `data/.admin_key` 里的密钥" not in rm)

    # ── 7. 向导三选一逻辑 ─────────────────────────────────
    print("\n  ── 7. 向导三态 ──")
    sp = (ROOT / "setup.py").read_text(encoding="utf-8")
    check("选项1 不设置", '("none"' in sp)
    check("选项2 生成并显示", '("gen"' in sp)
    check("选项3 占位符待改", '("later"' in sp)
    check("用 api_key_mode 区分空值的两种含义", "api_key_mode" in sp)
    check("ADMIN_KEY 在向导里强制输入/生成（无「留空=自动生成」）",
          "留空 = 首次启动自动生成到 data/.admin_key" not in sp)

    print("\n" + "=" * 66)
    print(f"  {'✅ 全部通过' if FAIL == 0 else '🔴 有失败'} {PASS}/{PASS + FAIL}")
    print("=" * 66)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
