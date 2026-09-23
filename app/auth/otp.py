"""验证码（OTP）后端 —— 可插拔实现 + 邮箱别名归属判定。

════════════════════════════════════════════════════════════════════
设计
════════════════════════════════════════════════════════════════════
Accio 走「邮箱 + 6 位数字验证码」。不同部署者收码方式不同，所以做成
后端可插拔，`.env` 里 `OTP_BACKEND=` 切换：

  cloudmail  自建 CloudMail（支持任意域名别名）
  imap       任何标准 IMAP 邮箱（Gmail/Outlook/QQ/自建…）—— 最通用
  manual     不自动取；由用户在网页上手动填写（最安全，零邮箱配置）

统一接口：`fetch_code(email, *, after_ts, timeout_s) -> str | None`
其中 `email` 是**本次注册使用的地址**（可能是一个别名）。

════════════════════════════════════════════════════════════════════
别名支持（v2 重点）
════════════════════════════════════════════════════════════════════
配了 `MAIL_PRIMARY` 后，所有别名都投递到同一个物理收件箱。因此收码
后端的核心工作从「找一个固定邮箱的邮件」变成
**「在一个收件箱里，挑出投递给指定别名的那一封」**。

归属判定统一委托给 `alias.MailIdentity.matches()` ——
IMAP 后端不再自己写 `"accio" in subject` 这类脆弱启发式。

⚠️ 历史上 IMAP 后端**完全忽略 `target_email` 参数**，只按主题含
   "accio" 取最新一封。这在单账号时能用，一旦并行注册多个账号
   就会互相偷码（A 拿到 B 的验证码）。v2 已修正。
"""

from __future__ import annotations

import email
import imaplib
import re
import time
from datetime import datetime
from email.header import decode_header, make_header
from typing import Protocol

from ..core.config import settings
from . import alias as alias_mod


class OtpAuthError(RuntimeError):
    """收码后端的**认证**失败（token 过期 / 密码错）—— 不可自动恢复。

    与「邮件还没来」有本质区别：前者继续轮询只会耗尽超时并伪装成
    漏码，必须立即让调用方看见。"""


CODE_RE = re.compile(r"\b(\d{6})\b")


def _decode_hdr(v) -> str:
    """解码 MIME 编码的头部（=?utf-8?B?...?= → 明文）。

    不解码会导致中文主题/收件人显示名变成乱码，
    但**地址部分通常不编码**，所以匹配多半仍能工作 ——
    解码是为了兜住少数会编码地址的 MTA。
    """
    if not v:
        return ""
    try:
        return str(make_header(decode_header(str(v))))
    except Exception:
        return str(v)


class OtpBackend(Protocol):
    name: str

    def fetch_code(self, target_email: str, *, after_ts: float = 0.0,
                   timeout_s: int = 120) -> str | None:
        ...


# ── 归属判定（三条后端共用）──────────────────────────────────

def _identity() -> alias_mod.MailIdentity | None:
    """当前配置下的邮箱身份；未配别名时为 None。"""
    try:
        return alias_mod.from_settings(settings)
    except alias_mod.AliasError:
        # 配置错误不应炸掉收码流程，只降级为「无别名」
        import logging
        logging.getLogger("accio2api.otp").warning(
            "别名配置无效，降级为单邮箱模式（详见 error 日志）", exc_info=True)
        return None


def _belongs(target_email: str, recipient_headers: list[str],
             ident: alias_mod.MailIdentity | None) -> bool:
    """这封邮件是否投递给 `target_email`。

    ident 存在 → 用别名引擎精确判定；
    ident 缺失 → 退化为「大小写不敏感的子串匹配」，但仍优先精确。
    """
    t = (target_email or "").strip().lower()
    if not t:
        return True                      # 未指定目标 → 接受任意（单邮箱模式）
    if ident is not None:
        return any(ident.matches(t, h) for h in recipient_headers if h)
    # 降级路径（ident 解析失败时）：必须用**词边界**匹配，不能裸子串。
    # 🔴 O-5：旧实现 `if t in hl` 会让 `ab@x.com` 命中 `xab@x.com` /
    #    `acc01@d.com` 命中 `acc010@d.com` —— 批量注册场景下直接串台偷码。
    #    这里加「前后不得是邮箱/标识符字符」的边界断言。
    pat = re.compile(rf"(?<![0-9a-z._+\-]){re.escape(t)}(?![0-9a-z._+\-])")
    for h in recipient_headers:
        if pat.search((h or "").lower()):
            return True
    return False


