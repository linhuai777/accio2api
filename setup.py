#!/usr/bin/env python3
"""accio2api 配置向导 —— 一条命令问出 `.env`，不用手改示例文件。

用法：
    python setup.py              # 交互式配置
    python setup.py --start      # 配置完直接启动

为什么要这个东西：
    README 让部署者「cp .env.example .env && vim .env」，但 `.env.example`
    有 145 行、五大分组，第一次部署的人分不清哪些**必须**改、哪些能不动。
    结果是两类典型翻车：把必填项漏了（服务起来了但密钥没生效），
    或者改了不该改的（站点地址、浏览器参数）。

    向导只问**真正需要你决定**的那几项，其余用经过验证的默认值填好。

设计取舍：
    · **只用标准库**。本项目的安全定位是「依赖越少越好」——为了一百来行
      交互逻辑引入 questionary / rich，不值得。
    · **不覆盖已存在的环境变量**。生成的是 `.env` 文件，语义与
      core/config.py 的加载顺序一致：`export API_KEY=xxx` 仍能临时压过它。
    · **Ctrl+C 不留半成品**。所有提问完成后才落盘，中途退出不写任何文件
      ——半个 `.env` 比没有 `.env` 更危险（服务会用错误配置静默启动）。
"""

from __future__ import annotations

import argparse
import getpass
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from tui import ProgressBar, Spinner, run_quiet   # 同目录，见 tui.py

ROOT = Path(__file__).resolve().parent
ENV_PATH = ROOT / ".env"

# 终端颜色：非 TTY 或设置了 NO_COLOR 时自动关闭
_COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")

# `--yes`：全默认值、不提问。所有提问原语都要遵守它，
# 否则「不提问」的承诺只对部分问题成立，脚本化调用会卡在交互上。
_AUTO_YES = False


def _c(code: str, s: str) -> str:
    return f"\033[{code}m{s}\033[0m" if _COLOR else s


def bold(s: str) -> str:
    return _c("1", s)


def dim(s: str) -> str:
    return _c("2", s)


def cyan(s: str) -> str:
    return _c("36", s)


def green(s: str) -> str:
    return _c("32", s)


def red(s: str) -> str:
    return _c("31", s)


def section(title: str) -> None:
    print()
    print(cyan("── ") + bold(title))


def _read(prompt: str = "") -> str:
    """统一的输入入口，把 EOF 归一到 KeyboardInterrupt。

    为什么：标准输入耗尽（管道喂完 / Ctrl+D / CI 环境）时，裸 input()
    抛的是 EOFError，会带出一整屏 traceback —— 对「配置向导」来说
    这是最差的表现。统一转成 KeyboardInterrupt，由 main() 收成一句
    「已取消，未改动任何文件」。

    注意：异常前先补一个换行。否则提示串会留在最后一行没有终结，
    后续输出（尤其是报错）会与它粘在一起，读起来像是两条信息叠了。
    即使 prompt 为空也要补 —— 调用方可能已经把提示单独 print 出去了
    （见 ask_secret 的管道分支），此时行尾同样悬着。
    """
    try:
        return input(prompt)
    except EOFError:
        sys.stdout.write("\n")
        sys.stdout.flush()
        raise KeyboardInterrupt


# ══════════════════════════════════════════════════════════════════
# 提问原语
# ══════════════════════════════════════════════════════════════════


def ask(prompt: str, *, default: str = "", hint: str = "", validate=None) -> str:
    """问一个值。回车采用默认值；validate 返回非空错误串则重问。

    `--yes` 模式下不提问，直接取默认值 —— 该模式承诺「全默认、不提问」，
    若还在等输入，脚本化调用就会挂住。
    """
    if _AUTO_YES:
        return default
    if hint:
        print(dim("   " + hint))
    while True:
        tail = f" [{default}]" if default else ""
        raw = _read(cyan("? ") + bold(prompt) + tail + " ").strip()
        val = raw or default
        if validate:
            err = validate(val)
            if err:
                print(red("  ✗ " + err))
                continue
        return val


