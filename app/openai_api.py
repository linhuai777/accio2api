"""OpenAI 兼容 API —— `/v1/chat/completions` + `/v1/models`。

映射关系（全部由 upstream/client.py 的逆向结论得来）：
    Accio `event/delta`.payload.contentDelta        → choices[].delta.content
    Accio `event/delta`.payload.reasoningDelta      → choices[].delta.reasoning_content
    Accio `event/finalize`.payload.finalContent     → 非流式时的完整回复
    Accio `event/finalize`.payload.executionUsage   → usage
    Accio `event/turn.end`.status == "ok"           → finish_reason="stop"

⚠ 一个语义差异必须处理：
    Accio 的对话是**有状态的**（conversationId）。OpenAI 客户端每次把**全量
    历史**发过来。本适配层把历史拍平成一条 query 发过去（Accio 端无需历史），
    每次请求用**新会话**，从而保持 OpenAI「无状态」语义。
    若要复用会话，客户端应在请求头带上 `X-Accio-Conversation-Id`。
"""

from __future__ import annotations

import hmac

import json
import time
import uuid
from typing import Iterable, Iterator

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .core.config import settings
from .core.credentials import pool
from .core.audit import audit, caller_id
from .core.persona import apply_override, guard_output
from .upstream.client import (AccioClient, delta_to_openai_chunk,  # noqa
                               finalize_to_usage)

router = APIRouter()


# ── 鉴权 ────────────────────────────────────────────────────
def _bearer(authorization: str | None, x_api_key: str | None) -> str:
    tok = ""
    if authorization and authorization.lower().startswith("bearer "):
        tok = authorization[7:].strip()
    elif x_api_key:
        tok = x_api_key.strip()
    return tok


def _raw_key(request: Request) -> str:
    """取出调用方密钥原文 —— **仅供审计折算哈希，绝不落盘明文**。

    审计只存 `caller_id()` 的结果（SHA-256 前 12 位），
    既可用于区分调用方，又无法反推密钥。
    """
    return _bearer(request.headers.get("authorization"),
                   request.headers.get("x-api-key"))


def _check_key(request: Request):
    """对外推理密钥校验。

    `API_KEY` 未配置时**放行**（本地/内网自用场景），但会记录；
    生产请务必设置 `API_KEY`。
    """
    if not settings.api_key:
        return
    tok = _bearer(request.headers.get("authorization"),
                  request.headers.get("x-api-key"))
    # 🔴 恒定时间比较（同 admin_guard，防时序侧信道）
    _api_ok = bool(settings.api_key) and hmac.compare_digest(
        tok, settings.api_key)
    _adm_ok = bool(settings.admin_key) and hmac.compare_digest(
        tok, settings.admin_key)
    if not (_api_ok or _adm_ok):
        raise HTTPException(status_code=401, detail={
            "error": {"message": "Invalid API key",
                      "type": "invalid_request_error", "code": "invalid_api_key"}})


# ── 凭证选择 ────────────────────────────────────────────────
def _pick_credential():
    creds = pool.enabled()
    if not creds:
        raise HTTPException(503, detail={
            "error": {"message": "No credential available. Add one in /admin.",
                      "type": "server_error", "code": "no_credential"}})
    # 简单策略：优先最近验证成功、且无错误的
    creds.sort(key=lambda c: (c.last_error != "", -c.last_verified_at))
    return creds[0]


def _build_client(cred) -> AccioClient:
    return AccioClient(
        user_id=cred.user_id, accio_id=cred.accio_id,
        cookies=dict(cred.cookies), agent_id=cred.agent_id,
        project_path=cred.project_path,
        account_key=cred.account_key)      # ← sticky 代理绑定用


# ── 消息拍平 ────────────────────────────────────────────────
def flatten_messages(messages: list[dict]) -> str:
    """把 OpenAI messages 拍平成一条 Accio query。

    为什么拍平：Accio 的 `sendQuery` 只接受单个 `question.query`，
    没有多轮消息数组。系统提示作为前置说明拼进去。

    `role=system` 的处理受 `SYSTEM_MESSAGE_POLICY` 控制：
      keep（默认）—— 拼进 query，保持 OpenAI 语义
      drop        —— 丢弃，防止调用者用 system 劫持反代背后的上游行为
    """
    parts: list[str] = []
    for m in messages or []:
        role = (m.get("role") or "user").lower()
        content = m.get("content")
        if isinstance(content, list):        # 多模态数组 → 取文本部分
            content = "".join(p.get("text", "") for p in content
                              if isinstance(p, dict))
        content = str(content or "").strip()
        if not content:
            continue
        if role == "system":
            if settings.system_message_policy == "drop":
                continue
            parts.append(f"[System instruction]\n{content}")
        elif role == "assistant":
            parts.append(f"[Assistant]\n{content}")
        elif role == "tool":
            parts.append(f"[Tool result]\n{content}")
        else:
            parts.append(content)
    return "\n\n".join(parts)


