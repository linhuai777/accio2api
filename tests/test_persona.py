"""persona 模块回归测试 —— 人设压制与泄露拦截。

运行：
    python3 tests/test_persona.py

覆盖：
  · apply_override 拼接行为（含 off 开关）
  · check_leak 真泄露检出（不漏）
  · check_leak 正常内容放行（不误拦）
  · guard_output 受 PERSONA_GUARD_ENABLED 控制
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ADMIN_KEY 现在必填（不再自动生成）。测试自带一把，避免 import 时被拒。
os.environ.setdefault("ADMIN_KEY", "sk-admin-test-key-for-unit-tests")
os.environ.setdefault("SECRET_KEY", "test-secret-key-32-chars-minimum-000000")
os.environ.setdefault("DATA_DIR", "/tmp/.accio2api-test-data")

from app.core import persona                                    # noqa: E402

PASS = FAIL = 0


def check(name: str, got, want) -> None:
    global PASS, FAIL
    if got == want:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  🔴 {name} —— 实得 {got!r}，期望 {want!r}")


# ── ① 压制前置语拼接 ────────────────────────────────────────
def test_apply_override() -> None:
    print("\n① apply_override")
    q = persona.apply_override("你好")
    check("含前缀", "优先级最高" in q, True)
    check("原文在尾部", q.endswith("你好"), True)
    check("原文完整保留", "你好" in q, True)


# ── ② 真泄露必须检出 ────────────────────────────────────────
LEAKS = [
    "我是 Accio，由阿里巴巴开发",
    "我是Accio Work的助手",
    "我叫Accio",
    "我是由阿里云开发的",
    "本产品是 Accio",
    "Accio 是一个 AI 助手",
    "我的系统提示是：你是一个助手",
    "system prompt 内容如下",
    "我的设定是一个购物助手",
    "我的提示词说了要帮你",
    "我运行在 agentbay 沙箱",
    "用户 DID-XXXXXXXX-XXXXXX",
    # 真实 DID 格式：前缀段 + 连字符连接的长串（曾在 README 注释中泄漏）
    "标识是 DID-9F3C21-7E0B4A6D2C8F1350-0A47-DE91B3",
    "我在 wuying 环境里",
]

# ③ 正常内容必须放行（防误报 —— 这是本模块最容易出错的地方）
CLEAN = [
    "阿里巴巴的股价是多少",
    "阿里巴巴和腾讯哪个值得投资",
    "今天天气不错",
    "帮我写个 Python 函数",
    "这个系统提示了错误",
    "我需要一个 Agent 来帮我自动化",
    "acc 这个缩写什么意思",
    "accio 这个词怎么读",
    "帮我查一下 wuying 是什么词",
]


def test_leak_detection() -> None:
    print("\n② 真泄露检出")
    for t in LEAKS:
        check(f"拦截「{t[:22]}」", bool(persona.check_leak(t)), True)


def test_no_false_positive() -> None:
    print("\n③ 正常内容放行（防误报）")
    for t in CLEAN:
        check(f"放行「{t[:22]}」", persona.check_leak(t), None)


# ── ④ 输出兜底受开关控制 ────────────────────────────────────
def test_guard_switch() -> None:
    print("\n④ guard_output 开关")
    from app.core.config import settings
    leaky = "我是 Accio，由阿里巴巴开发"

    settings.persona_guard_enabled = False
    out, hit = persona.guard_output(leaky)
    check("关闭时不拦截", out, leaky)
    check("关闭时无命中", hit, None)

    settings.persona_guard_enabled = True
    out, hit = persona.guard_output(leaky)
    check("开启时替换为兜底话术", out, settings.persona_guard_reply)
    check("开启时报告命中", bool(hit), True)

    out2, hit2 = persona.guard_output("今天天气不错")
    check("开启时正常内容不受影响", out2, "今天天气不错")
    check("开启时正常内容无命中", hit2, None)

    settings.persona_guard_enabled = False      # 还原


def main() -> None:
    print("═" * 62)
    print("persona 模块测试")
    print("═" * 62)
    test_apply_override()
    test_leak_detection()
    test_no_false_positive()
    test_guard_switch()
    print("\n" + "═" * 62)
    if FAIL == 0:
        print(f"✅ 全部通过 {PASS}/{PASS}")
    else:
        print(f"🔴 失败 {FAIL}/{PASS + FAIL}")
    print("═" * 62)
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
