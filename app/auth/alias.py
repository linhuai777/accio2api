"""邮箱别名引擎 —— 从一个「主邮箱」派生无限个可收码的注册地址。

════════════════════════════════════════════════════════════════════
为什么需要这个
════════════════════════════════════════════════════════════════════
批量注册场景下，如果所有账号共用**同一个邮箱字面量**，会遇到两个问题：

  1. **上游风控**：同一邮箱重复注册会被判为滥用（Accio 会直接拒绝或
     标记账号）。单一邮箱 = 单一身份，注册第二个账号就撞墙。
  2. **收码串台**：多个注册流程并行时，无法区分「这封验证码是给哪个
     账号的」。若只有一个收件箱，并发注册必然互相抢码。

别名邮箱同时解决两者：**每个账号一个独立地址，但全部投递到同一个
收件箱**。上游看到的是 N 个不同邮箱（独立身份），我们只需要维护
**一个邮箱的凭证**（一套 IMAP 配置）。

════════════════════════════════════════════════════════════════════
支持哪两种别名机制
════════════════════════════════════════════════════════════════════
主流邮箱的别名机制分两派，本模块都支持：

  ┌────────────┬──────────────────────────┬──────────────────────┐
  │ 机制        │ 形态                      │ 谁支持                │
  ├────────────┼──────────────────────────┼──────────────────────┤
  │ plus 子地址 │ 主名+标签@域名             │ Gmail / Outlook /    │
  │            │ yourname+accio01@gmail.com │ iCloud / Proton /    │
  │            │                          │ QQ / 大量自建服务     │
  ├────────────┼──────────────────────────┼──────────────────────┤
  │ 域名别名    │ 任意本地名@自有域名         │ 自建域（CloudMail）   │
  │            │ any-name@yourdomain.com   │ 指向同一收件箱        │
  └────────────┴──────────────────────────┴──────────────────────┘

**RFC 5233（subaddressing）+ RFC 5321（+ 是合法的邮箱本地部分字符）**
规定了 `+` 子地址必须投递到去掉 `+tag` 后的主地址 —— 这是标准行为，
不是服务商特性，所以 Gmail / Outlook / iCloud / Proton 等都遵循。

════════════════════════════════════════════════════════════════════
为什么收码逻辑不用改
════════════════════════════════════════════════════════════════════
因为别名都是**投递到同一个物理收件箱**的，IMAP 连的还是那个邮箱，
只是**过滤条件从「邮箱匹配」放宽为「收件人包含该别名」**。

本模块提供 `matches()` 做这个判断 —— 收码后端拿它比对邮件头：
  · plus 模式   → 邮件 To 头里会出现完整别名（yourname+tag@gmail.com）
  · 域名别名模式 → 邮件 To 头里是那个别名
  · 兜底       → 若上游把 To 改写成主地址（少数 MTA 会做），
                 仅当 `accept_primary_fallback=True` 才认。**默认 False**：
                 开启会让所有别名账号认领主地址的邮件 → 跨账号偷码。

`standardize()` 把别名归一化成「主地址形式」，供跨机制比对 ——
`yourname+tag@gmail.com` 与 `yourname@gmail.com` 归一化后相同，
于是「这封邮件到底该归给哪个账号」可以精确判定。
"""

from __future__ import annotations

import hashlib
import re
import secrets
import string

# RFC 5321：本地部分允许这些字符。`+` 是子地址分隔符。
_LOCAL_OK = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+$")

# 别名标签安全字符（去掉可能被 MTA/URL 转义的字符，保证可放在 To 头里）
_TAG_ALPHABET = string.ascii_lowercase + string.digits

# 「点号不区分」的域名 —— 本地部分的 `.` 会被服务商忽略，
# 即 `first.last@x` 与 `firstlast@x` 是同一收件箱。
# 这**不是**通用邮箱规则（RFC 5321 里 `.` 是有效字符），
# 而是这两个服务商的既定行为。QQ/163 等不在此列，保持严格。
DOT_INSENSITIVE_DOMAINS = frozenset({
    "gmail.com", "googlemail.com",
    "outlook.com", "outlook.jp", "hotmail.com", "live.com", "live.cn", "msn.com",
})

# Gmail 的 googlemail.com 与 gmail.com 等价（同一收件箱）
_DOMAIN_EQUIV = {"googlemail.com": "gmail.com"}


class AliasError(ValueError):
    """别名配置或生成失败。"""


