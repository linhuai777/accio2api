"""终端 UI 小组件 —— 转圈、进度条、受控的子进程执行。

为什么需要这一层：
    `subprocess.call(["docker", "compose", "up", "-d"])` 会把镜像层下载、
    容器创建、端口绑定的**每一行原始输出**直接吐到用户控制台。对
    `docker compose up` 来说那是几十到几百行，用户真正关心的只有
    「成了没有」。控制台被刷满之后，真正的错误反而淹没在里面。

    这一层的原则：
      · 默认**吞掉**子进程输出，只显示一行会动的状态；
      · 失败时才把捕获的输出**摘要**回放（并有上限，不整屏倾倒）；
      · 需要用户决策的输出（比如拉取镜像）才显式展示。

不用第三方库（tqdm / yaspin 之类）：本项目的定位是「依赖越少越好」，
这些组件加起来不到一百行。

线程安全：转圈在后台线程跑，主线程负责结束它 —— 所有 UI 写入都过一把锁。
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from typing import Sequence

# 输出重定向到非终端时（管道 / 日志文件），动画毫无意义且会污染内容，
# 一律退回「无动画、只打印最终结果」。
_TTY = sys.stdout.isatty()


class Spinner:
    """单行转圈状态。用法：

        with Spinner("拉取镜像") as sp:
            do_work()
            sp.done("拉取完成")        # 成功
        # 或者抛异常 → 自动显示失败态

    非 TTY 环境下不画动画，只在结束时打一行 —— 保证 CI 日志可读。
    """

    FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

    def __init__(self, text: str, *, enabled: bool = True):
        self.text = text
        self._enabled = enabled and _TTY
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._t0 = time.monotonic()
        self._final: tuple[str, str] | None = None

    def __enter__(self) -> "Spinner":
        if self._enabled:
            self._thread = threading.Thread(target=self._spin, daemon=True)
            self._thread.start()
        else:
            # 非 TTY：开始时就打一行，让日志知道这步在做什么
            print(f"  {self.text} …")
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        """未显式调用 done()/fail() 就离开 with 块时兜底收尾。

        两种情况：
          · 正常退出但忘了标记 —— 补一个「完成」，别让转圈行一直挂着；
          · 抛异常 —— 标失败，且**不吞异常**（返回 False 让异常继续传播）。
        """
        if self._stop.is_set():
            return False                      # done()/fail() 已收尾
        if exc_type is not None:
            self._finish("✗", "31", f"{self.text} —— 中断")
            return False                      # 传播异常
        self.done()
        return False

    def _spin(self) -> None:
        i = 0
        while not self._stop.wait(0.08):
            frame = self.FRAMES[i % len(self.FRAMES)]
            sys.stdout.write(f"\r\033[2K  {frame} {self.text}")
            sys.stdout.flush()
            i += 1

    def _finish(self, mark: str, color_code: str, msg: str) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=0.3)
        elapsed = time.monotonic() - self._t0
        tail = f" {_dim(f'({elapsed:.1f}s)')}" if elapsed >= 0.5 else ""
        line = f"  {_color(color_code, mark)} {msg}{tail}"
        if self._enabled:
            sys.stdout.write("\r\033[2K" + line + "\n")
        else:
            print(line)
        sys.stdout.flush()

    def done(self, msg: str | None = None) -> None:
        self._finish("✓", "32", msg or self.text)

    def fail(self, msg: str | None = None) -> None:
        self._finish("✗", "31", msg or self.text)


def _color(code: str, s: str) -> str:
    return f"\033[{code}m{s}\033[0m" if _TTY else s


def _dim(s: str) -> str:
    return _color("2", s)


def run_quiet(cmd: Sequence[str], *, what: str,
              show_output_on: str = "fail",
              max_lines: int = 15,
              filter_noise: bool = True,
              cwd=None) -> tuple[bool, str]:
    """执行命令，默认只显示转圈，不刷屏。

    参数：
        what              —— 状态行文案，例如「拉取 Docker 镜像」
        show_output_on    —— "fail"（默认）只在失败时回放输出；
                             "always" 总是回放（用于需要用户看进度的场景）；
                             "never" 从不回放
        max_lines         —— 回放的行数上限（超长时头尾折叠）
        filter_noise      —— 回放前剔除「纯进度」行。`docker build` 的
                             `Building layer 3/80`、pip 的 `Downloading ...`
                             这类行对排查失败没有价值，留下只会把真正的
                             错误挤出屏幕。

    返回 (是否成功, 完整输出)。
    """
    captured: list[str] = []

    with Spinner(what) as sp:
        try:
            proc = subprocess.run(
                list(cmd), cwd=cwd,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, errors="replace",
            )
            captured = (proc.stdout or "").splitlines()
            ok = proc.returncode == 0
        except FileNotFoundError:
            sp.fail(f"{what} —— 命令不存在：{cmd[0]}")
            raise
        except KeyboardInterrupt:
            sp.fail(f"{what} —— 已中断")
            raise

        if ok:
            sp.done(what)
        else:
            sp.fail(f"{what} —— 失败")

    output = "\n".join(captured)
    should_show = (show_output_on == "always"
                   or (show_output_on == "fail" and not ok))
    if should_show and captured:
        shown = _strip_noise(captured) if filter_noise else captured
        # 过滤后若什么都不剩（全是噪声），说明没查出有用信息，
        # 回退到原始尾部，总比一片空白强。
        _print_excerpt(shown if shown else captured[-max_lines:], max_lines)
    return ok, output


# 纯进度行：对定位失败原因毫无帮助，且量极大
_NOISE_PATTERNS = (
    "[+] Building layer",           # docker build
    "Downloading ", "Downloaded ",  # pip
    "Collecting ", "Using cached ",
    "Installing collected packages",
    "Container accio2api",          # docker compose 状态漂移动画
    "\x1b[",                        # 残留 ANSI
)


def _strip_noise(lines: list[str]) -> list[str]:
    """剔除进度噪声，但**永远保留**含错误特征的行。

    宁可漏滤也不误滤 —— 一条被误删的 `ERROR:` 会让用户多排查半小时。
    """
    keep: list[str] = []
    for ln in lines:
        low = ln.lower()
        is_error = any(k in low for k in
                       ("error", "fail", "cannot", "denied", "not found",
                        "no space", "traceback", "exception", "refused",
                        "timeout", "invalid", "unable"))
        if is_error or not ln.strip():
            keep.append(ln)
            continue
        if any(ln.strip().startswith(p) or p in ln for p in _NOISE_PATTERNS):
            continue
        keep.append(ln)
    return keep


def _print_excerpt(lines: list[str], max_lines: int) -> None:
    """打印输出摘要。

    超长时**优先保留尾部** —— 命令的失败原因几乎总在最后几行
    （`ERROR: ...`、`command not found`、traceback 末尾），而头部
    通常是进度噪声（`Building layer 3/80`）。所以失败回放时头部只留
    极少（2 行，给个上下文），把预算让给尾部。
    """
    if len(lines) <= max_lines:
        for ln in lines:
            print("      " + _dim(ln))
        return
    head, tail = 2, max_lines - 3
    for ln in lines[:head]:
        print("      " + _dim(ln))
    print("      " + _dim(f"…（省略 {len(lines) - head - tail - 1} 行）"))
    for ln in lines[-(tail + 1):]:
        print("      " + _dim(ln))


class ProgressBar:
    """有确定总量的进度条。用法：

        bar = ProgressBar("下载模型", total=12)
        for item in items:
            ...
            bar.advance()

    总数为 0 或非 TTY 时退化为纯文本节点输出。
    """

    WIDTH = 28

    def __init__(self, text: str, total: int, *, enabled: bool = True):
        self.text = text
        self.total = max(total, 0)
        self.n = 0
        self._enabled = enabled and _TTY and self.total > 0
        self._t0 = time.monotonic()

    def advance(self, k: int = 1, note: str = "") -> None:
        self.n = min(self.n + k, self.total)
        if not self._enabled:
            return
        frac = self.n / self.total
        filled = int(self.WIDTH * frac)
        bar = "█" * filled + "░" * (self.WIDTH - filled)
        pct = f"{frac * 100:3.0f}%"
        tail = f"  {_dim(note)}" if note else ""
        sys.stdout.write(f"\r\033[2K  {bar} {pct}  {self.text}{tail}")
        sys.stdout.flush()

    def done(self, msg: str | None = None) -> None:
        elapsed = time.monotonic() - self._t0
        line = f"  {_color('32', '✓')} {msg or self.text} {_dim(f'({elapsed:.1f}s)')}"
        if self._enabled:
            sys.stdout.write("\r\033[2K" + line + "\n")
        else:
            print(line)
        sys.stdout.flush()
