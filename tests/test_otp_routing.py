"""OTP 归属判定回归测试（收码不串台）。

运行：`python3 tests/test_otp_routing.py`（零依赖，无需真实邮箱）

为什么单独测这一层：
    `otp._belongs()` 是「一封邮件到底归哪个账号」的唯一裁定点。
    它一旦判错，症状是**静默的串台** —— 注册流程全部成功，但账号
    A 的凭证里存的是账号 B 的验证码。没有报错、没有异常日志，
    只有事后「为什么我登进去是别人的号」才会暴露。

    这类 bug 无法靠端到端测试发现（需要真实多账号并发），
    只能在单元层用构造的邮件头钉死。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.auth import otp  # noqa: E402
from app.auth.alias import MailIdentity  # noqa: E402

_PASS = 0
_FAIL: list[str] = []


def check(desc: str, got, exp) -> None:
    global _PASS
    if got == exp:
        _PASS += 1
        print(f"  ✅ {desc}")
    else:
        _FAIL.append(desc)
        print(f"  🔴 {desc} —— 实得 {got!r}，期望 {exp!r}")


def test_belongs_with_alias() -> None:
    print("\n① 有别名身份时的归属判定")
    ident = MailIdentity("yourname@gmail.com")
    me = "yourname+accio01@gmail.com"

    check("To 里是我的别名",
          otp._belongs(me, ["yourname+accio01@gmail.com"], ident), True)
    check("To 带显示名",
          otp._belongs(me, ['"Accio" <yourname+accio01@gmail.com>'], ident), True)
    check("To 是我的别名 Cc 是别人",
          otp._belongs(me, ["yourname+accio01@gmail.com", "x@y.com"], ident), True)
    check("兄弟别名（必须拒）",
          otp._belongs(me, ["yourname+accio02@gmail.com"], ident), False)
    check("主地址兜底（默认关 → 拒）",
          otp._belongs(me, ["yourname@gmail.com"], ident), False)
    ident_on = MailIdentity("yourname@gmail.com", accept_primary_fallback=True)
    check("主地址兜底（显式开 → 认）",
          otp._belongs(me, ["yourname@gmail.com"], ident_on), True)
    check("Delivered-To 保留原始别名",
          otp._belongs(me, ["yourname@gmail.com", "yourname+accio01@gmail.com"], ident), True)
    check("完全无关",
          otp._belongs(me, ["someone@else.com"], ident), False)
    check("空收件人头",
          otp._belongs(me, [], ident), False)


def test_belongs_without_alias() -> None:
    print("\n② 无别名身份时的降级判定（向后兼容）")
    me = "box@mydomain.com"
    check("精确匹配", otp._belongs(me, ["box@mydomain.com"], None), True)
    check("大小写不敏感", otp._belongs(me, ["BOX@MyDomain.com"], None), True)
    check("不相关", otp._belongs(me, ["other@x.com"], None), False)

    # 空 target = 未指定目标 → 接受任意（单邮箱模式的旧行为）
    check("空 target 接受任意", otp._belongs("", ["any@x.com"], None), True)


def test_domain_mode_routing() -> None:
    print("\n③ domain 模式（CloudMail 泛域名收信）")
    ident = MailIdentity("box@mydomain.com", scheme="domain", domain="mail.mydomain.com")
    check("我的别名",
          otp._belongs("accio01@mail.mydomain.com",
                       ["accio01@mail.mydomain.com"], ident), True)
    check("兄弟别名（必须拒）",
          otp._belongs("accio01@mail.mydomain.com",
                       ["accio02@mail.mydomain.com"], ident), False)
    check("第三方域名（必须拒）",
          otp._belongs("accio01@mail.mydomain.com",
                       ["accio01@other.com"], ident), False)


def test_parallel_accounts_isolation() -> None:
    """核心安全属性：N 个并行账号，每封邮件只归属唯一一个。"""
    print("\n④ 并行账号隔离（串台检测）")
    ident = MailIdentity("yourname@gmail.com")
    accounts = [f"yourname+acc{i:02d}@gmail.com" for i in range(5)]

    # 构造 5 封邮件，每封投递给一个账号
    mails = [(a, [a]) for a in accounts]

    collisions = []
    for target, headers in mails:
        hits = [a for a in accounts if otp._belongs(a, headers, ident)]
        if hits != [target]:
            collisions.append((target, hits))
    check("每封邮件恰好归属 1 个账号", collisions, [])

    # 反向：每个账号只认自己的那封
    wrong = []
    for target, headers in mails:
        if not otp._belongs(target, headers, ident):
            wrong.append(target)
    check("每个账号都能认领自己的邮件", wrong, [])


def test_code_extraction() -> None:
    print("\n⑤ 验证码提取")
    check("主题取 6 位码", otp.CODE_RE.search("Your Accio code is 483920").group(1), "483920")
    check("中文主题", otp.CODE_RE.search("【Accio】验证码 991122 请在 10 分钟内使用").group(1), "991122")
    check("无码返回 None", otp.CODE_RE.search("Welcome to Accio"), None)
    # 边界（重要）：必须靠词边界防止从长数字串里截出错误码。
    # `\b` 保证 12345678 / 12345 / 1234567 都不会被当成 6 位验证码 ——
    # 否则会返回一个**错误的验证码**，导致登录失败且难以定位。
    check("8 位数字不误取（词边界）", otp.CODE_RE.search("order 12345678"), None)
    check("5 位数字不取", otp.CODE_RE.search("code 12345"), None)
    check("7 位数字不取", otp.CODE_RE.search("code 1234567"), None)
    check("字母数字混合边界", otp.CODE_RE.search("id A123456B"), None)


def test_env_config_path() -> None:
    print("\n⑥ 环境变量配置路径")
    import os
    os.environ["IMAP_HOST"] = "imap.gmail.com"
    os.environ["IMAP_PORT"] = "993"
    os.environ["IMAP_USER"] = "yourname@gmail.com"
    os.environ["IMAP_PASSWORD"] = "app-password-here"
    b = otp.ImapBackend()
    check("IMAP_HOST 读取", b.host, "imap.gmail.com")
    check("IMAP_USER 读取", b.user, "yourname@gmail.com")
    check("IMAP_PORT 读取", b.port, 993)
    check("缺 host 时不炸（返回 None）",
          otp.ImapBackend().__class__.__name__, "ImapBackend")


def main() -> int:
    print("═" * 64)
    print("OTP 归属判定回归测试")
    print("═" * 64)
    test_belongs_with_alias()
    test_belongs_without_alias()
    test_domain_mode_routing()
    test_parallel_accounts_isolation()
    test_code_extraction()
    test_env_config_path()

    print("\n" + "═" * 64)
    total = _PASS + len(_FAIL)
    if _FAIL:
        print(f"🔴 失败 {len(_FAIL)}/{total}")
        for f in _FAIL:
            print(f"   · {f}")
        return 1
    print(f"✅ 全部通过 {_PASS}/{total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
