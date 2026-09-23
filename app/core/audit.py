"""调用审计 —— 每一次 /v1/* 请求的持久化记录。

════════════════════════════════════════════════════════════════════
为什么需要
════════════════════════════════════════════════════════════════════
OpenAI 兼容网关一旦对外提供，运维者必然要回答这些问题：

  · 谁在用？用了多少？（token 账单 / 配额分配依据）
  · 哪个模型最热？延迟如何？（容量规划依据）
  · 失败率多高？集中在哪个账号？（故障定位依据）
  · 某时刻的异常请求能不能回溯？（安全审计依据）

没有审计层时，这些问题的唯一答案是「翻服务日志 grep」——
日志会被轮转、格式是混杂的、且没有结构化查询能力。

本模块提供一个**结构化、可轮转、可聚合**的审计层。

════════════════════════════════════════════════════════════════════
存储设计
════════════════════════════════════════════════════════════════════
采用**按天分片的 JSONL**（`audit/YYYY-MM-DD.jsonl`），而非数据库：

  · **零依赖** —— 不需要 SQLite/PG，部署即用（本项目的核心约束）
  · **追加写** —— 每行一个 JSON 对象，崩溃不损坏已有数据
  · **易轮转** —— 按天分片，删旧文件即清理
  · **易导出** —— 就是文本，grep/jq/导入分析工具都直接可用

代价是没有索引，查询需全量扫描。但对「单机自用/小团队」的
量级（每天数千条）完全够用，且避免了数据库的运维负担。

════════════════════════════════════════════════════════════════════
隐私
════════════════════════════════════════════════════════════════════
**默认不记录消息正文** —— 只记元数据（模型、token 数、延迟、
状态码、调用方标识）。这既是隐私考虑，也避免日志体积失控。

如需记录正文用于调试，显式设置 `AUDIT_LOG_BODY=true`，
且**正文会被截断**（默认 400 字符）。

调用方标识（`caller`）取自 API Key 的哈希前缀，**不存明文** ——
既能区分不同调用方，又不泄露密钥。
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from .config import settings

_lock = threading.Lock()


def _dir() -> Path:
    """返回审计目录（权限 0700）。

    🔴 审计内容含密钥哈希前缀、account_key、模型、上游 URL，
    开启 log_body 后还含用户正文。旧实现不设 mode → 继承 umask 0022
    → 目录 0755、文件 0644，**同主机任意用户可读全部审计**。
    这里显式收紧，并在已存在时纠正旧权限（兼容历史部署）。
    """
    d = Path(settings.audit_dir or "audit")
    d.mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        try:
            os.chmod(d, 0o700)
        except OSError:
            pass
    return d


def _open_append(p: Path):
    """以 0600 打开追加写句柄（不存在则创建时即带权限，避免竞态窗口）。"""
    if os.name == "posix":
        return os.fdopen(
            os.open(p, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600),
            "a", encoding="utf-8")
    return p.open("a", encoding="utf-8")


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _path(day: str) -> Path:
    return _dir() / f"{day}.jsonl"


def caller_id(api_key: str) -> str:
    """把 API Key 折算成可展示的调用方标识（**不可逆**）。

    取 SHA-256 前 12 位 —— 足以区分调用方（碰撞概率可忽略），
    又无法反推密钥。比「记录明文密钥」安全，
    比「只记 unknown」可运维。
    """
    if not api_key:
        return "anonymous"
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:12]


class AuditLog:
    """审计记录器 —— 线程安全、失败静默（不影响主流程）。"""

    def __init__(self):
        self.enabled = bool(getattr(settings, "audit_enabled", True))
        self.log_body = bool(getattr(settings, "audit_log_body", False))
        self.body_limit = int(getattr(settings, "audit_body_limit", 400))
        self.retention_days = int(getattr(settings, "audit_retention_days", 30))
        self.max_body = self.body_limit

    # ── 写入 ────────────────────────────────────────────────────

    def record(self, *, kind: str, model: str = "", caller: str = "",
               account_key: str = "", status: int = 0, ok: bool = True,
               prompt_tokens: int = 0, completion_tokens: int = 0,
               latency_ms: int = 0, error: str = "", stream: bool = False,
               user_text: str = "", extra: dict | None = None) -> None:
        """记一条审计。

        ⚠️ 本方法**绝不抛异常** —— 审计失败不能拖垮推理请求。
           任何写入错误都被吞掉（静默降级为「无审计」）。
        """
        if not self.enabled:
            return
        try:
            row = {
                "ts": time.time(),
                "kind": kind,                 # chat / models / error …
                "model": model,
                "caller": caller,
                "account_key": account_key,
                "status": status,
                "ok": bool(ok),
                "stream": bool(stream),
                "prompt_tokens": int(prompt_tokens or 0),
                "completion_tokens": int(completion_tokens or 0),
                "total_tokens": int(prompt_tokens or 0) + int(completion_tokens or 0),
                "latency_ms": int(latency_ms or 0),
            }
            if error:
                row["error"] = str(error)[:300]
            if self.log_body and user_text:
                row["user_text"] = str(user_text)[:self.max_body]
            if extra:
                row["extra"] = extra

            line = json.dumps(row, ensure_ascii=False, separators=(",", ":"))
            with _lock:
                with _open_append(_path(_today())) as f:
                    f.write(line + "\n")
        except Exception:
            pass                              # 审计永不阻断主流程

    # ── 读取 ────────────────────────────────────────────────────

    def _iter_day(self, day: str):
        p = _path(day)
        if not p.exists():
            return
        try:
            with p.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        yield json.loads(line)
                    except Exception:
                        continue          # 跳过损坏行，不中断
        except Exception:
            return

    def days(self) -> list[str]:
        """有哪些天的记录（倒序）。"""
        try:
            return sorted(
                (p.stem for p in _dir().glob("*.jsonl")),
                reverse=True)
        except Exception:
            return []

    def recent(self, limit: int = 100, *, ok: bool | None = None,
               model: str = "", caller: str = "") -> list[dict]:
        """最近 N 条（跨天，倒序）。支持按状态/模型/调用方过滤。"""
        out: list[dict] = []
        for day in self.days():
            for row in self._iter_day(day):
                if ok is not None and bool(row.get("ok")) != ok:
                    continue
                if model and row.get("model") != model:
                    continue
                if caller and row.get("caller") != caller:
                    continue
                out.append(row)
            if len(out) >= limit:
                break
        out.sort(key=lambda r: r.get("ts", 0), reverse=True)
        return out[:limit]

    def stats(self, days: int = 7) -> dict:
        """聚合统计 —— 仪表盘的数据源。

        返回：总量 / 成功率 / token 合计 / 按模型 / 按天 / 延迟分位
        """
        cutoff = time.time() - days * 86400
        total = ok_n = 0
        pt = ct = 0
        by_model: dict[str, dict] = {}
        by_day: dict[str, dict] = {}
        by_caller: dict[str, dict] = {}
        latencies: list[int] = []

        for day in self.days()[: days + 2]:
            for row in self._iter_day(day):
                ts = row.get("ts", 0)
                if ts < cutoff:
                    continue
                total += 1
                good = bool(row.get("ok"))
                ok_n += 1 if good else 0
                pt += int(row.get("prompt_tokens") or 0)
                ct += int(row.get("completion_tokens") or 0)
                lat = int(row.get("latency_ms") or 0)
                if lat:
                    latencies.append(lat)

                # by_model 只统计**有模型语义**的记录（chat/playground），
                # 避免 models 列表请求等无模型记录污染分布图
                kind = row.get("kind") or ""
                has_model = bool(row.get("model")) and kind in ("chat", "playground")
                buckets = [(by_caller, row.get("caller") or "-")]
                if has_model:
                    buckets.append((by_model, row.get("model")))
                for bucket, key in buckets:
                    b = bucket.setdefault(key, {"count": 0, "ok": 0, "tokens": 0})
                    b["count"] += 1
                    b["ok"] += 1 if good else 0
                    b["tokens"] += (int(row.get("prompt_tokens") or 0) +
                                    int(row.get("completion_tokens") or 0))

                dk = datetime.fromtimestamp(ts, timezone.utc).strftime("%m-%d")
                dd = by_day.setdefault(dk, {"count": 0, "ok": 0, "tokens": 0})
                dd["count"] += 1
                dd["ok"] += 1 if good else 0
                dd["tokens"] += (int(row.get("prompt_tokens") or 0) +
                                 int(row.get("completion_tokens") or 0))

        latencies.sort()

        def pct(p: float) -> int:
            if not latencies:
                return 0
            return latencies[min(len(latencies) - 1, int(len(latencies) * p))]

        return {
            "window_days": days,
            "total": total,
            "ok": ok_n,
            "failed": total - ok_n,
            "success_rate": round(ok_n / total * 100, 2) if total else 0.0,
            "prompt_tokens": pt,
            "completion_tokens": ct,
            "total_tokens": pt + ct,
            "latency_p50": pct(0.50),
            "latency_p95": pct(0.95),
            "latency_max": latencies[-1] if latencies else 0,
            "by_model": dict(sorted(by_model.items(),
                                    key=lambda kv: -kv[1]["count"])[:12]),
            "by_caller": dict(sorted(by_caller.items(),
                                     key=lambda kv: -kv[1]["count"])[:12]),
            "by_day": dict(sorted(by_day.items())),
            "days_available": self.days()[: days + 2],
        }

    # ── 清理 ────────────────────────────────────────────────────

    def purge(self) -> int:
        """删除超出保留期的分片，返回删除天数。"""
        try:
            keep = set()
            now = time.time()
            removed = 0
            for p in _dir().glob("*.jsonl"):
                try:
                    d = datetime.strptime(p.stem, "%Y-%m-%d").replace(
                        tzinfo=timezone.utc)
                    if now - d.timestamp() > self.retention_days * 86400:
                        p.unlink()
                        removed += 1
                    else:
                        keep.add(p.stem)
                except Exception:
                    continue
            return removed
        except Exception:
            return 0


audit = AuditLog()
