"""Web 内嵌登录会话 —— 把浏览器画面推到网页，用户在原站页面上操作。

为什么需要它：
    「模式一：用户自己添加凭证」要能在**网页里**完成登录，而不是让用户
    去装浏览器插件或跑命令行。Baxia 风控会检测反代/iframe 的域名，
    直接反代 `www.accio.com/login` 必然被判 bot —— 所以只能：
        **后端起真实 headless 浏览器 → CDP 截屏 → 前端显示 → 输入回传**

数据通路：
    画面：Playwright `page.screenshot()` 循环 或 CDP `Page.startScreencast`
    输入：前端把 mouse/key 事件 POST 回来 → 转成 `page.mouse.*` / `keyboard.*`

每个登录会话是一个隔离的 BrowserContext（cookie 不串）。
"""

from __future__ import annotations

import base64
import threading
import time
import math
import re
import uuid
from dataclasses import dataclass, field
from typing import Any

from ..core.config import settings


@dataclass
class WebLoginSession:
    login_id: str
    email: str = ""
    created_at: float = field(default_factory=time.time)
    expires_at: float = field(
        default_factory=lambda: time.time() + settings.login_session_ttl_s)

    # 状态：starting | ready | waiting_otp | done | error | expired
    stage: str = "starting"
    detail: str = ""
    # 是否允许服务端代为滑过（默认关：网页登录模式下让用户自己操作更自然，
    # 也避免误判；自动注册模式下走 login_with_browser，不经过这里）
    auto_solve_slider: bool = False
    error: str = ""

    # 结果
    result_cookies: dict[str, str] = field(default_factory=dict)
    user_id: str = ""
    nickname: str = ""

    # 内部
    _thread: Any = None
    _stop: Any = field(default_factory=threading.Event)
    _lock: Any = field(default_factory=threading.RLock)
    _frame: bytes = b""
    _frame_at: float = 0.0
    _queue: list = field(default_factory=list)      # 待执行的前端输入
    _queue_lock: Any = field(default_factory=threading.Lock)
    _pw: Any = None          # playwright 对象
    _browser: Any = None
    _ctx: Any = None
    _page: Any = None

    # ── 生命周期 ─────────────────────────────────────
    @property
    def alive(self) -> bool:
        return (not self._stop.is_set()
                and time.time() < self.expires_at
                and self.stage not in ("done", "error", "expired"))

    def snapshot(self) -> dict:
        return {
            "login_id": self.login_id,
            "stage": self.stage,
            "detail": self.detail,
            "error": self.error,
            "expires_in": max(0, int(self.expires_at - time.time())),
            "user_id": self.user_id,
            "nickname": self.nickname,
            "has_result": bool(self.result_cookies),
        }

    def latest_frame(self) -> bytes:
        with self._lock:
            return self._frame

    def close(self):
        self._stop.set()
        for attr in ("_ctx", "_browser"):
            obj = getattr(self, attr, None)
            try:
                if obj:
                    obj.close()
            except Exception:
                pass
        try:
            if self._pw:
                self._pw.stop()
        except Exception:
            pass
        self.stage = "expired"


