"""运行时配置 —— 全部来自环境变量，零硬编码。

设计原则（本项目是给**别人部署**的开源服务）：
  1. **不写死任何站点标识**：sandbox 主机、agentId、accountId 全部运行时动态获取。
     开发调试时抓到的那些值（具体会话的 `DID-xxx` / 实例 ID）
     属于**运行时数据**，绝不能进仓库 —— 别人的账号拿到它们毫无意义。
  2. **不写死任何凭证**：cookie / token 一律来自 credentials 存储。
  3. 所有可变项都有合理默认值，`docker compose up` 能直接跑起来。
"""

from __future__ import annotations

import logging
import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("accio2api.config")


# ══════════════════════════════════════════════════════════════════
# .env 自动加载
# ══════════════════════════════════════════════════════════════════
# 为什么需要：README 让部署者「改 .env 里的这两行」，但如果进程
# 启动时没人读 `.env`，改了完全不生效 —— 用户会看到服务用默认值
# 跑起来、密钥对不上，且毫无提示。这是最伤开箱体验的一类问题。
#
# 实现取舍：**自己解析，不引入 python-dotenv**。
#   · `.env` 格式足够简单（KEY=VALUE + 注释 + 引号）
#   · 少一个依赖 = 少一处供应链风险、少一次装包失败
#   · 语义严格对齐 docker compose 的 env_file：**已存在的环境变量
#     优先**（不覆盖），这样 `ADMIN_KEY=xxx docker compose up` 这类
#     临时覆盖仍然有效
#
# 查找顺序：$ACCIO_ENV_FILE → ./.env → 包上一级/.env
def _load_dotenv() -> int:
    """把 `.env` 灌进 os.environ（**不覆盖已有变量**）。返回载入条数。"""
    candidates = []
    custom = os.environ.get("ACCIO_ENV_FILE")
    if custom:
        candidates.append(Path(custom))
    candidates.append(Path.cwd() / ".env")
    candidates.append(Path(__file__).resolve().parent.parent.parent / ".env")

    for path in candidates:
        try:
            if not path.is_file():
                continue
            n = 0
            for raw in path.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("export "):
                    line = line[7:].strip()
                if "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                if not key or not key.replace("_", "").isalnum():
                    continue
                val = val.strip()
                # 成对引号剥掉（保留内部空白，密码可能含空格）
                if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
                    val = val[1:-1]
                # 已存在的环境变量优先 —— 不覆盖
                if key not in os.environ:
                    os.environ[key] = val
                    n += 1
            return n
        except Exception:
            continue        # .env 损坏不应导致启动失败
    return 0


_DOTENV_LOADED = _load_dotenv()


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


def _env_bool(key: str, default: bool = False) -> bool:
    v = _env(key)
    if not v:
        return default
    return v.lower() in ("1", "true", "yes", "on")


def _env_int(key: str, default: int) -> int:
    try:
        return int(_env(key) or default)
    except ValueError:
        return default


def _env_float(key: str, default: float) -> float:
    try:
        return float(_env(key) or default)
    except ValueError:
        return default


# ── Accio 站点（这是产品入口，属于公开信息，可以写默认值）──────────
DEFAULT_BASE_URL = "https://www.accio.com"
DEFAULT_LOGIN_URL = (
    "https://www.accio.com/login?embed=1"
    "&parent_origin=https%3A%2F%2Fwww.accio.com&channel=accio_work_web"
)