# ── 机制判定 ─────────────────────────────────────────────────────

def detect_scheme(addr: str) -> str:
    """按地址形态推断应使用哪种别名机制。

    返回：
        "plus"     —— 支持 `+tag` 子地址（Gmail / Outlook / iCloud…）
        "domain"   —— 自有域名，可任意本地名（CloudMail 类）
        "unknown"  —— 无法判断；调用方应显式配置，不要猜

    判定依据（保守，宁可报 unknown 也不猜错）：
        · Gmail / Googlemail / Outlook / Hotmail / Live / iCloud /
          Proton / Yahoo / QQ / 163 / 126 → plus
        · 其它域名 → unknown（由用户显式指定 USABLE_ALIAS_SCHEME）

    ⚠️ **不做「域名看起来像自建就判 domain」的猜测** —— 猜错会导致
       生成的别名根本收不到码，且失败现象是「注册成功但永远等不到
       验证码」，极难排查。宁可要求显式配置。
    """
    if "@" not in addr:
        raise AliasError(f"非法邮箱地址：{addr!r}")
    domain = addr.rsplit("@", 1)[1].lower().strip()

    plus_domains = (
        "gmail.com", "googlemail.com",
        "outlook.com", "outlook.jp", "hotmail.com", "live.com", "live.cn",
        "msn.com", "passport.com",
        "icloud.com", "me.com", "mac.com",
        "proton.me", "protonmail.com", "pm.me",
        "yahoo.com", "yahoo.co.jp", "ymail.com",
        "qq.com", "foxmail.com",
        "163.com", "126.com", "yeah.net",
        "zoho.com", "zohomail.com",
        "fastmail.com", "gmx.com", "gmx.net", "mail.com",
    )
    if domain in plus_domains:
        return "plus"
    return "unknown"


# ── 标签生成 ─────────────────────────────────────────────────────

def make_tag(seed: str | None = None, *, length: int = 6) -> str:
    """生成一个别名标签。

    为什么不用纯随机：**可追溯性**。seed 传入账号标识（如凭证 id 前缀）
    时，标签可复现 —— 排查「某封码是给谁的」时能直接从标签反推账号。
    seed 为空则用 `secrets` 生成，保证不可预测（防上游按标签规律封号）。
    """
    if seed:
        # 取 hash 前 N 位：既缩短长度，又避免 seed 里的特殊字符
        h = hashlib.sha256(seed.encode("utf-8")).hexdigest()
        return h[:max(1, int(length))]
    return "".join(secrets.choice(_TAG_ALPHABET) for _ in range(max(1, int(length))))


# ── 地址派生 ─────────────────────────────────────────────────────

