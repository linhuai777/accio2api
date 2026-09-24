"""自动化登录内核 —— 邮箱 + Baxia 滑块 + 验证码，产出可用 cookie。

这是「模式二 自动注册」与「模式一 网页登录」共用的底座。

滑块实现要点（血泪，别删注释）：
  1. Accio 用的是**阿里 NC 滑块**（`#nc_1_n1z` 把手 42×30），
     不是阿里云验证码 2.0（`aliyunCaptcha-*`），选择器完全不同。
  2. 🔴 **坐标必须换算**：滑块在 iframe 里，`getBoundingClientRect()`
     返回的是 **frame 本地坐标**，而 `page.mouse` 用的是**顶层视口坐标**。
     Baxia iframe 通常定位在页面中央（实测偏移 (510, 290)），
     不换算的话鼠标点在 iframe 外面 —— **滑块纹丝不动，且不报错**。
  3. 拖动要**拟人**：贝塞尔曲线 + ease-out + 垂直微抖 + 不均匀步间延时。
     一步到位必被风控拒。
     ⚠️ 2026-09-23 实测：上面的贝塞尔版连续 3 次全败（无头 1 + 有头 2），
     轨迹/步频特征被判 bot；改用**人肉节奏**（见 `_human_drag`）后一次通过。
"""

from __future__ import annotations

import math
import random
import re
import time
from dataclasses import dataclass, field

from ..core.config import settings
from .otp import OtpAuthError

# ── NC 滑块选择器 ───────────────────────────────────────────
SEL_HANDLE = "#nc_1_n1z"
SEL_TRACK = "#nc_1_n1t"
SEL_TEXT = "#nc_1__scale_text"

_PASS_MARKERS = ("验证通过", "通过验证", "验证成功")
_CODE_PAGE_MARKERS = ("输入验证码", "请输入发送至此邮箱的验证码")


def detect_slider(frame) -> dict | None:
    """在 frame 内检测 NC 滑块，返回把手/轨道几何（**frame 本地坐标**）。"""
    try:
        return frame.evaluate("""() => {
            const h = document.querySelector('#nc_1_n1z');
            const t = document.querySelector('#nc_1_n1t');
            const x = document.querySelector('#nc_1__scale_text');
            if (!h || !t) return null;
            const hb = h.getBoundingClientRect(), tb = t.getBoundingClientRect();
            return {handle:{x:hb.x,y:hb.y,w:hb.width,h:hb.height},
                    track:{x:tb.x,y:tb.y,w:tb.width,h:tb.height},
                    text: x ? (x.innerText||'') : ''};
        }""")
    except Exception:
        return None


def frame_offset(page, frame) -> tuple[float, float]:
    """iframe 在顶层视口中的偏移 —— 见模块顶部要点 2。

    🔴 2026-09-24 修复三层嵌套双重叠加：Playwright 的
    ElementHandle.bounding_box() 本身就返回**相对主视口**的坐标（文档
    明示），对 frame_element 取一次 box 即已包含全部祖先 iframe 偏移。
    旧实现沿 parent_frame 链逐层累加，两层场景（punish 直接嵌在主页）
    恰好只加一次所以没炸；三层场景（桥内页 → punish iframe）中间层被
    加两次，拖动坐标系统性偏移 → 滑块从未被真正抓住（实测文案纹丝不动）。
    """
    if frame == page.main_frame:
        return 0.0, 0.0
    try:
        el = frame.frame_element()
        if el is None:
            return 0.0, 0.0
        box = el.bounding_box()
        if box:
            return box["x"], box["y"]
    except Exception:
        pass
    return 0.0, 0.0


def _bezier_drag(page, x0, y0, x1, y1, steps, wobble=1.6):
    """贝塞尔拟人拖动（已弃用，保留备用）。

    ⚠️ 2026-09-23 实测被 Baxia 判 bot：24~80 步 ease-out + 正弦变速步频
    连续 3 次全败。生产路径已换 `_human_drag`，此函数仅存档。
    """
    dx, dy = x1 - x0, y1 - y0
    if math.hypot(dx, dy) < 1.0:
        return
    amp = min(6.0, math.hypot(dx, dy) * 0.04)
    c1x = x0 + dx * 0.30 + random.uniform(-amp, amp)
    c1y = y0 + dy * 0.30 + random.uniform(-wobble, wobble)
    c2x = x0 + dx * 0.72 + random.uniform(-amp, amp)
    c2y = y0 + dy * 0.72 + random.uniform(-wobble, wobble)
    for i in range(1, steps + 1):
        t = i / steps
        e = 1 - (1 - t) ** 2.2                     # ease-out：先快后慢
        bx = ((1-e)**3*x0 + 3*(1-e)**2*e*c1x + 3*(1-e)*e**2*c2x + e**3*x1)
        by = ((1-e)**3*y0 + 3*(1-e)**2*e*c1y + 3*(1-e)*e**2*c2y + e**3*y1)
        page.mouse.move(bx, by)
        base = 8 + 14 * math.sin(math.pi * t)
        time.sleep((base + random.uniform(-3, 6)) / 1000.0)