def ask_secret(prompt: str, *, hint: str = "") -> str:
    """问一个敏感值。

    终端下用 getpass —— 输入不回显（密钥不该留在屏幕和 scrollback 里）。
    非 TTY（管道 / CI）下 getpass 会直接抛错，此时退回普通读取，
    并且**提示与输入分成两行**：管道场景没有交互提示，若与上一行粘连
    会读不清在问什么。

    两种模式都恰好消耗一行输入 —— 这一点对脚本化测试很关键：
    行为不一致会让 `printf ... | python setup.py` 的输入串极难对齐。
    """
    if _AUTO_YES:
        return ""
    if hint:
        print(dim("   " + hint))
    if not sys.stdin.isatty():
        print(cyan("? ") + bold(prompt))
        sys.stdout.flush()
        return _read().strip()
    return getpass.getpass(cyan("? ") + bold(prompt) + " ").strip()


def ask_choice(prompt: str, options: list[tuple[str, str]], *, default: int = 1) -> str:
    """options = [(返回值, 显示文案)]，返回选中的返回值。"""
    if _AUTO_YES:
        return options[default - 1][0]
    print()
    print(bold(prompt))
    for i, (_, label) in enumerate(options, 1):
        mark = cyan("▸") if i == default else " "
        print(f"  {mark} {i}) {label}")
    while True:
        raw = _read(cyan("? ") + f"选 [1-{len(options)}] [{default}] ").strip()
        if not raw:
            return options[default - 1][0]
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return options[int(raw) - 1][0]
        print(red(f"  ✗ 请输入 1-{len(options)}"))


def ask_yesno(prompt: str, *, default: bool = False) -> bool:
    """是 / 否。

    同时接受 y/n 与 1/2 —— 本项目其它地方（取码方式、别名机制）用的是
    数字菜单，用户很容易顺手敲数字；只认字母会让人以为程序卡住了。
    1/2 映射到 y/n 的顺序与 `ask_choice` 的菜单渲染一致。
    """
    if _AUTO_YES:
        return default
    d = "1" if default else "2"
    while True:
        raw = _read(cyan("? ") + bold(prompt) + f" [1=是 / 2=否] [{d}] ").strip().lower()
        if not raw:
            return default
        if raw in ("y", "yes", "1"):
            return True
        if raw in ("n", "no", "0", "2"):
            return False
        print(red("  ✗ 请输入 1 或 2（也可以打 y / n）"))


# ══════════════════════════════════════════════════════════════════
# 校验
# ══════════════════════════════════════════════════════════════════

_RE_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_RE_DOMAIN = re.compile(
    r"^[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?"
    r"(\.[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?)+$")


def v_port(s: str) -> str:
    if not s.isdigit():
        return "请输入数字"
    if not (1 <= int(s) <= 65535):
        return "端口范围 1-65535"
    return ""


def v_required(s: str) -> str:
    return "" if s else "这一项不能为空"


def v_email(s: str) -> str:
    return "" if _RE_EMAIL.match(s) else "邮箱格式不对，例如 you@gmail.com"


def v_domain(s: str) -> str:
    return "" if _RE_DOMAIN.match(s) else "只填域名，不带协议，例如 api.example.com"


def v_key(s: str) -> str:
    """密钥强度：允许留空（留空=自动生成），填了就必须够长。"""
    if not s:
        return ""
    return "" if len(s) >= 16 else "太短了，至少 16 位（或直接回车让我生成）"


def gen_key() -> str:
    return secrets.token_hex(16)          # 32 字符，128 bit


def mask(s: str) -> str:
    if not s:
        return "—"
    return s[:6] + "…" + s[-4:] if len(s) > 12 else "…"


# ══════════════════════════════════════════════════════════════════
# 生成 .env
# ══════════════════════════════════════════════════════════════════