class MailIdentity:
    """一个「主邮箱 + 别名策略」的身份。

    用法：
        ident = MailIdentity("yourname@gmail.com", scheme="plus")
        alias = ident.alias("accio01")      # → yourname+accio01@gmail.com
        ident.matches(alias, to_header)     # → 这封邮件是不是给它的
    """

    def __init__(self, primary: str, *, scheme: str = "auto",
                 domain: str = "", tag_length: int = 6,
                 accept_primary_fallback: bool = False):
        """
        primary  主邮箱（真实收件箱，也是 IMAP 登录账号）
        scheme   "plus" / "domain" / "auto"（auto = detect_scheme）
        domain   仅 domain 模式需要（自有域名，如 "mail.example.com"）；
                 plus 模式忽略此参数
        accept_primary_fallback
                 上游把别名改写回主地址时是否仍认。**默认 False**。
                 为何默认关闭：开启后，收件箱里任何一封投给主地址的
                 邮件都会被**每个**别名账号认领 → 批量注册时 A 账号
                 会抢到 B 账号的验证码（偷码）。只有在你确认上游确实
                 改写地址、且不存在并发多账号注册时才可开启。
        """
        primary = (primary or "").strip()
        if "@" not in primary:
            raise AliasError(f"主邮箱非法：{primary!r}")
        self.primary = primary
        self.local, self._domain = primary.rsplit("@", 1)
        self._domain = self._domain.lower()

        if scheme == "auto":
            scheme = detect_scheme(primary)
            if scheme == "unknown":
                raise AliasError(
                    f"无法自动判断 {primary} 支持哪种别名机制。"
                    "请在 .env 显式设置 ALIAS_SCHEME=plus（Gmail/Outlook 类）"
                    "或 ALIAS_SCHEME=domain（自有域名）。")
        if scheme not in ("plus", "domain"):
            raise AliasError(f"未知别名机制：{scheme!r}（可选 plus / domain）")
        if scheme == "domain" and not domain:
            raise AliasError("domain 模式必须提供 ALIAS_DOMAIN（自有域名）")

        self.scheme = scheme
        self.alias_domain = (domain or "").strip().lower()
        self.tag_length = int(tag_length)
        self.accept_primary_fallback = bool(accept_primary_fallback)

    # ── 派生 ────────────────────────────────────────────────────

    def alias(self, tag: str | None = None) -> str:
        """派生一个别名地址。

        tag 为空时用 `make_tag()` 随机生成（不可预测，防规律封号）。
        """
        t = (tag or make_tag(length=self.tag_length)).strip().lower()
        if not t:
            raise AliasError("别名标签不能为空")
        if not all(c in _TAG_ALPHABET for c in t):
            raise AliasError(
                f"别名标签只允许小写字母与数字：{t!r}"
                "（避免 MTA / URL 编码差异导致收码匹配失败）")

        if self.scheme == "plus":
            # RFC 5233：主名+标签@原域名
            return f"{self.local}+{t}@{self._domain}"
        # domain 模式：任意本地名 @ 自有域名
        return f"{t}@{self.alias_domain}"

    def alias_for_account(self, account_key: str) -> str:
        """按账号键派生**可复现**的别名（同一账号永远得到同一地址）。

        用于：凭证已存在时重算地址、排查时从地址反查账号。
        """
        return self.alias(make_tag(account_key, length=self.tag_length))

    # ── 匹配 ────────────────────────────────────────────────────

    def standardize(self, addr: str) -> str:
        """归一化：把别名折叠回「主地址形式」，用于跨机制比对。

        plus 模式：  yourname+tag@gmail.com  →  yourname@gmail.com
        domain 模式：anything@aliasdomain   →  primary
        已是主地址： 原样返回

        这是 `matches()` 的底层依据 —— 若两封邮件归一化后相同，
        说明它们来自同一个物理收件箱。
        """
        a = (addr or "").strip().lower()
        if not a or "@" not in a:
            return ""
        local, dom = a.rsplit("@", 1)

        if self.scheme == "plus":
            # 去掉 `+` 及其后（含 Gmail 的 `.` 折叠规则：点号不区分）
            base = local.split("+", 1)[0].replace(".", "")
            if dom in ("gmail.com", "googlemail.com"):
                # Gmail 的 googlemail/gmail 等价
                return f"{base}@gmail.com"
            return f"{base}@{dom}"

        # domain 模式：只有一个物理收件箱 = primary
        if dom == self.alias_domain:
            return self.primary.lower()
        return a

    def matches(self, alias: str, recipient_header: str) -> bool:
        """判断邮件头里的收件人 `recipient_header` 是否属于别名 `alias`。

        比对顺序（从严到宽）：
            1. 精确匹配完整地址（最常见、最可靠）
            2. 从 To 头**解析出地址**，比对「同一收件箱 + 标签完全相等」
            3. 若 accept_primary_fallback，且解析出的地址归一化后是主地址 → 认

        ⚠️ 严禁用「标签是否为 header 子串」判断 —— 标签 `a` 会是
           `yourname+b@gmail.com` 的子串，导致兄弟账号互相偷码。
           （此坑已由单元测试捕获，见 tests/test_alias.py）

        返回 False 表示「不是给我的」，调用方应继续找下一封。
        """
        h = (recipient_header or "").strip().lower()
        a = (alias or "").strip().lower()
        if not h or not a:
            return False

        # 1) 精确：To 头可能形如 "名字 <addr>"，用**词边界**子串匹配
        #    加边界是为了避免 `+abc` 命中 `+abcd`（前缀包含）。
        for cand in self._extract_addrs(h):
            if cand == a:
                return True
            # 2) 同一收件箱 + 标签/本地名完全相等（含点号折叠归一化）
            if self._same_mailbox(cand, a) and self._label_eq(cand, a):
                return True
            # 3) 主地址兜底
            if self.accept_primary_fallback and self._is_primary(cand):
                return True

        # 兜底：header 里没有可解析地址（异常格式）时，用带边界的整串包含
        return bool(re.search(rf"(?<![0-9a-z._+-]){re.escape(a)}(?![0-9a-z._+-])", h))

    # ── 内部判定（拆开便于单测）─────────────────────────────────

    @staticmethod
    def _extract_addrs(header: str) -> list[str]:
        """从 To/Cc 头解析出所有邮箱地址（处理 "名字 <addr>" 与多地址）。"""
        out = []
        for m in re.finditer(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", header):
            out.append(m.group(0).lower())
        return out or ([header] if "@" in header else [])

    def _label(self, addr: str) -> str:
        """取地址用于身份判定的「标签」：

        plus 模式：`+` 之后的部分；**主地址（无 `+`）返回空串**
                   —— 这样 `yourname+a@` 与 `yourname@` 标签不同，
                      但两者都区别于 `yourname+b@`。
        domain 模式：本地部分整体（自有域下每个本地名是独立别名）。
        """
        if "@" not in addr:
            return ""
        local, dom = addr.rsplit("@", 1)
        if self.scheme == "plus":
            tag = local.split("+", 1)[1] if "+" in local else ""
            # 标签内若含点号，按域名规则折叠（Gmail/Outlook 点号不区分）
            return self._norm_local(tag, dom) if tag else ""
        return local

    def _label_eq(self, addr: str, other: str) -> bool:
        """标签是否等价（含点号折叠）。两者都必须是别名（非主地址）。"""
        la, lo = self._label(addr), self._label(other)
        # 主地址（空标签）不参与本分支 —— 它走 _is_primary 兜底
        if not la or not lo:
            return False
        return la == lo

    def _same_mailbox(self, addr: str, other: str) -> bool:
        """两个地址是否落在同一个物理收件箱。"""
        d1 = addr.rsplit("@", 1)[-1] if "@" in addr else ""
        d2 = other.rsplit("@", 1)[-1] if "@" in other else ""
        if self.scheme == "plus":
            def norm_dom(d):
                return _DOMAIN_EQUIV.get(d, d)
            return norm_dom(d1) == norm_dom(d2)
        return d1 == self.alias_domain and d2 == self.alias_domain

    @staticmethod
    def _norm_local(local: str, dom: str) -> str:
        """按域名规则归一化本地部分（点号折叠）。"""
        if _DOMAIN_EQUIV.get(dom, dom) in DOT_INSENSITIVE_DOMAINS:
            return local.replace(".", "")
        return local

    def _is_primary(self, addr: str) -> bool:
        """地址是否就是主地址（含点号折叠 / googlemail 等价折算）。"""
        if "@" not in addr:
            return False
        local, dom = addr.rsplit("@", 1)
        pl, pd = self.primary.lower().rsplit("@", 1)

        if _DOMAIN_EQUIV.get(dom, dom) != _DOMAIN_EQUIV.get(pd, pd):
            return False
        if self.scheme == "plus":
            return self._norm_local(local, dom) == self._norm_local(pl, pd)
        return f"{local}@{dom}" == self.primary.lower()

    # ── 诊断 ────────────────────────────────────────────────────

    def describe(self) -> dict:
        """给管理端展示用（**不含任何密钥**）。"""
        return {
            "primary": self.primary,
            "scheme": self.scheme,
            "alias_domain": self.alias_domain or None,
            "tag_length": self.tag_length,
            "accept_primary_fallback": self.accept_primary_fallback,
            "example_alias": self.alias("example"),
        }


# ── 工厂 ─────────────────────────────────────────────────────────

def from_settings(settings) -> MailIdentity | None:
    """按 `.env` 配置构造身份；未配置别名时返回 None。

    配置项：
        MAIL_PRIMARY            主邮箱（真实收件箱）
        ALIAS_SCHEME            plus / domain / auto（默认 auto）
        ALIAS_DOMAIN            domain 模式的域名
        ALIAS_TAG_LENGTH        标签长度（默认 6）
        ALIAS_ACCEPT_PRIMARY_FALLBACK  主地址兜底（默认 true）
    """
    primary = (getattr(settings, "mail_primary", "") or "").strip()
    if not primary:
        return None
    return MailIdentity(
        primary,
        scheme=(getattr(settings, "alias_scheme", "auto") or "auto").lower(),
        domain=getattr(settings, "alias_domain", "") or "",
        tag_length=int(getattr(settings, "alias_tag_length", 6) or 6),
        accept_primary_fallback=bool(
            getattr(settings, "alias_accept_primary_fallback", False)),
    )