def _human_drag(page, x0: float, y0: float, x1: float, y1: float, steps: int = 40):
    """人肉节奏拖动 —— 2026-09-23 实测一次通过（贝塞尔版 3 连败后的替换者）。

    与贝塞尔版的本质差异（风控看的就是这些信号）：
      1. **匀速慢节奏**：固定步数 40 步 × 18ms 均匀步频，总时长 ~0.7s，
         不做 ease-out 加速 —— 机器爱加速，人手是近似匀速的小幅抖动；
      2. **毫米级垂直抖动**：±0.5px 的 y 向微颤（i%3-1 三值循环），
         模拟手指握持不稳，贝塞尔版 ±1.6px 的"拟人弧线"反而太光滑；
      3. **起手与按下有停顿**：move 到把手后停 0.4s、mouse.down 后停 0.3s，
         是真人「看到滑块→伸手→按住→拖」的反应链，机器没有。
    """
    n = max(24, min(60, steps))
    for i in range(1, n + 1):
        page.mouse.move(x0 + (x1 - x0) * i / n, y0 + (i % 3 - 1) * 0.5)
        time.sleep(0.018)


def solve_slider(frame, page, *, max_tries: int = 3) -> bool:
    """拖动 NC 滑块至通过。返回是否通过。

    「滑块消失」= 通过：验证码 iframe 在验证完成后会被风控端拆除，
    这是它消失的唯一正常原因；循环开头发现滑块不在同理（无事可做，
    或已被同流程上一次拖动解决）。
    🔴 2026-09-24 修复假阴性：此前「拖动成功 → 下轮循环滑块已消失 →
       return False」，调用方拿到失败而实际已通过（实测复现：
       验证码正常下发、登录成功，函数却报 False）。
    """
    for _ in range(max_tries):
        info = detect_slider(frame)
        if not info:
            time.sleep(1.0)
            info = detect_slider(frame)
            if not info:
                return True   # 滑块不在 = 无滑块可解（含已通过的拆框态）
        hb, tb = info["handle"], info["track"]
        ox, oy = frame_offset(page, frame)         # ⭐ 必须换算
        sx = ox + hb["x"] + hb["w"] / 2
        sy = oy + hb["y"] + hb["h"] / 2
        over = random.uniform(4, 9)
        ex = ox + tb["x"] + tb["w"] - hb["w"] * 0.15 + over
        ey = sy + random.uniform(-1.2, 1.2)

        page.mouse.move(sx + random.uniform(-30, 30), sy + random.uniform(-14, 14))
        time.sleep(random.uniform(0.15, 0.35))
        page.mouse.move(sx + random.uniform(-3, 3), sy, steps=random.randint(4, 8))
        time.sleep(random.uniform(0.4, 0.6))      # 起手停顿（实测 0.4s 关键值）
        page.mouse.down()
        time.sleep(random.uniform(0.3, 0.45))     # 按下停顿（实测 0.3s 关键值）
        # 🔴 生产路径用 _human_drag：贝塞尔版实测 3 连败被判 bot（2026-09-23）
        _human_drag(page, sx, sy, ex, ey)
        time.sleep(random.uniform(0.3, 0.45))     # 拖完停顿，松手前人手会顿一下
        page.mouse.up()

        time.sleep(random.uniform(1.2, 2.2))
        try:
            txt = frame.evaluate(
                "() => { const e = document.querySelector('#nc_1__scale_text');"
                " return e ? (e.innerText||'') : ''; }")
        except Exception:
            txt = ""
        if any(m in (txt or "") for m in _PASS_MARKERS) or detect_slider(frame) is None:
            return True
        time.sleep(random.uniform(1.0, 1.8))
    return False


# ── 登录流程 ────────────────────────────────────────────────
@dataclass
class LoginResult:
    ok: bool = False
    cookies: dict[str, str] = field(default_factory=dict)
    error: str = ""
    stage: str = ""


def _click_any(page, names: tuple[str, ...], timeout: int = 8000) -> bool:
    """按钮文案随语言变（英文 Continue / 中文「继 续」），逐个试。"""
    for name in names:
        try:
            page.get_by_role("button", name=name).click(timeout=timeout)
            return True
        except Exception:
            continue
    return False


def _page_has_any(page, names: tuple[str, ...]) -> bool:
    """页面上是否还能看到这些文案之一（用于判定「还停在验证态」）。

    仅做即时可见性检查，不等待，用于区分「首击被风控拦截」与「首击已成功」。
    """
    for name in names:
        try:
            if page.get_by_text(name, exact=False).first.is_visible():
                return True
        except Exception:
            continue
    return False