def build_env(c: dict) -> str:
    L: list[str] = []
    a = L.append

    a("# accio2api 配置 —— 由 setup.py 生成")
    a(f"# {datetime.now():%Y-%m-%d %H:%M:%S}")
    a("# 改完重启服务生效。已存在的环境变量优先级高于本文件（见 core/config.py）。")
    a("")

    a("# ── 服务 ──────────────────────────────────────────────")
    a("HOST=0.0.0.0")
    a(f"PORT={c['port']}")
    a("LOG_LEVEL=info")
    a("DATA_DIR=./data")
    a("")

    a("# ── 密钥 ──────────────────────────────────────────────")
    a("# API_KEY：任何 OpenAI 客户端 / New API 渠道填这个。")
    a(f"API_KEY={c['api_key']}")
    if c["admin_key"]:
        a("# ADMIN_KEY：登录 /admin 管理界面用。")
        a(f"ADMIN_KEY={c['admin_key']}")
    else:
        a("# ADMIN_KEY：留空 = 首次启动自动生成到 data/.admin_key")
    a("# SECRET_KEY：凭证加密主密钥。⚠️ 丢失后已存账号全部不可恢复，务必备份。")
    a("# 留空 = 首次启动自动生成到 data/.secret_key")
    a("")

    a("# ── Accio 站点（一般不改）─────────────────────────────")
    a("ACCIO_BASE_URL=https://www.accio.com")
    a("")

    a("# ── 浏览器（网页登录 / 自动注册必需）──────────────────")
    a("BROWSER_ENABLED=true")
    a("BROWSER_HEADLESS=true")
    a("")

    a("# ── 验证码后端：cloudmail | imap | manual ──────────────")
    a("# 详见 docs/EMAIL.md")
    a(f"OTP_BACKEND={c['otp']}")
    if c["otp"] == "imap":
        a(f"IMAP_HOST={c['imap_host']}")
        a("IMAP_PORT=993")
        a(f"IMAP_USER={c['imap_user']}")
        a(f"IMAP_PASSWORD={c['imap_pass']}")
        a("IMAP_SSL=1")
    elif c["otp"] == "cloudmail":
        a(f"CLOUDMAIL_BASE={c['cm_base']}")
        a(f"CLOUDMAIL_TOKEN={c['cm_token']}")
        a(f"CLOUDMAIL_DOMAIN={c['cm_domain']}")
    else:
        a("# manual：不自动取码 —— 网页登录时你亲自在画面上填验证码。")
    a("")

    if c["alias"]:
        a("# ── 邮箱别名（批量注册多账号用）────────────────────────")
        a(f"MAIL_PRIMARY={c['mail_primary']}")
        a(f"ALIAS_SCHEME={c['alias_scheme']}")
        if c["alias_scheme"] == "domain":
            a(f"ALIAS_DOMAIN={c['alias_domain']}")
        a("ALIAS_TAG_LENGTH=6")
        a("# 🔴 保持 false。开启后同一收件箱下多个账号会互相认领验证码。")
        a("ALIAS_ACCEPT_PRIMARY_FALLBACK=false")
        a("")

    if c["domain"]:
        a("# ── 公网访问 ──────────────────────────────────────────")
        a("# Host 白名单：防 DNS rebinding（见 main.py 的注释）")
        a(f"ALLOWED_HOSTS=localhost,127.0.0.1,{c['domain']}")
        a("# 跨域白名单：管理端需要跨域访问时才填")
        a(f"ADMIN_ORIGINS=https://{c['domain']}")
        a("")

    a("# ── 速率限制 ──────────────────────────────────────────")
    a("RATE_LIMIT_ENABLED=true")
    a("RATE_LIMIT_INFERENCE_RPM=120")
    a("RATE_LIMIT_ADMIN_RPM=60")
    a("RATE_LIMIT_LOGIN_PER_5MIN=10")
    a("")
    return "\n".join(L)


# ══════════════════════════════════════════════════════════════════
# 主流程
# ══════════════════════════════════════════════════════════════════


