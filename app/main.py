"""accio2api —— Accio Work ↔ OpenAI 兼容 API 网关。

对外暴露：
    /v1/models              模型列表
    /v1/chat/completions    OpenAI 兼容（含 SSE 流式）
    /admin                  管理端网页（添加凭证 / 监控 / 用量）
    /admin/api/*            管理端接口

启动：
    uvicorn app.main:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import logging
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import admin_api, openai_api
from .core.config import settings
from .core.credentials import pool
from .core.ratelimit import RateLimiter, middleware as ratelimit_middleware

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
log = logging.getLogger("accio2api")

app = FastAPI(
    title="accio2api",
    description="Accio Work ↔ OpenAI compatible API gateway",
    version="0.1.0",
)

# 🔴 中间件顺序（顺序即正确性，勿随意调整）：
#    Starlette 后注册的中间件在**外层**。要让被限流的 429 也带上 CORS 头
#    （浏览器否则读不到 `Retry-After`，无法自动退避），必须让 CORS 在外层
#    —— 即 **先注册限流，后注册 CORS**。
#    旧顺序（限流最后注册 = 最外层）会让 429 短路在 CORS 之前，实测丢头。
#    中间件内部通过 app.state.ratelimiter 取实例（startup 时初始化）。
app.middleware("http")(ratelimit_middleware)

# 🔴 Host 头校验 —— 防 DNS Rebinding 的纵深防御。
#    攻击者把自己的域名解析到本服务 IP，受害者浏览器即可用该域名访问
#    `/admin`（Host 头是攻击者域名，CORS 拦不住「同源」的 rebinding）。
#
#    但这是**第二道**防线，主防线是 `_admin_guard` 的密钥校验。
#    默认 `*` 不校验：Host 白名单是精确匹配，用 IP 访问时 Host 就是 IP，
#    默认收窄会让用户连自己的后台都进不去（`Invalid host header`），
#    且必须改 .env 重启才能自救 —— 代价大于收益。
#
#    设了值（非 `*`）才启用。用域名部署时建议设上。
_hosts = [h.strip() for h in (settings.allowed_hosts or "").split(",") if h.strip()]
if _hosts and _hosts != ["*"]:
    log.info("Host 白名单已启用: %s", _hosts)

    # 🔴 不用 Starlette 的 TrustedHostMiddleware —— 它被拦时只回一句
    #    裸文本 `Invalid host header`（400），既不说为什么也不说怎么改。
    #    而且它是在**中间件层直接返回**的，不抛异常，所以
    #    `@app.exception_handler(400)` 根本收不到，无法事后补提示。
    #    自己写一个：匹配逻辑一致（支持 `*.example.com` 子域通配），
    #    但拦截时把「当前 Host / 白名单 / 改法」一并返回，让用户能自救。
    import re as _re

    _host_pats = []
    for _p in _hosts:
        if _p == "*":
            _host_pats.append(_re.compile(r".*"))
        elif _p.startswith("*."):
            # `*.example.com` 匹配任意深度的子域，但不匹配裸 example.com
            # —— 与 Starlette 语义保持一致。
            _host_pats.append(_re.compile(
                r"^[^.]+(\.[^.]+)*\." + _re.escape(_p[2:]) + r"$"))
        else:
            _host_pats.append(_re.compile(r"^" + _re.escape(_p) + r"$"))

    @app.middleware("http")
    async def _host_guard(request, call_next):
        # Host 头可能带端口（`example.com:8000`），比对前剥掉
        raw = request.headers.get("host", "")
        host = raw.split(":")[0].lower()
        if host and not any(p.match(host) for p in _host_pats):
            from fastapi.responses import JSONResponse as _JSON
            return _JSON({
                "error": {
                    "message": f"Host 头 '{raw}' 不在白名单里",
                    "type": "invalid_request_error",
                    "code": "invalid_host_header",
                    "allowed_hosts": _hosts,
                    "hint": ("服务启用了 ALLOWED_HOSTS（精确匹配）。"
                             "把当前访问用的 Host 加进去，"
                             "或清空 ALLOWED_HOSTS 以关闭校验。"),
                    "fix": f"编辑 .env → ALLOWED_HOSTS="
                           f"{','.join(_hosts)},{host}  → 重启服务",
                }
            }, status_code=400)
        return await call_next(request)
else:
    log.info("Host 白名单未启用（ALLOWED_HOSTS=*）—— "
             "主防线为管理密钥校验；绑域名部署时建议设 ALLOWED_HOSTS=你的域名")

# 🔴 CORS：默认不放通配。
#    `/admin/api/*` 是凭证管理接口（能读 cookie、能删账号），
#    `allow_origins=["*"]` 意味着任意网站都能拿用户的浏览器去打这些接口。
#    虽然本服务用 Bearer 而非 Cookie 鉴权（CSRF 风险较低），但
#    「通配 + 管理接口」仍是不必要的暴露面 —— 需要跨域的部署方
#    显式配置 ADMIN_ORIGINS 白名单。
_cors = [o.strip() for o in (settings.admin_origins or "").split(",") if o.strip()]
if _cors:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors,
        allow_credentials=False,      # 本服务不用 Cookie 会话，保持 False
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-API-Key"],
    )
    log.info("CORS 白名单: %s", _cors)
else:
    # 未配置 → 不启用 CORS 中间件（同源部署时无需跨域）
    log.info("CORS 未启用（同源部署；跨域请在 .env 设 ADMIN_ORIGINS）")

app.include_router(openai_api.router)
app.include_router(admin_api.router)

STATIC = Path(__file__).resolve().parents[1] / "static"


def _key_fp(k: str) -> str:
    """密钥指纹：只够比对"是不是同一把"，不足以还原任何字符。"""
    import hashlib
    return hashlib.sha256((k or "").encode()).hexdigest()[:8]


@app.on_event("startup")
def _startup():
    log.info("accio2api starting")
    log.info("  data_dir      = %s", settings.data_dir)
    log.info("  otp_backend   = %s", settings.otp_backend)
    log.info("  browser       = %s (headless=%s)",
             settings.browser_enabled, settings.browser_headless)
    log.info("  credentials   = %d", len(pool.all()))

    # ── 速率限制 ─────────────────────────────────────────────
    # 三条独立限流线（推理 / 管理 / 浏览器登录），详见 core/ratelimit.py
    app.state.ratelimiter = RateLimiter(
        enabled=settings.rate_limit_enabled,
        inference_rpm=settings.rate_limit_inference_rpm,
        admin_rpm=settings.rate_limit_admin_rpm,
        login_per_5min=settings.rate_limit_login_per_5min,
    )
    if settings.rate_limit_enabled:
        log.info("  限流          = 推理 %d/min·key | 管理 %d/min·IP | "
                 "登录 %d/5min·IP",
                 settings.rate_limit_inference_rpm,
                 settings.rate_limit_admin_rpm,
                 settings.rate_limit_login_per_5min)
    else:
        log.warning("  限流          = 已关闭（RATE_LIMIT_ENABLED=false）")

    if not settings.api_key:
        log.warning("  API_KEY 未设置 —— /v1/* 对外接口将不校验密钥。"
                    "生产环境请在 .env 中设置 API_KEY。")

    # 🔴 公开示例密钥检测：本仓库的 .env.example / docker-compose.yml 里
    #    带了开箱可用的示例密钥（ADMIN_KEY=test 等），方便快速测试。
    #    但如果部署方 clone 后没改就直接上公网，等于门户大开 ——
    #    示例值全世界都能在 GitHub 上看到。
    #    这里**不阻止启动**（测试场景需要它），只把警告打在启动日志最显眼处。
    _SAMPLE_KEYS = {"test", "sk-accio-please-change-me",
                    "sk-9Nh77kKgVaQHr2BrK8HDgWU3Oz1r9W41LaGmoKntkyUjYKXA"}
    if (settings.admin_key or "") in _SAMPLE_KEYS:
        log.warning("=" * 62)
        log.warning("  ⚠️  正在使用【公开示例】ADMIN_KEY —— 任何人都能登录 /admin！")
        log.warning("      该值写在公开仓库的 .env.example 里，仅用于测试。")
        log.warning("      正式部署请改 .env：")
        log.warning("        python -c \"import secrets;print('sk-admin-'"
                    "+secrets.token_urlsafe(24))\"")
        log.warning("=" * 62)
    if (settings.api_key or "") in _SAMPLE_KEYS:
        log.warning("  ⚠️  正在使用【公开示例】API_KEY —— 会被别人拿去白嫖配额。"
                    "正式部署请改 .env。")

    # 🔴 不回显密钥本身，只给指纹（前 8 位 sha256）。
    #    原写法 `admin_key[:12]` 对自动生成的 41 字符密钥无碍（暴露 3 个
    #    随机字符 ≈ 残留 174 bit），但**部署方可能自定义短密钥** ——
    #    若 `ADMIN_KEY=prod2024`，[:12] 就等于把密钥完整打进日志。
    #    指纹足以让运维比对"是不是同一个密钥"，又不泄露任何字符。
    log.info("  admin_key     = <sha256:%s>（长度 %d）",
             _key_fp(settings.admin_key), len(settings.admin_key or ""))


@app.get("/health")
def health():
    return {"status": "ok", "time": int(time.time()),
            "credentials": len(pool.all()),
            "version": "0.1.0"}


@app.get("/")
def index():
    f = STATIC / "index.html"
    if f.exists():
        return FileResponse(f)
    return JSONResponse({"name": "accio2api", "docs": "/docs",
                         "admin": "/admin", "health": "/health"})


@app.get("/admin")
def admin_page():
    f = STATIC / "admin.html"
    if f.exists():
        return FileResponse(f)
    return JSONResponse({"error": "admin UI not installed"}, status_code=404)


@app.exception_handler(StarletteHTTPException)
async def _http_exc(request: Request, exc: StarletteHTTPException):
    """把 HTTPException 的 detail 提升为 OpenAI 规范的**顶层** `error`。

    默认 Starlette 会输出 `{"detail": {...}}`，而 OpenAI 契约是
    `{"error": {...}}`（且含 `type` / `code` / `param`）。客户端（官方
    SDK、New API、Cherry Studio 等）按后者解析，不改就会出现
    「能取到 detail 但读不到 error.message」的兼容性问题。
    """
    d = exc.detail
    if isinstance(d, dict) and "error" in d and isinstance(d["error"], dict):
        err = dict(d["error"])           # 🔴 拷贝，不原地改 exc.detail
    elif isinstance(d, dict):
        err = dict(d)
    else:
        err = {"message": str(d)}
    # 不原地 setdefault —— 若 detail 是模块级复用的常量字典，
    # 第一次调用会把 type 永久固化，后续 5xx 会误报 invalid_request_error。
    err.setdefault("type", "invalid_request_error" if exc.status_code < 500
                   else "server_error")
    err.setdefault("code", None)
    err.setdefault("param", None)
    return JSONResponse({"error": err}, status_code=exc.status_code)


@app.exception_handler(Exception)
async def _unhandled(request: Request, exc: Exception):
    """🔴 对外绝不回显异常原文（可能含内部路径 / 参数 / 凭证片段）。
    完整堆栈只进日志，响应体用 request_id 关联排查。
    """
    rid = uuid.uuid4().hex[:12]
    log.exception("unhandled [%s] %s %s: %s",
                  rid, request.method, request.url.path, exc)
    return JSONResponse(
        {"error": {"message": "internal server error", "type": "server_error",
                   "code": "internal_error", "param": None,
                   "request_id": rid}},
        status_code=500)