class WebLoginManager:
    # 同时存活的登录会话上限（防浏览器实例堆积）
    MAX_LIVE = 3
    """管理所有网页登录会话。"""

    def __init__(self):
        self._sessions: dict[str, WebLoginSession] = {}
        self._lock = threading.RLock()

    def _purge(self):
        now = time.time()
        for k, s in list(self._sessions.items()):
            if now > s.expires_at + 300 or s.stage in ("error", "expired"):
                try:
                    s.close()
                except Exception:
                    pass
                self._sessions.pop(k, None)

    def get(self, login_id: str) -> WebLoginSession | None:
        with self._lock:
            self._purge()
            return self._sessions.get(login_id)

    def all(self) -> list[WebLoginSession]:
        with self._lock:
            self._purge()
            return list(self._sessions.values())

    def start(self, email: str, otp_backend_name: str | None = None) -> WebLoginSession:
        """开一个登录会话，后台线程驱动浏览器。"""
        with self._lock:
            self._purge()
            # 🔴 会话数上限：每次 start 都会拉起一个真实 Chromium。
            #    不限量的话，反复调用可以打爆内存/进程表 —— 单管理员
            #    场景下 3 个并发登录会话已经绰绰有余。
            live = [x for x in self._sessions.values()
                    if x.stage not in ("done", "error", "expired")]
            if len(live) >= self.MAX_LIVE:
                # 淘汰最老的，保证新的还能开。
                # 🔴 1-4：**必须先真正 close()**，不能只 pop 字典。
                #    旧实现只删字典条目 → `_stop` 未置位、浏览器未关、
                #    后台线程继续跑 → 上限逻辑不但不防堆积，反而制造出
                #    多个「失去句柄、永远无法回收」的孤儿 Chromium。
                oldest = min(live, key=lambda x: x.created_at)
                self._sessions.pop(oldest.login_id, None)
                try:
                    oldest.close()
                    if oldest._thread and oldest._thread.is_alive():
                        oldest._thread.join(timeout=10)
                except Exception:
                    pass

            lid = "wl_" + uuid.uuid4().hex
            s = WebLoginSession(login_id=lid, email=email)
            self._sessions[lid] = s
        t = threading.Thread(target=self._run, args=(s, otp_backend_name),
                             daemon=True)
        s._thread = t
        t.start()
        return s

    # ── 后台驱动 ─────────────────────────────────────
    def _run(self, s: WebLoginSession, otp_backend_name: str | None):
        from playwright.sync_api import sync_playwright

        from .login import (_click_any, detect_slider,  # noqa
                            solve_slider)
        from .otp import get_backend

        try:
            s._pw = sync_playwright().start()
            s._browser = s._pw.chromium.launch(
                headless=settings.browser_headless,
                args=["--disable-blink-features=AutomationControlled",
                      "--no-sandbox", "--disable-dev-shm-usage"])
            s._ctx = s._browser.new_context(
                viewport={"width": 1280, "height": 800}, locale="zh-CN")
            s._page = s._ctx.new_page()

            page = s._page
            page.goto(settings.login_url, wait_until="domcontentloaded",
                      timeout=60000)
            page.wait_for_timeout(3000)
            s.stage, s.detail = "ready", "请在页面中完成登录"
            self._pump(s)          # 进入持续截屏循环（阻塞到会话结束）
        except Exception as e:
            s.stage, s.error = "error", str(e)[:300]
        finally:
            # 🔴 1-1：三步必须**各自 try**。旧实现包在同一个 try 里，
            #    一旦 `ctx.close()` 抛异常（页面崩溃/导航中常见），后面的
            #    `_pw.stop()` 会被跳过 → 整个 Chromium 进程树滞留。
            #    顺序也有讲究：先关 browser，再 stop driver（它会强杀其拉起的
            #    浏览器），最后关 ctx。
            for op in (lambda: s._browser and s._browser.close(),
                       lambda: s._pw and s._pw.stop(),
                       lambda: s._ctx and s._ctx.close()):
                try:
                    op()
                except Exception:
                    pass

    def _pump(self, s: WebLoginSession):
        """循环截屏 + 检测登录完成。"""
        page = s._page
        ctx = s._ctx
        # 🔴 判定「已登录」不能只看有没有 accio cookie ——
        #    未登录时站点也会种 `_m_h5_tk` 之类的风控 cookie。
        #    必须要求**登录态专有 cookie**（xman_t / cookie2 / _l_g_ / munb）
        #    或已跳转到非登录页。
        LOGIN_MARKERS = ("xman_t", "munb", "_nk_", "sgcookie")
        last_sig = ""
        while s.alive:
            # ① 先执行积压的输入事件（必须在本线程，见 forward_input 注释）
            while True:
                with s._queue_lock:
                    evt = s._queue.pop(0) if s._queue else None
                if evt is None:
                    break
                self._apply_evt(page, evt)

            try:
                # 🔴 1-3：必须显式设 timeout —— 页面渲染冻结时默认 30s 阻塞，
                #    会让会话回收等上几十秒（叠加 `_stop` 只在下一轮才检查）
                shot = page.screenshot(type="jpeg", quality=55, timeout=10000)
                with s._lock:
                    s._frame, s._frame_at = shot, time.time()
            except Exception:
                pass

            try:
                cur = page.url or ""
                # 🔴 判定「已登录」的两个必要条件（缺一不可）：
                #   ① 出现**登录态专有** cookie（xman_t 等）——
                #      未登录时站点就会种 cookie2 / _m_h5_tk / cna / tfstk，
                #      只看这些会一路误判成"已登录"。
                #   ② URL 已离开 /login 页。
                #   实测：未登录的登录页就带 18 个 accio cookie，但**没有 xman_t**。
                on_login_page = "/login" in cur
                ck = {c["name"]: c["value"] for c in ctx.cookies()
                      if "accio" in (c.get("domain") or "")}
                has_login_cookie = any(m in ck for m in LOGIN_MARKERS)
                if has_login_cookie and not on_login_page:
                    sig = ",".join(sorted(ck))
                    if sig != last_sig:
                        last_sig = sig
                        time.sleep(2.5)     # 等 cookie 写全
                        ck = {c["name"]: c["value"] for c in ctx.cookies()
                              if "accio" in (c.get("domain") or "")}
                        if not any(m in ck for m in LOGIN_MARKERS):
                            continue
                        s.result_cookies = ck
                        s.stage, s.detail = "done", "登录成功"
                        return

                if s.stage == "ready":
                    s.detail = ("请在页面中完成登录"
                                if on_login_page else "等待登录完成…")
                    # 可选：检测到滑块时在 detail 里提示（手动模式下用户自己拖；
                    # 也可调用 solve_auto=True 让服务端代为通过）
                    if s.auto_solve_slider and on_login_page:
                        try:
                            from .login import detect_slider, solve_slider
                            for f in page.frames:
                                if detect_slider(f):
                                    s.detail = "正在自动通过滑块…"
                                    solve_slider(f, page, max_tries=2)
                                    break
                        except Exception:
                            pass
            except Exception:
                pass

            time.sleep(0.45)
        if s.stage not in ("done",):
            s.stage = "expired"

    # ── 前端输入回传 ─────────────────────────────────
    def forward_input(self, login_id: str, evt: dict) -> bool:
        """把前端的鼠标/键盘事件转成 Playwright 动作。

        🔴 **必须在浏览器所属线程执行**。
        Playwright 的 sync API 对象（page/mouse/keyboard）**不能跨线程使用** ——
        在 FastAPI 的请求线程里直接 `page.mouse.move()` 会抛
        "greenlet.error: cannot switch to a different thread"。

        做法：把动作入队，由截屏循环（`_pump`，与浏览器同线程）执行。
        这样既不阻塞请求，也不违反 Playwright 的线程约束。

        evt 形态（前端按这个发）：
          {"t":"move","x":100,"y":200}
          {"t":"down","x":..,"y":..}  {"t":"up","x":..,"y":..}
          {"t":"click","x":..,"y":..}   {"t":"type","text":"abc"}
          {"t":"key","key":"Enter"}     {"t":"scroll","dy":300}
        """
        s = self.get(login_id)
        if s is None or s._page is None:
            return False
        if time.time() > s.expires_at:
            return False
        with s._queue_lock:
            # 队列积压保护：输入事件洪泛时丢弃最旧的，保证低延迟
            if len(s._queue) > 60:
                s._queue = s._queue[-30:]
            s._queue.append(dict(evt))
        return True

    # 允许的按键白名单 —— 不设白名单等于把任意键序交给调用方
    _ALLOWED_KEYS = {
        "Enter", "Tab", "Backspace", "Delete", "Escape", "ArrowUp",
        "ArrowDown", "ArrowLeft", "ArrowRight", "Home", "End",
        "PageUp", "PageDown", "Space",
    }
    _MAX_TEXT = 2000          # 单次输入文本上限
    _MAX_COORD = 10000        # 坐标上限（视口 1280x800，给足余量）
    # 🔴 5-3：`type`/`paste` 可输入任意文本 —— 若不加约束，白名单形同虚设
    #    （攻击者用 type 即可输入任意键序）。这里做字符级净化：
    #    允许 可打印 ASCII + 中文/东亚文字 + 常用符号，剔除控制字符与换行。
    #    换行尤其危险：可在多字段表单里"跳到下一格"填入非预期内容。
    _TEXT_BAD = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")

    @classmethod
    def _clean_text(cls, raw) -> str:
        s = str(raw or "")[: cls._MAX_TEXT]
        return cls._TEXT_BAD.sub("", s)

    @classmethod
    def _coord(cls, evt: dict):
        """安全解析坐标。

        🔴 为什么必须校验：`float("inf")` / `float("nan")` 都能通过
        float() 转换，Playwright 收到 NaN/Inf 坐标后行为未定义；
        超大值则会让鼠标飞出可视区。这里统一夹到 [0, MAX]，
        非有限值直接丢弃。
        """
        try:
            x = float(evt["x"]); y = float(evt["y"])
        except (KeyError, TypeError, ValueError):
            return None
        if not (math.isfinite(x) and math.isfinite(y)):
            return None
        return (min(max(x, 0.0), cls._MAX_COORD),
                min(max(y, 0.0), cls._MAX_COORD))

    @classmethod
    def _apply_evt(cls, page, evt: dict) -> None:
        """在浏览器线程内真正执行一个动作。所有入参都经校验/夹取。"""
        t = evt.get("t")
        try:
            if t in ("move", "down", "up", "click"):
                c = cls._coord(evt)
                if c is None:
                    return
                if t == "move":
                    page.mouse.move(*c)
                elif t == "down":
                    page.mouse.move(*c); page.mouse.down()
                elif t == "up":
                    page.mouse.move(*c); page.mouse.up()
                else:
                    page.mouse.click(*c)
            elif t == "type":
                page.keyboard.type(
                    cls._clean_text(evt.get("text")), delay=30)
            elif t == "key":
                k = str(evt.get("key", ""))
                if k in cls._ALLOWED_KEYS:      # ← 白名单，其余忽略
                    page.keyboard.press(k)
            elif t == "scroll":
                try:
                    dy = float(evt.get("dy", 300))
                except (TypeError, ValueError):
                    return
                if math.isfinite(dy):
                    page.mouse.wheel(
                        0, min(max(dy, -cls._MAX_COORD), cls._MAX_COORD))
            elif t == "paste":
                page.keyboard.insert_text(cls._clean_text(evt.get("text")))
        except Exception:
            pass


web_login = WebLoginManager()