def main() -> int:
    ap = argparse.ArgumentParser(
        description="accio2api 配置向导",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例：\n  python setup.py\n  python setup.py --start")
    ap.add_argument("--start", action="store_true", help="写完 .env 直接启动服务")
    ap.add_argument("--yes", action="store_true", help="全默认值，不提问（用于脚本）")
    args = ap.parse_args()

    global _AUTO_YES
    _AUTO_YES = args.yes

    print()
    print(bold("  accio2api 配置向导"))
    print(dim("  只问必须你决定的那几项，其余填好默认值。Ctrl+C 可随时退出。"))

    # 目录不完整时提前说 —— 别等用户答完十个问题，最后才在启动那步失败
    _pre = preflight()
    if _pre and not args.start:
        print()
        print(red("  ! " + _pre.split("\n")[0]))
        print(dim("    仍可生成 .env，但之后请到完整项目目录里启动。"))

    c: dict = {}

    # ── 端口 ─────────────────────────────────────────────
    section("服务端口")
    c["port"] = ask("端口", default="8000", hint="客户端连的就是这个端口",
                    validate=v_port)

    # ── 对外密钥 ──────────────────────────────────────────
    section("对外推理密钥 API_KEY")
    print(dim("   客户端用它访问 /v1/*。留空我会生成一把强的（推荐）。"))
    if args.yes:
        c["api_key"] = gen_key()
    elif ask_yesno("自动生成？", default=True):
        c["api_key"] = gen_key()
    else:
        c["api_key"] = ask("输入你的密钥", validate=lambda s: v_required(s) or v_key(s))
    print(green("  ✓ ") + mask(c["api_key"]))

    # ── 管理密钥 ──────────────────────────────────────────
    section("管理端密钥 ADMIN_KEY")
    print(dim("   登录 /admin 用。留空 = 首次启动自动生成到 data/.admin_key（推荐）。"))
    if args.yes or ask_yesno("自动生成？", default=True):
        c["admin_key"] = ""
        print(green("  ✓ ") + "留空，启动时自动生成")
    else:
        c["admin_key"] = ask("输入你的密钥", validate=lambda s: v_required(s) or v_key(s))

    # ── 验证码后端 ────────────────────────────────────────
    section("验证码怎么取")
    print(dim("   网页登录 / 自动注册时需要。不确定就选 1，不需要额外配置。"))
    if args.yes:
        c["otp"] = "manual"
    else:
        c["otp"] = ask_choice("选择取码方式", [
            ("manual", "手动填码 —— 登录时你亲自在画面上输（最简单）"),
            ("imap", "IMAP 邮箱 —— 任何标准邮箱，最通用"),
            ("cloudmail", "自建 CloudMail —— 适合自己搭的收信服务"),
        ], default=1)

    if c["otp"] == "imap":
        section("IMAP 配置")
        print(dim("   Gmail / Outlook 要用「应用专用密码」，不是登录密码。"))
        c["imap_host"] = ask("IMAP 服务器", default="imap.gmail.com",
                             hint="Outlook 用 outlook.office365.com",
                             validate=v_required)
        c["imap_user"] = ask("邮箱账号", validate=v_email)
        c["imap_pass"] = ask_secret("应用专用密码")
    elif c["otp"] == "cloudmail":
        section("CloudMail 配置")
        print(dim("   注意：CloudMail 鉴权是裸 token，不带 Bearer 前缀。"))
        c["cm_base"] = ask("服务地址", hint="例如 https://mail.example.com",
                           validate=v_required)
        c["cm_token"] = ask_secret("Token")
        c["cm_domain"] = ask("收信域名", hint="例如 example.com", validate=v_domain)

    # ── 别名池 ────────────────────────────────────────────
    section("邮箱别名池（可选）")
    print(dim("   只有你要批量注册多个账号时才需要。单账号跳过即可。"))
    if args.yes:
        c["alias"] = False
    else:
        c["alias"] = ask_yesno("配置别名池？", default=False)

    if c["alias"]:
        c["mail_primary"] = ask("主邮箱", hint="别名由它派生", validate=v_email)
        c["alias_scheme"] = ask_choice("别名机制", [
            ("auto", "自动判定 —— 按域名猜（Gmail 走 plus，自有域名走 domain）"),
            ("plus", "you+tag@domain —— Gmail / Outlook 类"),
            ("domain", "tag@yourdomain —— 自有域名"),
        ], default=1)
        if c["alias_scheme"] == "domain":
            c["alias_domain"] = ask("收信域名", hint="例如 mail.example.com",
                                    validate=v_domain)
        else:
            c["alias_domain"] = ""
    else:
        c["mail_primary"] = c["alias_domain"] = ""

    # ── 公网 ──────────────────────────────────────────────
    section("是否暴露到公网")
    print(dim("   选「是」会配好 Host 白名单与跨域白名单，并提醒你加鉴权。"))
    if args.yes:
        c["domain"] = ""
    elif ask_yesno("要给公网访问？", default=False):
        c["domain"] = ask("域名", hint="只填域名，不带 https://，例如 api.example.com",
                          validate=v_domain)
    else:
        c["domain"] = ""

    # ── 回顾 ──────────────────────────────────────────────
    section("确认")
    rows = {
        "端口": c["port"],
        "API_KEY": mask(c["api_key"]),
        "ADMIN_KEY": "自动生成" if not c["admin_key"] else mask(c["admin_key"]),
        "取码方式": c["otp"],
        "别名池": ("是" if c["alias"] else "否"),
        "公网": (f"https://{c['domain']}" if c["domain"] else "仅本机访问"),
    }
    for k, v in rows.items():
        print(f"  {dim(k.ljust(10))} {v}")

    # 已存在 .env → 备份而非直接覆盖
    if ENV_PATH.exists():
        print()
        print(red(f"  ! 已存在 {ENV_PATH.name}"))
        if args.yes or ask_yesno("覆盖它？（原文件会备份到 .env.bak）", default=False):
            bak = ROOT / ".env.bak"
            shutil.copy2(ENV_PATH, bak)
            os.chmod(bak, 0o600)
            print(green(f"  ✓ 已备份 → {bak.name}"))
        else:
            print(dim("  已取消，未改动任何文件。"))
            return 1

    if not args.yes and not ask_yesno("写入 .env？", default=True):
        print(dim("  已取消，未改动任何文件。"))
        return 1

    ENV_PATH.write_text(build_env(c), encoding="utf-8")
    os.chmod(ENV_PATH, 0o600)
    print()
    print(green(f"  ✓ 已写入 {ENV_PATH}") + dim("  (权限 0600)"))

    # ── 收尾 ──────────────────────────────────────────────
    port = c["port"]
    print()
    print(bold("  下一步"))
    print(f"    启动      {cyan('docker compose up -d')}")
    print(f"    管理界面  {cyan(f'http://localhost:{port}/admin')}")
    print(f"    登录密钥  {dim('cat data/.admin_key')}")
    if c["domain"]:
        print()
        print(red("  ⚠️ 暴露公网前请务必："))
        print("     · 反代终结 TLS（本服务不提供 HTTPS）")
        print("     · 再加一层基础认证，别只靠 ADMIN_KEY")

    if args.start:
        return _start(c["port"])
    if not args.yes:
        print()
        if ask_yesno("现在启动服务？", default=False):
            return _start(c["port"])
    print()
    return 0


def preflight() -> str | None:
    """启动前检查：项目文件是否齐全。

    为什么要检查：向导在「没有 app/ 的目录」里也能跑完并写出 .env，
    然后 prompt「现在启动服务？」——用户答 y 就会拿一个注定 import
    失败的目录去启动 uvicorn，看到一屏 traceback。与其让用户自己
    从栈底猜，不如在这里说清楚。

    返回 None = 可以启动；返回字符串 = 不能启动的原因。
    """
    if not (ROOT / "app" / "main.py").exists():
        return (f"当前目录不是完整的 accio2api 项目（缺 app/main.py）：\n"
                f"      {ROOT}\n"
                f"    请先 clone 完整仓库，再运行向导。")
    return None


def _start(port: int) -> int:
    """启动服务。

    🔴 这里不把子进程输出直接放到控制台。
       `docker compose up -d` 首次运行会拉镜像，原始输出有几十到几百行
       （每层下载、每个容器创建），用户的终端会被刷满，真正的报错反而
       被淹没。改为：跑的时候只显示一行转圈，**失败时才**回放输出摘要。

       uvicorn 是前台常驻进程，不能用同一套（它会一直输出访问日志）——
       那种场景用户的预期就是「前台跑着」，日志本来就要看，所以直接交还
       终端控制权（exec 语义），并明确告诉他怎么停。
    """
    print()
    reason = preflight()
    if reason:
        print(red("  ✗ 无法启动："))
        print(dim("    " + reason))
        print(dim("\n  .env 已生成，可稍后在项目目录里手动启动："))
        print("      docker compose up -d")
        return 1

    if shutil.which("docker") and (ROOT / "docker-compose.yml").exists():
        ok, out = run_quiet(
            ["docker", "compose", "up", "-d"],
            what="启动容器（首次会拉取镜像，可能要几分钟）",
            show_output_on="fail", max_lines=20, cwd=ROOT)
        if not ok:
            return 1
        _report_up(port)
        return 0

    if shutil.which("uvicorn"):
        print(bold("  启动 uvicorn"))
        print(dim("    服务日志会直接输出到这里。停止：Ctrl+C"))
        print()
        try:
            return subprocess.call(["uvicorn", "app.main:app",
                                    "--host", "0.0.0.0", "--port", str(port)],
                                   cwd=ROOT)
        except KeyboardInterrupt:
            print()
            print(dim("  服务已停止。"))
            return 0

    print(red("  ✗ 既没有 docker 也没有 uvicorn。先装依赖："))
    print("      pip install -r requirements.txt")
    return 1


def _report_up(port: int) -> None:
    """启动后做一次存活探测，再给结论 —— 不让用户自己去猜。

    探测不通过时不谎报「运行中」：容器可能建起来了但应用崩了
    （端口占用、配置错误、依赖缺失都会这样）。这种情况必须说清楚，
    并给出查日志的命令，否则用户会对着一个死服务反复试接口。
    """
    import urllib.error
    import urllib.request

    url = f"http://127.0.0.1:{port}/health"
    ok = False
    with Spinner("等待服务就绪") as sp:
        for _ in range(20):                     # 最多等 10 秒
            try:
                with urllib.request.urlopen(url, timeout=1) as r:
                    ok = r.status == 200
                    break
            except (urllib.error.URLError, OSError, ValueError):
                time.sleep(0.5)
        if ok:
            sp.done("服务已就绪")
        else:
            sp.fail("服务未响应健康检查")

    print()
    if ok:
        print(bold("  运行中"))
        print(f"    管理界面  {cyan(f'http://localhost:{port}/admin')}")
        print(f"    接口地址  {cyan(f'http://localhost:{port}/v1')}")
        print(f"    查看日志  {dim('docker compose logs -f')}")
        print(f"    停止服务  {dim('docker compose down')}")
    else:
        print(bold("  容器已启动，但服务没起来"))
        print(dim("    多半是配置或端口问题。先看日志："))
        print(f"      {cyan('docker compose logs --tail=100')}")
        print(dim(f"\n    常见原因："))
        print(dim(f"      · 端口 {port} 被占用"))
        print(dim("      · .env 里有拼错的变量名"))
        print(dim("      · 首次启动需拉依赖，等一会儿再试"))


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n" + dim("  已取消，未改动任何文件。"))
        sys.exit(130)