# ── CloudMail ────────────────────────────────────────────────
def _body_says_401(r) -> bool:
    """CloudMail 把业务错误塞在 body 里：HTTP 200 + {"code":401,...}。

    用于让 `_post` 的自愈逻辑正确识别「token 失效」。

    🔴 双重条件（O-2）：仅 `code in (401,403)` 可能误判 —— 某些后台用
    `code` 表达正常业务状态。必须**同时**满足「业务码异常 **且** 没有
    有效 `data`」，才认定为 token 失效，避免每次成功请求后白打一发
    `genToken`（徒增被上游限流的概率）。
    """
    try:
        if "json" not in (r.headers.get("content-type") or "").lower():
            return False
        d = r.json()
        return (int(d.get("code") or 0) in (401, 403)
                and not d.get("data"))
    except Exception:
        return False


class CloudMailBackend:
    """自建 CloudMail（`mail.<domain>`）。

    实测要点（都是踩过的坑，写下来免得后人重踩）：
      * 鉴权用**裸 token**：`Authorization: <token>`
        —— 带 `Bearer ` 前缀会 401。
      * `emailList` **忽略 email 参数**，返回全表最新 N 条，
        必须**客户端按 toEmail 过滤**。
      * 邮件 `content` 可能是几十 KB 的 HTML，请求要限 size 并截断正文。
      * token 会过期，401 时用 email+password 重新 `genToken` 自愈。

    别名：CloudMail 走 `ALIAS_SCHEME=domain` —— 泛域名收信，
    任意 `本地名@自有域名` 都进同一收件箱。
    """
    name = "cloudmail"

    def __init__(self):
        self.base = settings.cloudmail_base.rstrip("/")
        self.token = settings.cloudmail_token
        self.email = settings.cloudmail_email
        self.password = settings.cloudmail_password
        self._session = None

    def _s(self):
        import requests
        if self._session is None:
            s = requests.Session()
            s.trust_env = False
            s.headers.update({"Content-Type": "application/json"})
            if self.token:
                s.headers["Authorization"] = self.token   # 裸 token，无 Bearer
            self._session = s
        return self._session

    def _gen_token(self) -> str:
        """用邮箱+密码换新 token。

        🔴 必须检查 **业务码**，不能只看 HTTP 状态 / 直接取 `data.token`：
        CloudMail 密码错误时返回的是 **HTTP 200 + {"code":401,...}**（同其
        它接口一致的行为）。旧实现直接 `r.json()["data"]["token"]` 会抛
        `KeyError('token')`，冒泡后被 `fetch_code` 的宽 `except` 吞成
        「没收到邮件」，并在超时窗口内反复重试 —— 把认证失败伪装成
        「邮件没来」。这里显式抛 `OtpAuthError`，让上层立即暴露真实原因。
        """
        import requests
        r = requests.post(f"{self.base}/genToken",
                          json={"email": self.email, "password": self.password},
                          headers={"Content-Type": "application/json"},
                          timeout=20)
        r.raise_for_status()
        try:
            body = r.json()
        except Exception:
            raise OtpAuthError("收码后端返回非 JSON，无法获取 token")
        code = int(body.get("code") or 0)
        tok = ((body.get("data") or {}) or {}).get("token")
        if not tok:
            raise OtpAuthError(
                f"收码后端拒绝换取 token（code={code}）—— "
                f"请核对 IR_CLOUDMAIL_EMAIL / IR_CLOUDMAIL_PASSWORD 是否正确")
        self.token = tok
        if self._session is not None:
            self._session.headers["Authorization"] = self.token
        return self.token

    def _post(self, path: str, payload: dict, _retry: bool = True) -> dict:
        r = self._s().post(f"{self.base}{path}", json=payload, timeout=25)
        # 🔴 CloudMail 的「token 失效」是 **HTTP 200 + body {"code":401}**，
        #    不是 HTTP 401（实测）。只判 `r.status_code` 会导致自愈**永不触发**
        #    ——表现为 token 一过期就永久收不到码，且报「未获取到验证码」。
        inflight = (r.status_code == 401) or _body_says_401(r)
        if inflight and _retry and self.email and self.password:
            self._gen_token()
            got = self._post(path, payload, _retry=False)
            # 🔴 换过 token **仍然** 401 → 不是「token 过期」而是认证本身
            #    不成立（密码错/账号停用）。此时必须显式抛错，否则会返回
            #    一个看似正常的空结果，把认证失败再次伪装成「邮件没来」。
            if isinstance(got, dict) and int(got.get("code") or 0) in (401, 403):
                raise OtpAuthError(
                    "换取 token 后仍被拒绝 —— 邮箱或密码无效")
            return got
        r.raise_for_status()
        return r.json()

    def fetch_code(self, target_email: str, *, after_ts: float = 0.0,
                   timeout_s: int = 120) -> str | None:
        ident = _identity()
        seen: set[str] = set()
        t0 = time.time()
        # 认证类错误（401/403）**不可恢复** —— 继续轮询只是在浪费
        # `timeout_s`，最终返回 None 让上层误以为「邮件没来」。
        # 旧代码把这类异常和网络抖动一起吞掉，导致真正的原因
        # （token 失效 / 密码错）被完全掩盖。这里显式区分：
        # 认证失败 → 立即抛出，让调用方看到真实错误。
        _auth_fails = 0
        while time.time() - t0 < timeout_s:
            try:
                data = self._post("/emailList", {"size": 30, "current": 1})
                _auth_fails = 0
            except Exception as e:
                code = getattr(getattr(e, "response", None), "status_code", 0)
                if code in (401, 403):
                    _auth_fails += 1
                    if _auth_fails >= 2:      # 重取 token 后仍失败
                        raise OtpAuthError(
                            f"CloudMail 认证失败（HTTP {code}）—— 请检查 "
                            f"IR_CLOUDMAIL_TOKEN / IR_CLOUDMAIL_PASSWORD 是否有效"
                        ) from e
                time.sleep(3)
                continue
            rows = data.get("data") or []
            cands = []
            for m in rows:
                to_raw = str(m.get("toEmail") or "")
                if not _belongs(target_email, [to_raw], ident):
                    continue
                if "accio" not in str(m.get("sendEmail") or "").lower():
                    continue
                mid = str(m.get("emailId") or "")
                if mid in seen:
                    continue
                ts = self._ts(m.get("createTime"))
                if after_ts:
                    if not ts:
                        # ⚠️ 时间戳缺失/无法解析：**保守跳过**而非放行。
                        # 旧逻辑 `if ts and after_ts and ...` 在 ts==0 时
                        # 短路为假 → 该邮件无条件进入候选 → 可能把上一位
                        # 账号的旧码当成本次结果（串台）。
                        continue
                    if ts < after_ts - 5:
                        continue
                cands.append((ts, mid, str(m.get("subject") or "")))
            if cands:
                cands.sort(reverse=True)
                _, mid, subj = cands[0]
                seen.add(mid)
                mm = CODE_RE.search(subj)
                if mm:
                    return mm.group(1)
            time.sleep(3)
        return None

    @staticmethod
    def _ts(v) -> float:
        """把 CloudMail 的 createTime 解析成 epoch 秒。

        🔴 真实格式是**字符串日期** `"2026-09-23 17:29:23"`（实测），
        不是数字时间戳。早期版本只做 `float(v)`，对字符串直接抛异常
        返回 0.0；而调用方的「时间戳缺失则保守跳过」分支会因此**丢掉
        每一封邮件**——表现为「收件箱有码但一直等不到」。
        这里同时兼容：数字（秒/毫秒）、字符串日期、ISO 8601。
        """
        if v is None or v == "":
            return 0.0
        # 1) 数字时间戳（秒 或 毫秒）
        try:
            n = float(v)
            return n / 1000 if n > 1e11 else n
        except (TypeError, ValueError):
            pass
        # 2) 带时区的 ISO（必须**先于**朴素格式处理）
        #    🔴 O-4：旧实现先做 `s[:19]` 截断再 strptime，会把
        #    `"2026-09-23T17:29:23Z"` 的时区信息丢掉并按**本地时间**解释。
        #    本机 TZ=UTC 时恰好正确，掩盖了 bug；换到 TZ=Asia/Shanghai
        #    即偏移 8 小时 → 8 小时内的新验证码全被判为「过期」丢弃，
        #    又回到「收件箱有码却等不到」。
        s = str(v).strip()
        if s.endswith("Z") or re.search(r"[+-]\d{2}:?\d{2}$", s):
            try:
                return datetime.fromisoformat(
                    s.replace("Z", "+00:00")).timestamp()
            except Exception:
                pass
        # 3) 无时区的朴素格式
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S",
                    "%Y/%m/%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                return datetime.strptime(s[:19], fmt).timestamp()
            except Exception:
                continue
        return 0.0