@dataclass
class Settings:
    # ── 服务 ──────────────────────────────────────────────
    host: str = field(default_factory=lambda: _env("HOST", "0.0.0.0"))
    port: int = field(default_factory=lambda: _env_int("PORT", 8000))
    log_level: str = field(default_factory=lambda: _env("LOG_LEVEL", "info"))

    # ── 鉴权 ──────────────────────────────────────────────
    # 管理端密钥：首次启动若未设置则**自动生成并落盘**，避免"默认密钥"上线
    admin_key: str = field(default_factory=lambda: _env("ADMIN_KEY"))
    # 对外推理密钥（New API 里填这个）；为空则不下发，只能走管理端签发
    api_key: str = field(default_factory=lambda: _env("API_KEY"))
    # 凭证加密密钥；未设置则自动生成落盘（0600）
    secret_key: str = field(default_factory=lambda: _env("SECRET_KEY"))
    # CORS 白名单（逗号分隔）。留空 = 不启用 CORS（同源部署够用）。
    # 需要跨域访问管理端时显式配置，不要用 "*"。
    admin_origins: str = field(default_factory=lambda: _env("ADMIN_ORIGINS"))
    # Host 头白名单（防 DNS rebinding 的**纵深防御**，不是主防线）。
    #
    # 🔴 默认 `*`（不校验）。为什么不用「localhost,127.0.0.1」做默认：
    #    本服务最常见的部署方式是「局域网/公网 IP 直连」，而 Host 白名单是
    #    **精确匹配**——用 IP 访问时 Host 是 `1.2.3.4`，不在白名单里就返回
    #    `Invalid host header`，用户连自己的后台都进不去，且必须改 .env
    #    重启服务才能自救。把「安全增强项」做成「默认把人锁在门外」，
    #    净效果是负的。
    #
    #    真正拦截未授权访问的是 `_admin_guard`（每个 /admin/api/* 都强制
    #    恒定时间比对的密钥）。DNS rebinding 即使成功，攻击者 JS 也拿不到
    #    管理密钥，读不到数据。Host 白名单只在「密钥已泄露 + 存在 rebinding
    #    通道」时才提供额外价值——那是很窄的场景。
    #
    # 什么时候该设：服务绑在**域名**后面（反代终结 TLS）时，
    #    设 `ALLOWED_HOSTS=your.domain`（支持 `*.your.domain` 子域通配）。
    #    只用 IP 访问就别设，或者把 IP 一起写进去。
    allowed_hosts: str = field(
        default_factory=lambda: _env("ALLOWED_HOSTS", "*"))
    # 可信反代 IP / CIDR 列表（逗号分隔）。**仅当**直连来源落在此列表内，
    # 才采信 X-Forwarded-For / X-Real-IP 判定客户端 IP；否则一律用 socket
    # 对端地址。留空 = 不信任何转发头（默认，最安全）。
    # 例：TRUSTED_PROXIES=127.0.0.1,::1,10.0.0.0/8
    trusted_proxies: str = field(default_factory=lambda: _env("TRUSTED_PROXIES"))

    # ── 邮箱别名（批量注册的核心机制）─────────────────────────
    # 主邮箱 = 真实收件箱，也是 IMAP 登录账号。留空 = 不用别名（单邮箱模式）。
    # 配了它之后，一次注册可派生出 N 个独立别名地址，全部投递到这一个
    # 收件箱 —— 上游看到 N 个身份，我们只需维护一套邮箱凭证。
    mail_primary: str = field(default_factory=lambda: _env("MAIL_PRIMARY"))
    # 别名机制：plus（Gmail/Outlook 类 +tag 子地址）/ domain（自有域名）/ auto
    alias_scheme: str = field(
        default_factory=lambda: _env("ALIAS_SCHEME", "auto"))
    # 仅 domain 模式需要：别名使用的自有域名（如 mail.example.com）
    alias_domain: str = field(default_factory=lambda: _env("ALIAS_DOMAIN"))
    # 别名标签长度（越大越不容易撞车，也越不易被人肉关联）
    alias_tag_length: int = field(
        default_factory=lambda: _env_int("ALIAS_TAG_LENGTH", 6))
    # 上游把别名改写回主地址时是否仍认。
    # ⚠️ 默认 **False（关闭）**：开启后只要收件箱里出现一封投给**主地址**
    #    的邮件，**所有**别名账号都会认领它 → 跨账号偷码（批量注册时
    #    A 账号会拿到 B 账号的验证码）。仅当你确认上游会改写地址、
    #    且不存在多账号并发注册时才开启。
    alias_accept_primary_fallback: bool = field(
        default_factory=lambda: _env_bool("ALIAS_ACCEPT_PRIMARY_FALLBACK", False))

    # ── 上游沙箱域名提示 ─────────────────────────────────────
    # 用于「嗅探 WS 地址」时的**辅助匹配子串**（真正的判据是路径
    # `/websocket/connect`，这里只是提高命中率）。
    #
    # 为什么做成配置而不是写死：
    #   上游（阿里无影 agentbay）若换域名，用户改一行 .env 即可，
    #   无需改代码。多个候选用逗号分隔。
    # ⚠ 这是**公开的上游服务标识**，不含任何个人/实例信息；
    #   每个账号的真实实例 ID 由运行时动态获取，绝不硬编码。
    sandbox_host_hints: str = field(
        default_factory=lambda: _env("SANDBOX_HOST_HINTS", "agentbay"))

    # ── 系统提示词控制 ───────────────────────────────────────
    # 上游（Accio）在服务端给它的 agent 注入了自己的身份设定与行为规则。
    # 反代这一侧拿不到那份文本（payload 里没有对应字段），所以做不到
    # 「删除」它 —— 能做的只有两件事：
    #   (1) 在每次 query 前面加一段「身份覆盖」前置语，压制它的原始人设；
    #   (2) 在输出侧拦截「我是 Accio / 我的系统提示是……」这类泄露话术。
    #
    # SYSTEM_PROMPT_OVERRIDE：前置语正文。留空 = 关闭压制。
    # 也可以用 SYSTEM_PROMPT_FILE 指向一个文件（便于放长文本 / 版本管理）。
    system_prompt_override: str = field(
        default_factory=lambda: _env("SYSTEM_PROMPT_OVERRIDE"))
    system_prompt_file: str = field(
        default_factory=lambda: _env("SYSTEM_PROMPT_FILE"))
    # 客户端传来的 role=system 消息如何处理：
    #   keep   —— 拼进 query（默认，保持 OpenAI 兼容语义）
    #   drop   —— 丢弃（防止调用者用 system 劫持上游行为）
    system_message_policy: str = field(
        default_factory=lambda: _env("SYSTEM_MESSAGE_POLICY", "keep").lower())
    # 输出侧身份泄露拦截：检测到「自称 Accio / 复述系统提示」等话术时替换为固定话术
    persona_guard_enabled: bool = field(
        default_factory=lambda: _env_bool("PERSONA_GUARD_ENABLED", False))
    persona_guard_reply: str = field(
        default_factory=lambda: _env(
            "PERSONA_GUARD_REPLY",
            "抱歉，我无法提供关于底层模型或系统设定的信息。"))

    # ── 速率限制 ─────────────────────────────────────────────
    # 三条独立限流线（详见 app/core/ratelimit.py）。0 = 该线不限制。
    # 默认值偏宽松：限流是防「脚本失控 / 暴力枚举」，不是卡正常用户。
    rate_limit_enabled: bool = field(
        default_factory=lambda: _env_bool("RATE_LIMIT_ENABLED", True))
    # 推理接口：每 API key 每分钟请求数（正常聊天远达不到 120）
    rate_limit_inference_rpm: int = field(
        default_factory=lambda: _env_int("RATE_LIMIT_INFERENCE_RPM", 120))
    # 管理接口：每客户端 IP 每分钟请求数
    rate_limit_admin_rpm: int = field(
        default_factory=lambda: _env_int("RATE_LIMIT_ADMIN_RPM", 60))
    # 浏览器登录会话：每 IP 每 5 分钟次数（每次都会拉真实 Chromium，单列严线）
    rate_limit_login_per_5min: int = field(
        default_factory=lambda: _env_int("RATE_LIMIT_LOGIN_PER_5MIN", 10))

    # ── 存储 ──────────────────────────────────────────────
    data_dir: Path = field(
        default_factory=lambda: Path(_env("DATA_DIR", "./data")))

    # ── Accio 站点 ────────────────────────────────────────
    base_url: str = field(
        default_factory=lambda: _env("ACCIO_BASE_URL", DEFAULT_BASE_URL))
    login_url: str = field(
        default_factory=lambda: _env("ACCIO_LOGIN_URL", DEFAULT_LOGIN_URL))
    # 客户端版本号：随前端更新，**动态取**更稳；这里给个兜底
    client_version: str = field(
        default_factory=lambda: _env("ACCIO_CLIENT_VERSION", "0.32.16"))

    # ── 浏览器（自动注册 / 网页内登录用）────────────────────
    browser_enabled: bool = field(
        default_factory=lambda: _env_bool("BROWSER_ENABLED", True))
    browser_headless: bool = field(
        default_factory=lambda: _env_bool("BROWSER_HEADLESS", True))
    browser_timeout_s: int = field(
        default_factory=lambda: _env_int("BROWSER_TIMEOUT_S", 180))
    # 登录会话存活时间（用户在这个窗口内完成登录）
    login_session_ttl_s: int = field(
        default_factory=lambda: _env_int("LOGIN_SESSION_TTL_S", 600))

    # ── 验证码后端 ────────────────────────────────────────
    # cloudmail | imap | manual
    otp_backend: str = field(
        default_factory=lambda: _env("OTP_BACKEND", "cloudmail").lower())
    # CloudMail
    cloudmail_base: str = field(
        default_factory=lambda: _env("CLOUDMAIL_BASE"))
    cloudmail_token: str = field(
        default_factory=lambda: _env("CLOUDMAIL_TOKEN"))
    cloudmail_email: str = field(
        default_factory=lambda: _env("CLOUDMAIL_EMAIL"))
    cloudmail_password: str = field(
        default_factory=lambda: _env("CLOUDMAIL_PASSWORD"))
    cloudmail_domain: str = field(
        default_factory=lambda: _env("CLOUDMAIL_DOMAIN"))
    otp_poll_timeout_s: int = field(
        default_factory=lambda: _env_int("OTP_POLL_TIMEOUT_S", 120))

    # ── 运维 ──────────────────────────────────────────────────
    # 健康巡检并发度（过高会给上游造成压力，过低则巡检慢）
    health_concurrency: int = field(
        default_factory=lambda: _env_int("HEALTH_CONCURRENCY", 6))
    # 全局 RPM 限制（0 = 不限）；对外网关的兜底保护
    global_rpm_limit: int = field(
        default_factory=lambda: _env_int("GLOBAL_RPM_LIMIT", 0))

    # ── 调用审计 ──────────────────────────────────────────────
    # 结构化记录每次 /v1/* 调用（JSONL 按天分片，零依赖）
    audit_enabled: bool = field(
        default_factory=lambda: _env_bool("AUDIT_ENABLED", True))
    audit_dir: str = field(default_factory=lambda: _env("AUDIT_DIR", "audit"))
    # ⚠️ 默认不记消息正文（隐私 + 体积）。开启后正文截断到 audit_body_limit
    audit_log_body: bool = field(
        default_factory=lambda: _env_bool("AUDIT_LOG_BODY", False))
    audit_body_limit: int = field(
        default_factory=lambda: _env_int("AUDIT_BODY_LIMIT", 400))
    audit_retention_days: int = field(
        default_factory=lambda: _env_int("AUDIT_RETENTION_DAYS", 30))

    # ── IMAP 收码（OTP_BACKEND=imap 时必填）───────────────────
    # Gmail / Outlook 必须用**应用专用密码**（服务商已禁用账号密码直连）。
    imap_host: str = field(default_factory=lambda: _env("IMAP_HOST"))
    imap_port: int = field(default_factory=lambda: _env_int("IMAP_PORT", 993))
    imap_user: str = field(default_factory=lambda: _env("IMAP_USER"))
    imap_password: str = field(default_factory=lambda: _env("IMAP_PASSWORD"))

    # ── 上游调用 ──────────────────────────────────────────
    upstream_timeout_s: float = field(
        default_factory=lambda: _env_float("UPSTREAM_TIMEOUT_S", 180.0))
    # 单凭证串行（Accio 的会话模型是「一个账号一个沙箱」，天然串行）
    max_concurrency_per_credential: int = field(
        default_factory=lambda: _env_int("MAX_CONCURRENCY_PER_CREDENTIAL", 1))

    # ── 代理 / IP 轮换 ────────────────────────────────────
    # 静态代理列表，逗号分隔：http://127.0.0.1:<port-a>,http://127.0.0.1:<port-b>
    proxies: str = field(default_factory=lambda: _env("PROXIES"))
    # 单代理（PROXIES 为空时生效）
    proxy: str = field(default_factory=lambda: _env("PROXY"))
    # 策略：sticky（同凭证同出口，默认）| rotate（每次换）| random
    proxy_strategy: str = field(
        default_factory=lambda: _env("PROXY_STRATEGY", "sticky").lower())
    # mihomo / clash 控制 API —— 支持「同端口动态换 IP」
    mihomo_api: str = field(default_factory=lambda: _env("MIHOMO_API"))
    mihomo_group: str = field(
        default_factory=lambda: _env("MIHOMO_GROUP"))
    # mihomo 主端口（被策略组切换的那条链路）。留空 = 取 PROXIES 第一项。
    mihomo_proxy_url: str = field(
        default_factory=lambda: _env("MIHOMO_PROXY_URL"))
    # 出口 IP 探测服务
    proxy_check_url: str = field(
        default_factory=lambda: _env("PROXY_CHECK_URL",
                                     "https://api.ipify.org"))
    # 代理是否也用于「自动注册 / 网页登录」的浏览器
    proxy_for_browser: bool = field(
        default_factory=lambda: _env_bool("PROXY_FOR_BROWSER", True))

    def __post_init__(self):
        self.data_dir = Path(self.data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._load_or_create_secrets()

    # ── 自动生成并持久化密钥 ──────────────────────────────
    def _load_or_create_secrets(self):
        sk_file = self.data_dir / ".secret_key"
        if not self.secret_key:
            if sk_file.exists():
                self.secret_key = sk_file.read_text().strip()
            else:
                self.secret_key = secrets.token_urlsafe(32)
                sk_file.write_text(self.secret_key)
                try:
                    sk_file.chmod(0o600)
                except OSError:
                    pass

        # 🔴 弱密钥校验：SECRET_KEY 是凭证加密的**唯一密钥材料**。
        #    如果部署方在 .env 里写了 "changeme" / "123456" 这种，
        #    加密等于没加。不足 32 字符直接拒绝启动。
        if len(self.secret_key) < 32:
            raise RuntimeError(
                f"SECRET_KEY 太短（{len(self.secret_key)} 字符，需 ≥32）。"
                " 凭证加密强度取决于它。生成一个："
                "python -c \"import secrets;print(secrets.token_urlsafe(32))\"")

        # ── ADMIN_KEY：不再自动生成 ──────────────────────────────
        # 设计变更（执剑人 2026-09-23 指定）：
        #   旧行为：ADMIN_KEY 留空 → 首次启动自动生成到 data/.admin_key。
        #   问题  ：用户根本不知道密钥长什么样，得进服务器 cat 文件才知道，
        #          而且「自动生成」给人一种「已经配置好了」的错觉。
        #   新行为：ADMIN_KEY 必须由部署者显式设定（写进 .env 或环境变量）。
        #          没设就拒绝启动，并把生成命令打出来。
        #
        #   向后兼容：若 data/.admin_key 已存在（老部署残留），仍读取它，
        #           但打一条警告提示尽快迁移到 .env。
        if not self.admin_key:
            ak_file = self.data_dir / ".admin_key"
            if ak_file.exists():
                legacy = ak_file.read_text().strip()
                if legacy:
                    self.admin_key = legacy
                    log.warning(
                        "ADMIN_KEY 来自旧版遗留文件 %s —— 建议改为写进 .env"
                        "（该文件内容：%s）", ak_file, legacy)
            if not self.admin_key:
                raise RuntimeError(
                    "ADMIN_KEY 未设置。管理后台 /admin 需要一个你自己定的密钥。\n"
                    "  生成一个并写入 .env：\n"
                    "    python -c \"import secrets;print('sk-admin-'+secrets.token_urlsafe(24))\"\n"
                    "  然后加一行到 .env：  ADMIN_KEY=<上面输出的值>\n"
                    "  或者重新运行向导：    python setup.py")

    @property
    def credentials_dir(self) -> Path:
        d = self.data_dir / "credentials"
        d.mkdir(parents=True, exist_ok=True)
        return d


settings = Settings()