def login_with_browser(email: str, *, otp_provider=None,
                       code_callback=None,
                       headless: bool | None = None,
                       on_stage=None) -> LoginResult:
    """完整自动登录：填邮箱 → 过滑块 → 取码 → 提交 → 导出 cookie。

    Args:
        email: 登录邮箱
        otp_provider: 有 `fetch_code(email, after_ts=...)` 的对象；None = 手动模式
        code_callback: 手动模式下的取码回调 `() -> str | None`（阻塞等待用户输入）
        headless: 是否无头
        on_stage: 阶段回调 `on_stage(stage, detail)`，供 UI 展示进度
    """
    from playwright.sync_api import sync_playwright

    def stage(s, d=""):
        if on_stage:
            try:
                on_stage(s, d)
            except Exception:
                pass

    hl = settings.browser_headless if headless is None else headless
    res = LoginResult()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=hl, args=[
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox", "--disable-dev-shm-usage",
        ])
        ctx = browser.new_context(
            viewport={"width": 1440, "height": 900}, locale="zh-CN",
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/131.0.0.0 Safari/537.36"))
        page = ctx.new_page()
        try:
            stage("open", "打开登录页")
            page.goto(settings.login_url, wait_until="domcontentloaded",
                      timeout=60000)
            page.wait_for_timeout(4500)

            stage("email", "填写邮箱")
            page.locator("input[name=email]").fill(email)
            page.wait_for_timeout(700)
            if not _click_any(page, ("继 续", "Continue")):
                res.error = "未找到「继续」按钮"; res.stage = "email"; return res
            page.wait_for_timeout(4000)

            stage("send_code", "请求验证码")
            _click_any(page, ("发送验证码", "Send code"))
            page.wait_for_timeout(3500)

            # 滑块（可能不触发）
            #
            # 🔴 时序陷阱（2026-09-23 第二次踩到，别再删）：
            #   点「发送验证码」时若命中 Baxia 风控，**请求会被拦截，邮件不会发出**，
            #   只弹一个滑块。滑块本身只是「解锁」，**过完必须再点一次发送按钮**，
            #   验证码才会真正投递。
            #   漏掉这一步的表现是：「滑块成功」+「收件箱无新邮件」+ 最终报
            #   「未获取到验证码」—— 极易误判成收码后端故障（本轮实测）。
            #
            # ⚠ L-1 修正（同日审计发现）：补点必须**只在确认首击被拦截时**执行，
            #   否则若首击其实已成功发码、滑块只是并行弹出的额外交互，补点
            #   会变成第二次真实发码 → 徒增风控触发概率。
            fr = next((f for f in page.frames if detect_slider(f)), None)
            if fr:
                stage("slider", "正在通过滑块验证")
                passed = solve_slider(fr, page, max_tries=3)
                page.wait_for_timeout(2500)
                if not passed and detect_slider(fr) is not None:
                    # L-2：滑块确实没过去 → 必须显式报错，不能静默落入取码
                    #      阶段（否则最终只报「未获取到验证码」，掩盖真实原因）
                    stage("slider", "滑块验证未通过")
                    res.error = "滑块验证未通过（可能被上游风控拦截）"
                    res.stage = "slider"
                    return res
                # 仅当首击确实被风控拦下（页面仍停留在验证/发送态）才补点
                if _page_has_any(page, ("请完成验证", "拖动滑块", "安全验证",
                                        "重新发送", "发送验证码", "Send code")):
                    stage("send_code", "滑块已通过，重新请求验证码")
                    _click_any(page, ("发送验证码", "Send code", "重新发送"))
                    page.wait_for_timeout(3500)

            # 取码
            stage("otp", "等待验证码")
            code = None
            if code_callback is not None:
                code = code_callback()
            elif otp_provider is not None:
                try:
                    code = otp_provider.fetch_code(
                        email, after_ts=time.time() - 30,
                        timeout_s=settings.otp_poll_timeout_s)
                except OtpAuthError as e:
                    # 收码后端认证失败 —— 必须如实上报，否则会被误读为
                    # 「邮件没来」而反复重试（本轮实测踩过：CloudMail
                    # token 失效耗时 5 分钟才定位）。
                    res.error = f"收码后端认证失败：{e}"
                    res.stage = "otp"
                    return res
            if not code:
                res.error = "未获取到验证码"; res.stage = "otp"; return res

            stage("submit", "提交验证码")
            filled = False
            for sel in ("input[name=code]", "input[placeholder*=验证码]",
                        "input[maxlength='6']"):
                try:
                    page.locator(sel).first.fill(str(code), timeout=4000)
                    filled = True
                    break
                except Exception:
                    continue
            if not filled:
                res.error = "未找到验证码输入框"; res.stage = "submit"; return res
            page.wait_for_timeout(600)
            _click_any(page, ("继 续", "Continue", "登录", "Sign in"))
            page.wait_for_timeout(8000)

            stage("export", "导出登录态")
            cookies = {c["name"]: c["value"] for c in ctx.cookies()
                       if "accio" in (c.get("domain") or "")}
            if not cookies:
                res.error = "登录后未获得 cookie"; res.stage = "export"; return res
            res.ok = True
            res.cookies = cookies
            res.stage = "done"
        except Exception as e:
            res.error = str(e)[:300]
        finally:
            try:
                browser.close()
            except Exception:
                pass
    return res
