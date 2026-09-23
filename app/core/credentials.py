"""凭证存储 —— 加密落盘 + 池化管理。

开源项目的存储设计要点：
  1. **加密**：cookie 是账号的完整凭证（`_m_h5_tk` / `xman_t` / `cookie2`
     任意一个泄露都等于账号被盗），必须加密落盘，不能明文躺着。
  2. **不进仓库**：默认 `./data/`，`.gitignore` 兜住。
  3. **可多账号**：池化，一个账号失效不影响其它。
  4. **身份稳定**：用 `account_key`（账号 UID 的哈希）作稳定标识，
     不依赖文件名 —— 文件名会变，身份不会。
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .config import settings

log = logging.getLogger("accio2api.credentials")

# `InvalidToken` 用于区分「密钥不对/数据被篡改」与「文件只是格式坏了」。
# cryptography 缺失时用占位类兜底（此时解密一律走 EncryptionUnavailable）。
try:
    from cryptography.fernet import InvalidToken
except ImportError:  # pragma: no cover
    class InvalidToken(Exception):  # type: ignore[no-redef]
        """cryptography 不可用时的占位（永不实际抛出）。"""


# ── 加密（Fernet 式对称加密，密钥来自 SECRET_KEY 派生）────────────
def _derive_key(secret: str) -> bytes:
    """从 SECRET_KEY 派生 32 字节 Fernet 密钥。"""
    return base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest())


def _fernet():
    try:
        from cryptography.fernet import Fernet
    except ImportError:  # pragma: no cover
        return None
    return Fernet(_derive_key(settings.secret_key))


class EncryptionUnavailable(RuntimeError):
    """缺少加密依赖 —— 拒绝降级明文落盘。"""


# 🔴 曾经这里是「无 cryptography 就明文落盘，只打条警告」。
#    那是**不可接受**的：cookie 里含 `_m_h5_tk` / `xman_t` / `cookie2`，
#    任意一条泄露 = 账号被完全接管。为了"能跑起来"而把账号凭证明文写盘，
#    是拿用户资产换便利。
#
#    正确做法：**失败要响**。缺依赖就拒绝保存凭证，让部署方去装依赖 ——
#    宁可服务起不来，也不能静默地裸奔。（真实事故：PYTHONPATH 污染导致
#    cryptography 加载失败，凭证以明文落盘且无人察觉。）
def _fernet_or_raise():
    f = _fernet()
    if f is None:
        raise EncryptionUnavailable(
            "缺少 cryptography 依赖，拒绝以明文保存凭证。"
            "请执行：pip install cryptography")
    return f


def encrypt(text: str) -> str:
    return "enc:" + _fernet_or_raise().encrypt(text.encode()).decode()


def decrypt(token: str) -> str:
    if token.startswith("enc:"):
        return _fernet_or_raise().decrypt(
            token[len("enc:"):].encode()).decode()
    if token.startswith("plain:"):
        # 只读兼容：早期版本可能留下过明文，读得出来但**下次保存会转加密**
        log.warning("检测到历史明文凭证，将在下次保存时自动加密")
        return token[len("plain:"):]
    return token


# ── 数据结构 ──────────────────────────────────────────────────
@dataclass
class Credential:
    """一个 Accio 账号凭证。"""
    # 身份
    account_key: str = ""            # 稳定标识 = sha256(userId)[:16]
    user_id: str = ""                # Accio userId
    accio_id: str = ""               # Accio accioId
    nickname: str = ""
    email: str = ""

    # 凭证本体（加密存储）
    cookies: dict[str, str] = field(default_factory=dict)

    # 运行时派生（每次连接时动态获取，不持久化也可）
    agent_id: str = ""
    project_path: str = ""

    # 状态
    enabled: bool = True
    created_at: float = field(default_factory=time.time)
    last_verified_at: float = 0.0
    last_error: str = ""

    # 余额/用量快照
    credits: dict[str, Any] = field(default_factory=dict)

    def to_public(self) -> dict:
        """给管理端看的视图 —— **不含 cookie 原文**。"""
        d = asdict(self)
        d.pop("cookies", None)
        d["has_cookies"] = bool(self.cookies)
        d["cookie_count"] = len(self.cookies)
        return d


def make_account_key(user_id: str) -> str:
    return hashlib.sha256(str(user_id).encode()).hexdigest()[:16]


# ── 池 ───────────────────────────────────────────────────────
class CredentialPool:
    """凭证池：加载/保存/查找/状态更新。线程安全。"""

    def __init__(self, directory: Path | None = None):
        self.dir = directory or settings.credentials_dir
        self.dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._cache: dict[str, Credential] = {}
        # 载入失败清单 [(文件名, 异常类名)] —— 供健康巡检暴露，
        # 避免「SECRET_KEY 变更导致凭证静默消失」无法被察觉。
        self._load_failures: list[tuple[str, str]] = []
        self._load()

    @property
    def load_failures(self) -> list[tuple[str, str]]:
        """本次启动时未能载入的凭证文件 [(文件名, 异常类名)]。"""
        with self._lock:
            return list(self._load_failures)

    # ── 落盘 ─────────────────────────────────────────
    def _path(self, account_key: str) -> Path:
        return self.dir / f"{account_key}.cred"

    def _load(self):
        """从磁盘恢复全部凭证。

        ⚠️ 为什么不能「一律 continue」：若 `SECRET_KEY` 被轮换/变更，
        Fernet 解密会抛 `InvalidToken`。旧代码裸 `except Exception: continue`
        会把这类**系统性失败**静默吞掉 —— 表现为「所有账号凭空消失」，
        运维毫无线索。因此这里分类处理：
          · 解密失败 / 加密不可用 → 计入 `load_failures` 并 log.error
          · 单个文件格式损坏        → 计入 `load_failures` 并 log.warning
        `load_failures` 会经 `/admin/api/credentials/health` 暴露。
        """
        for f in sorted(self.dir.glob("*.cred")):
            try:
                obj = json.loads(decrypt(f.read_text()))
                cred = Credential(**obj)
                if cred.account_key:
                    self._cache[cred.account_key] = cred
            except Exception as e:
                self._load_failures.append((f.name, type(e).__name__))
                if isinstance(e, InvalidToken):
                    log.error(
                        "凭证解密失败（疑似 SECRET_KEY 变更）：%s —— 该账号"
                        "不会被载入。请恢复原 SECRET_KEY 或重新登录导出。", f.name)
                elif isinstance(e, EncryptionUnavailable):
                    log.error("凭证 %s 无法解密：cryptography 不可用", f.name)
                else:
                    log.warning("凭证 %s 载入失败（%s）：%s",
                                f.name, type(e).__name__, str(e)[:200])

    def save(self, cred: Credential):
        with self._lock:
            if not cred.account_key:
                cred.account_key = make_account_key(cred.user_id)
            self._cache[cred.account_key] = cred
            payload = json.dumps(asdict(cred), ensure_ascii=False)
            p = self._path(cred.account_key)
            tmp = p.with_suffix(".tmp")
            tmp.write_text(encrypt(payload))
            try:
                tmp.chmod(0o600)
            except OSError:
                pass
            tmp.replace(p)

    def delete(self, account_key: str) -> bool:
        with self._lock:
            self._cache.pop(account_key, None)
            p = self._path(account_key)
            if p.exists():
                p.unlink()
                return True
            return False

    # ── 查询 ─────────────────────────────────────────
    def get(self, account_key: str) -> Credential | None:
        with self._lock:
            return self._cache.get(account_key)

    def all(self) -> list[Credential]:
        with self._lock:
            return list(self._cache.values())

    def enabled(self) -> list[Credential]:
        return [c for c in self.all() if c.enabled]

    def find_by_user_id(self, user_id: str) -> Credential | None:
        k = make_account_key(user_id)
        return self.get(k)

    # ── 状态更新 ─────────────────────────────────────
    def mark_verified(self, account_key: str, *, credits: dict | None = None):
        with self._lock:
            c = self._cache.get(account_key)
            if not c:
                return
            c.last_verified_at = time.time()
            c.last_error = ""
            if credits is not None:
                c.credits = credits
            self.save(c)

    def mark_error(self, account_key: str, err: str):
        with self._lock:
            c = self._cache.get(account_key)
            if not c:
                return
            c.last_error = str(err)[:400]
            self.save(c)

    def set_enabled(self, account_key: str, enabled: bool) -> bool:
        with self._lock:
            c = self._cache.get(account_key)
            if not c:
                return False
            c.enabled = bool(enabled)
            self.save(c)
            return True


pool = CredentialPool()