# ── 通用 IMAP ────────────────────────────────────────────────
class ImapBackend:
    """标准 IMAP 收码 —— 最通用，不绑定任何自建服务。

    配置（.env）：
        IMAP_HOST / IMAP_PORT / IMAP_USER / IMAP_PASSWORD / IMAP_SSL
        IMAP_FOLDER         默认 INBOX（Gmail 可设 "[Gmail]/All Mail"）
        IMAP_MAILBOX_DOMAINS  逗号分隔；别名可能出现在多个收信域时使用

    Gmail / Outlook 用**应用专用密码**（App Password）—— 两大服务商
    都已禁用「账号密码直连 IMAP」，必须用应用码。如何在网页上开启见
    README「邮箱配置」章节。

    ══════════════════════════════════════════════════════════════
    别名收码的正确做法（v2 修正）
    ══════════════════════════════════════════════════════════════
    IMAP 服务端**不索引 `+tag`**：搜 `To: yourname+accio01@gmail.com`
    通常搜不到任何邮件（Gmail 的 IMAP SEARCH 只按主地址索引）。

    所以流程必须是：
        1. 用宽松条件（FROM accio / 时间窗口）拉一批候选邮件
        2. **在客户端逐封解析 To/Cc 头**，用别名引擎判定归属
        3. 命中即返回

    绝不能只靠服务端 SEARCH —— 那是本后端 v1 的失败原因之一。
    """

    name = "imap"

    def __init__(self):
        import os
        self.host = os.environ.get("IMAP_HOST", "") or getattr(settings, "imap_host", "")
        self.port = int(os.environ.get("IMAP_PORT", "993") or 993)
        self.user = os.environ.get("IMAP_USER", "") or getattr(settings, "imap_user", "")
        self.password = (os.environ.get("IMAP_PASSWORD", "")
                         or getattr(settings, "imap_password", ""))
        self.use_ssl = str(os.environ.get("IMAP_SSL", "1")).lower() in ("1", "true", "yes")
        self.folder = os.environ.get("IMAP_FOLDER", "INBOX") or "INBOX"

    def _connect(self):
        box = (imaplib.IMAP4_SSL(self.host, self.port) if self.use_ssl
               else imaplib.IMAP4(self.host, self.port))
        box.login(self.user, self.password)
        try:
            box.select(self.folder, readonly=True)
        except Exception:
            box.select("INBOX", readonly=True)
        return box

    def _recipients(self, msg) -> list[str]:
        """取出这封邮件的全部收件人头（To / Cc / X-Original-To / Delivered-To）。

        `Delivered-To` / `X-Original-To` 是**关键** —— 有些 MTA 在转发
        到最终收件箱时会把 `To` 改写为主地址，但 `Delivered-To` 里仍
        保留原始（别名）地址。别名匹配要把这些头都算进去。
        """
        heads = []
        for key in ("To", "Cc", "X-Original-To", "Delivered-To", "Envelope-To"):
            for v in msg.get_all(key) or []:
                heads.append(_decode_hdr(v))
        return heads

    def fetch_code(self, target_email: str, *, after_ts: float = 0.0,
                   timeout_s: int = 120) -> str | None:
        if not self.host:
            return None
        ident = _identity()
        t0 = time.time()
        seen_ids: set[bytes] = set()

        while time.time() - t0 < timeout_s:
            box = None
            try:
                box = self._connect()
                # 候选范围：优先按发件人/近 N 天缩，避免全箱扫描
                ids: list[bytes] = []
                for criterion in (
                    '(FROM "accio")',
                    f'(SINCE "{time.strftime("%d-%b-%Y", time.localtime(t0 - 86400))}")',
                    "ALL",
                ):
                    typ, data = box.search(None, criterion)
                    if typ == "OK" and data and data[0]:
                        ids = data[0].split()
                        if ids:
                            break

                for i in reversed(ids[-40:]):        # 只看最近 40 封
                    if i in seen_ids:
                        continue
                    seen_ids.add(i)
                    typ, msg_data = box.fetch(i, "(RFC822)")
                    if typ != "OK" or not msg_data or not msg_data[0]:
                        continue
                    try:
                        msg = email.message_from_bytes(msg_data[0][1])
                    except Exception:
                        continue

                    # 时间窗（after_ts 之后才发出的码才算数）
                    if after_ts > 0:
                        try:
                            sent = email.utils.parsedate_to_datetime(msg.get("Date"))
                            if sent and sent.timestamp() < after_ts - 60:
                                continue
                        except Exception:
                            pass

                    if not _belongs(target_email, self._recipients(msg), ident):
                        continue

                    subj = _decode_hdr(msg.get("Subject"))
                    mm = CODE_RE.search(subj)
                    if not mm:
                        # 有些服务商把码放正文，兜底扫正文
                        body = self._text_body(msg)
                        mm = CODE_RE.search(body)
                    if mm:
                        return mm.group(1)
            except Exception:
                pass                          # 网络/认证抖动 → 下一轮重试
            finally:
                try:
                    if box is not None:
                        box.logout()
                except Exception:
                    pass
            time.sleep(4)
        return None

    @staticmethod
    def _text_body(msg) -> str:
        """取纯文本正文（截断），用于主题里找不到码的情况。"""
        try:
            if msg.is_multipart():
                for part in msg.walk():
                    if part.get_content_type() == "text/plain":
                        raw = part.get_payload(decode=True) or b""
                        return raw.decode(part.get_content_charset() or "utf-8",
                                          errors="ignore")[:4000]
                return ""
            raw = msg.get_payload(decode=True) or b""
            return raw.decode(msg.get_content_charset() or "utf-8",
                              errors="ignore")[:4000]
        except Exception:
            return ""


# ── 手动 ─────────────────────────────────────────────────────
class ManualBackend:
    """不自动取码 —— 由网页上的用户手动填写。

    实现方式：登录会话里维护一个「等待输入」的槽位，
    网页 POST 上来的验证码直接塞进去。这里返回 None 不会阻塞调用方，
    因为 Web 登录模式走的是 `submit_code()` 另一条路。
    """
    name = "manual"

    def fetch_code(self, target_email: str, *, after_ts: float = 0.0,
                   timeout_s: int = 120) -> str | None:
        return None


_BACKENDS: dict[str, type] = {
    "cloudmail": CloudMailBackend,
    "imap": ImapBackend,
    "manual": ManualBackend,
}


def get_backend(name: str | None = None) -> OtpBackend:
    key = (name or settings.otp_backend or "cloudmail").lower()
    cls = _BACKENDS.get(key)
    if cls is None:
        raise ValueError(f"未知 OTP 后端: {key}（可选 {'/'.join(_BACKENDS)}）")
    return cls()