# ── /v1/models ──────────────────────────────────────────────
@router.get("/v1/models")
def list_models(request: Request):
    _check_key(request)
    try:
        cred = _pick_credential()
        models = _build_client(cred).fetch_models()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, detail={"error": {
            "message": f"upstream models fetch failed: {e}",
            "type": "server_error", "code": "upstream_error"}})
    audit.record(kind="models", caller=caller_id(_raw_key(request)),
                 status=200, ok=True,
                 extra={"model_count": len(models)})
    return {
        "object": "list",
        "data": [{
            "id": m["id"], "object": "model",
            "created": int(time.time()), "owned_by": "accio",
            "name": m.get("name"),
            "context_window": m.get("context_window"),
        } for m in models if m.get("id")],
    }


# ── /v1/chat/completions ────────────────────────────────────
@router.post("/v1/chat/completions")
async def chat_completions(request: Request):
    _check_key(request)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, detail={"error": {
            "message": "invalid JSON body", "type": "invalid_request_error"}})

    messages = body.get("messages") or []
    if not messages:
        raise HTTPException(400, detail={"error": {
            "message": "'messages' is required",
            "type": "invalid_request_error", "code": "missing_messages"}})

    stream = bool(body.get("stream"))
    model = body.get("model") or "auto"
    caller = caller_id(_raw_key(request))
    t_start = time.time()
    query = apply_override(flatten_messages(messages))
    conv_hint = request.headers.get("x-accio-conversation-id", "")
    resp_id = "chatcmpl-" + uuid.uuid4().hex[:24]
    created = int(time.time())

    cred = _pick_credential()
    client = _build_client(cred)

    # ── 流式 ────────────────────────────────────────
    if stream:
        # OpenAI 规范：流式响应默认**不返回** usage；仅当请求显式带
        # `stream_options:{"include_usage":true}` 时，才在 `[DONE]` 之前
        # 追加一个 `choices:[]` 的独立 usage 帧。旧实现无条件把 usage
        # 塞进 finish 帧，导致严格客户端解析错位。
        _include_usage = bool(
            (body.get("stream_options") or {}).get("include_usage"))

        def gen() -> Iterator[str]:
            _t0 = t_start
            # `buf` 只保留**尾部窗口**：guard_output 检测的是固定句式，
            # 命中位置必在结尾附近；全量累积会让单请求内存随输出线性
            # 增长（且 CPython str `+=` 是 O(n) 拷贝）。
            _TAIL = 8192
            _streamed = {"len": 0, "usage": None, "err": "", "buf": ""}
            _aborted = False
            # 首帧：role
            yield _sse({"id": resp_id, "object": "chat.completion.chunk",
                        "created": created, "model": model,
                        "choices": [{"index": 0,
                                     "delta": {"role": "assistant",
                                               "content": ""},
                                     "finish_reason": None}]})
            usage = None
            try:
                for kind, obj in client.stream_query(
                        query, model=model, conversation_id=conv_hint,
                        language="zh"):
                    if kind == "delta":
                        _p = obj.get("payload") or {}
                        _streamed["buf"] = (
                            _streamed["buf"] + (_p.get("contentDelta") or "")
                        )[-_TAIL:]
                        ch = delta_to_openai_chunk(obj, model, resp_id)
                        if ch:
                            _streamed["len"] += len(
                                json.dumps(ch, ensure_ascii=False))
                            yield _sse(ch)
                    elif kind == "finalize":
                        usage = finalize_to_usage(obj)
                        cid = (obj.get("payload") or {}).get("conversationId")
                        if cid:
                            yield _sse({"id": resp_id,
                                        "object": "chat.completion.chunk",
                                        "created": created, "model": model,
                                        "choices": [],
                                        "x_accio_conversation_id": cid})
                    elif kind == "err":
                        msg = json.dumps(obj, ensure_ascii=False)[:400]
                        pool.mark_error(cred.account_key, msg)
                        _streamed["err"] = msg
                        audit.record(kind="chat", model=model, caller=caller,
                                     account_key=cred.account_key, status=502,
                                     ok=False, stream=True, error=msg,
                                     user_text=query,
                                     latency_ms=int((time.time() - _t0) * 1000),
                                     extra={"finish": "upstream_err"})
                        # 帧内错误：补全 `code` 并遵循 `[DONE]` 终止语义，
                        # 否则只认 `[DONE]` 的客户端会一直挂到超时。
                        yield _sse({"error": {"message": msg, "type": "server_error",
                                              "code": "upstream_error",
                                              "param": None}})
                        yield "data: [DONE]\n\n"
                        return
            except GeneratorExit:
                # 客户端主动断开：不写审计（避免噪声），但要让 WS 关闭
                # 传播到上游 —— client.stream_query 自带 finally。
                _aborted = True
                raise
            except Exception as e:
                _msg = f"{type(e).__name__}: {str(e)[:300]}"
                pool.mark_error(cred.account_key, _msg)
                _streamed["err"] = _msg
                audit.record(kind="chat", model=model, caller=caller,
                             account_key=cred.account_key, status=502, ok=False,
                             stream=True, error=_msg, user_text=query,
                             latency_ms=int((time.time() - _t0) * 1000),
                             extra={"finish": "exception"})
                yield _sse({"error": {"message": _msg, "type": "server_error",
                                      "code": "upstream_error", "param": None}})
                yield "data: [DONE]\n\n"
                return
            finally:
                # 无论正常/异常/断开都记录「总算力消耗」并刷新验证时间，
                # 避免审计出现「有开始无结束」的悬空记录。
                if _aborted:
                    audit.record(kind="chat", model=model, caller=caller,
                                 account_key=cred.account_key, status=499,
                                 ok=False, stream=True, error="client_abort",
                                 user_text=query,
                                 latency_ms=int((time.time() - _t0) * 1000),
                                 extra={"finish": "client_abort",
                                        "out_len": _streamed["len"]})

            pool.mark_verified(cred.account_key,
                               credits=cred.credits)   # 刷新验证时间
            if not _streamed["err"]:
                # 输出侧人设兜底：流式内容已经逐字发出，无法撤回；
                # 若终稿命中身份泄露，补发一个 x_final 帧让客户端覆盖渲染。
                _final_text, _leak = guard_output(_streamed["buf"])
                if _leak:
                    yield _sse({"id": resp_id,
                                "object": "chat.completion.chunk",
                                "created": created, "model": model,
                                "choices": [],
                                "x_final": _final_text})
                audit.record(kind="chat", model=model, caller=caller,
                             account_key=cred.account_key, status=200, ok=True,
                             stream=True, user_text=query,
                             prompt_tokens=(usage or {}).get("prompt_tokens", 0),
                             completion_tokens=(usage or {}).get("completion_tokens", 0),
                             latency_ms=int((time.time() - _t0) * 1000),
                             extra={"finish": "stop", "out_len": _streamed["len"]})
            # finish 帧：规范要求 choices 非空、usage **不**在此帧
            yield _sse({"id": resp_id, "object": "chat.completion.chunk",
                        "created": created, "model": model,
                        "choices": [{"index": 0, "delta": {},
                                     "finish_reason": "stop"}]})
            # usage 独立帧（仅当客户端要求）
            if _include_usage and usage:
                yield _sse({"id": resp_id, "object": "chat.completion.chunk",
                            "created": created, "model": model,
                            "choices": [], "usage": usage})
            yield "data: [DONE]\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache",
                                          "X-Accel-Buffering": "no"})

    # ── 非流式 ──────────────────────────────────────
    text, reasoning, usage, err = "", "", None, ""
    try:
        for kind, obj in client.stream_query(query, model=model,
                                             conversation_id=conv_hint,
                                             language="zh"):
            if kind == "delta":
                p = obj.get("payload") or {}
                text += p.get("contentDelta") or ""
                reasoning += p.get("reasoningDelta") or ""
            elif kind == "finalize":
                usage = finalize_to_usage(obj)
                fc = (obj.get("payload") or {}).get("finalContent")
                if fc and len(fc) > len(text):
                    text = fc
            elif kind == "err":
                err = json.dumps(obj, ensure_ascii=False)[:400]
                break
    except Exception as e:
        err = str(e)[:400]

    if err:
        # ⚠️ 不能只在「完全没吐字」时报错：若上游先吐一半正文再报错，
        #    `err and not text` 会为假 → 调用方拿到**残缺文本 + stop**
        #    却浑然不觉。因此只要有 err 就判失败。
        pool.mark_error(cred.account_key, err)
        audit.record(kind="chat", model=model, caller=caller,
                     account_key=cred.account_key, status=502, ok=False,
                     latency_ms=int((time.time() - t_start) * 1000),
                     error=err, stream=False, user_text=query,
                     extra={"finish": "error", "partial_out_len": len(text)})
        raise HTTPException(502, detail={"error": {
            "message": err, "type": "server_error",
            "code": "upstream_error", "param": None}})

    pool.mark_verified(cred.account_key, credits=cred.credits)
    text, leak_hit = guard_output(text)
    if leak_hit:
        reasoning = ""          # 推理链里往往已经写了内部细节，一并抹掉
    audit.record(kind="chat", model=model, caller=caller,
                 account_key=cred.account_key, status=200, ok=True,
                 prompt_tokens=(usage or {}).get("prompt_tokens", 0),
                 completion_tokens=(usage or {}).get("completion_tokens", 0),
                 latency_ms=int((time.time() - t_start) * 1000),
                 stream=False, user_text=query,
                 extra={"finish": "stop", "out_len": len(text)})
    msg: dict = {"role": "assistant", "content": text}
    if reasoning:
        msg["reasoning_content"] = reasoning
    return JSONResponse({
        "id": resp_id, "object": "chat.completion", "created": created,
        "model": model,
        "choices": [{"index": 0, "message": msg, "finish_reason": "stop"}],
        "usage": usage or {"prompt_tokens": 0, "completion_tokens": 0,
                           "total_tokens": 0},
    })


def _sse(obj: dict) -> str:
    return "data: " + json.dumps(obj, ensure_ascii=False) + "\n\n"
