"""邮箱别名引擎回归测试。

运行：`python3 tests/test_alias.py`（无需 pytest，零依赖）

覆盖重点：
  · 机制自动判定（Gmail/Outlook 系 → plus；未知域名 → 拒绝猜测）
  · 地址派生（plus / domain 两种形态）
  · **收码归属判定**（本模块最关键的不变量 —— 绝不能串台）
  · 主地址兜底 / Gmail 点号折算 / 大小写 / 显示名包裹
  · 非法输入拒绝（标签字符集、缺域名、非法主地址）

为什么「防串台」要单独测：
    批量注册时多个账号共用一个物理收件箱。若归属判定有漏洞
    （最典型的是「标签作为子串匹配」—— 标签 `a` 会命中
    `yourname+xyzabc@gmail.com`），账号 A 就会拿到账号 B 的验证码，
    现象是「注册成功但登录进的是另一个账号」，且不报错，极难排查。
    这类 bug 只能在单元测试里钉死。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.auth.alias import (  # noqa: E402
    AliasError, MailIdentity, detect_scheme, make_tag,
)

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


def expect_raise(desc: str, fn) -> None:
    global _PASS
    try:
        fn()
    except AliasError:
        _PASS += 1
        print(f"  ✅ {desc}")
    except Exception as e:  # noqa: BLE001
        _FAIL.append(desc)
        print(f"  🔴 {desc} —— 抛了非 AliasError：{type(e).__name__}: {e}")
    else:
        _FAIL.append(desc)
        print(f"  🔴 {desc} —— 未抛异常")


# ── ① 机制判定 ───────────────────────────────────────────────────

def test_detect_scheme() -> None:
    print("\n① 别名机制自动判定")
    for addr in ("yourname@gmail.com", "x@googlemail.com", "a@outlook.com",
                 "b@hotmail.com", "c@live.com", "d@icloud.com",
                 "e@proton.me", "f@yahoo.com", "g@qq.com", "h@163.com"):
        check(f"{addr} → plus", detect_scheme(addr), "plus")
    # 未知域名必须返回 unknown，绝不猜测
    for addr in ("x@mycompany.com", "y@selfhosted.io"):
        check(f"{addr} → unknown（不猜测）", detect_scheme(addr), "unknown")
    expect_raise("无 @ 的非法地址", lambda: detect_scheme("notanemail"))


# ── ② 派生 ───────────────────────────────────────────────────────

def test_derivation() -> None:
    print("\n② 别名派生")
    g = MailIdentity("yourname@gmail.com")
    check("plus 指定标签", g.alias("accio01"), "yourname+accio01@gmail.com")
    check("plus 标签强制小写", g.alias("ACCIO01"), "yourname+accio01@gmail.com")
    check("plus 随机标签长度", len(g.alias().split("+")[1].split("@")[0]), 6)

    # 按账号派生必须可复现（同 key 同地址）
    a1 = g.alias_for_account("cred-abc")
    a2 = g.alias_for_account("cred-abc")
    check("按账号派生可复现", a1, a2)
    check("不同账号派生不同", g.alias_for_account("cred-abc") != g.alias_for_account("cred-xyz"), True)

    d = MailIdentity("box@mydomain.com", scheme="domain", domain="mail.mydomain.com")
    check("domain 派生", d.alias("accio01"), "accio01@mail.mydomain.com")

    expect_raise("标签含非法字符", lambda: g.alias("bad tag!"))
    expect_raise("标签含大写以外的符号", lambda: g.alias("a+b"))
    expect_raise("domain 模式缺域名", lambda: MailIdentity("x@self.com", scheme="domain"))
    expect_raise("未知机制名", lambda: MailIdentity("a@b.com", scheme="weird"))
    expect_raise("非法主地址", lambda: MailIdentity("noatsign"))

    print("\n②b 标签生成")
    check("make_tag 固定长度", len(make_tag(length=8)), 8)
    check("make_tag 可复现", make_tag("seed", length=6), make_tag("seed", length=6))
    check("make_tag 种子不同→结果不同", make_tag("a", length=6) != make_tag("b", length=6), True)
    check("make_tag 字符集安全", all(c in "abcdefghijklmnopqrstuvwxyz0123456789" for c in make_tag(length=32)), True)


# ── ③ 归属判定（防串台，最关键）───────────────────────────────────

def test_matching_plus() -> None:
    print("\n③ plus 模式归属判定（防串台）")
    g = MailIdentity("yourname@gmail.com")
    me = "yourname+accio01@gmail.com"

    check("精确匹配", g.matches(me, me), True)
    check("大小写不敏感", g.matches(me, "Yourname+Accio01@Gmail.com"), True)
    check("带显示名包裹", g.matches(me, '"林淮" <yourname+accio01@gmail.com>'), True)

    # 以下为「不能误判」的用例 —— 每一条都对应一类串台风险
    check("兄弟标签", g.matches(me, "yourname+other@gmail.com"), False)
    check("单字符标签是长标签的子串", g.matches("yourname+a@gmail.com", "yourname+xyzabc@gmail.com"), False)
    check("前缀包含 b vs ab", g.matches("yourname+b@gmail.com", "yourname+ab@gmail.com"), False)
    check("无标签主地址（兜底关时）", MailIdentity("yourname@gmail.com",
          accept_primary_fallback=False).matches(me, "yourname@gmail.com"), False)
    check("他人域名", g.matches(me, "yourname+accio01@other.com"), False)
    check("完全无关地址", g.matches(me, "someone@else.com"), False)


def test_matching_domain() -> None:
    print("\n③b domain 模式归属判定")
    d = MailIdentity("box@mydomain.com", scheme="domain", domain="mail.mydomain.com")
    me = "accio01@mail.mydomain.com"

    check("精确匹配", d.matches(me, me), True)
    check("兄弟本地名", d.matches(me, "accio02@mail.mydomain.com"), False)
    check("前缀包含 accio01 vs accio011", d.matches(me, "accio011@mail.mydomain.com"), False)
    check("第三方域名", d.matches(me, "accio01@other.com"), False)
    check("主地址兜底（默认关 → 拒）", d.matches(me, "box@mydomain.com"), False)
    d_on = MailIdentity("box@mydomain.com", scheme="domain", domain="mail.mydomain.com",
                        accept_primary_fallback=True)
    check("主地址兜底（显式开 → 认）", d_on.matches(me, "box@mydomain.com"), True)


def test_matching_fallback() -> None:
    print("\n③c 主地址兜底（上游改写 To 头）")
    g_on = MailIdentity("yourname@gmail.com", accept_primary_fallback=True)
    g_off = MailIdentity("yourname@gmail.com", accept_primary_fallback=False)
    me = "yourname+accio01@gmail.com"

    check("兜底开：To=主地址", g_on.matches(me, "yourname@gmail.com"), True)
    check("兜底开：To=点号主地址", g_on.matches(me, "your.name@gmail.com"), True)
    check("兜底开：To=googlemail 等价", g_on.matches(me, "yourname@googlemail.com"), True)
    check("兜底关：To=主地址", g_off.matches(me, "yourname@gmail.com"), False)
    # 兜底开启时，仍不能把「别的别名」当成自己的
    check("兜底开：兄弟标签仍拒", g_on.matches(me, "yourname+other@gmail.com"), False)


def test_gmail_dot_folding() -> None:
    print("\n③d Gmail 点号折算")
    g = MailIdentity("your.name@gmail.com")
    me = g.alias("t1")
    check("点号主地址派生正确", me, "your.name+t1@gmail.com")
    check("无点号形式等价匹配", g.matches(me, "yourname+t1@gmail.com"), True)
    # 🔴 安全默认：不显式开启兜底时，主地址邮件**不得**被认领 ——
    #    防止「同一个 IP 下多个账号共享主地址」时互相偷码。
    check("点号主地址兜底（默认关 → 拒）", g.matches(me, "yourname@gmail.com"), False)
    g_on = MailIdentity("your.name@gmail.com", accept_primary_fallback=True)
    check("点号主地址兜底（显式开 → 认）", g_on.matches(me, "yourname@gmail.com"), True)

    # QQ / 163 等（严格语义）：即便开启兜底，主地址也不跨点号认领
    q_on = MailIdentity("first.last@qq.com", accept_primary_fallback=True)
    check("QQ 主地址兜底不跨点号（严格，即使开启）",
          q_on.matches("first.last+t@qq.com", "firstlast@qq.com"), False)

    # Gmail / Outlook 系：点号在本地部分**不区分**（first.last == firstlast）
    # —— 这是两大主流服务商的既定行为，不是 bug；把这条钉死防回归。
    o = MailIdentity("first.last@outlook.com")
    o_me = "first.last+t@outlook.com"
    check("Outlook 点号折算（与 Gmail 同为宽松语义）",
          o.matches(o_me, "firstlast+t@outlook.com"), True)
    check("Outlook 点号折算不串台",
          o.matches(o_me, "firstlast+other@outlook.com"), False)

    # QQ / 163 等：点号是有效字符，本地名不折算（严格）。
    # 注意区分「标签」与「本地名」：
    #   · 标签相同（first.last+t@ vs firstlast+t@）→ 同一别名，应当匹配
    #   · 本地名不同且标签不同 → 不同账号，必须拒绝
    q = MailIdentity("first.last@qq.com")
    check("QQ 标签等价仍匹配（标签一致）",
          q.matches("first.last+t@qq.com", "firstlast+t@qq.com"), True)
    check("QQ 标签不同必拒（严格语义）",
          q.matches("first.last+t@qq.com", "firstlast+u@qq.com"), False)
    check("QQ 主地址兜底不跨点号（严格）",
          q.matches("first.last+t@qq.com", "firstlast@qq.com"), False)


def test_malformed_input() -> None:
    print("\n③e 异常输入")
    g = MailIdentity("yourname@gmail.com")
    me = "yourname+a@gmail.com"
    check("空 header", g.matches(me, ""), False)
    check("空 alias", g.matches("", me), False)
    check("纯垃圾 header", g.matches(me, "not an email at all"), False)
    check("None 安全", g.matches(me, None), False)


# ── ④ 诊断输出 ───────────────────────────────────────────────────

def test_describe_no_secrets() -> None:
    print("\n④ describe 不含敏感信息")
    g = MailIdentity("yourname@gmail.com")
    d = g.describe()
    check("含 primary", d["primary"], "yourname@gmail.com")
    check("含 scheme", d["scheme"], "plus")
    check("含示例别名", "example_alias" in d, True)
    # 绝不应出现任何密码类字段
    check("无密钥字段", any(k in d for k in ("password", "token", "secret", "app_password")), False)


def main() -> int:
    print("═" * 64)
    print("邮箱别名引擎回归测试")
    print("═" * 64)
    test_detect_scheme()
    test_derivation()
    test_matching_plus()
    test_matching_domain()
    test_matching_fallback()
    test_gmail_dot_folding()
    test_malformed_input()
    test_describe_no_secrets()

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


# ── ⑤ 安全默认值回归（钉死，防被人改回）─────────────────────────

def test_primary_fallback_defaults_off() -> None:
    """🔴 安全契约：主地址兜底必须默认关闭。

    背景：上游有时把 To 头改写成主地址。若默认开启兜底，
    同一收件箱下多个别名账号会互相认领对方的验证码（跨账号偷码）。
    该测试锁死默认值，任何把默认改回 True 的改动都会在此失败。
    """
    print("\n⑤ 主地址兜底默认值（安全契约）")

    # 不传参 → 默认必须为 False
    g = MailIdentity("yourname@gmail.com")
    check("默认值 = False", g.accept_primary_fallback, False)

    me = "yourname+accio01@gmail.com"
    check("默认下主地址不得被认领",
          g.matches(me, "yourname@gmail.com"), False)

    # 显式开启后才认
    g_on = MailIdentity("yourname@gmail.com", accept_primary_fallback=True)
    check("显式开启后认领", g_on.matches(me, "yourname@gmail.com"), True)

    # 无论开关，自己的别名必须始终能认领
    check("自己的别名恒可认领（默认）",
          g.matches(me, "yourname+accio01@gmail.com"), True)
