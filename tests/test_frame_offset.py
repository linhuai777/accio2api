"""frame_offset 三层嵌套修复回归 —— 2026-09-24。

Playwright bounding_box() 本身返回主视口绝对坐标；旧实现沿 parent_frame
链逐层累加，三层场景（入口页 → 桥内登录页 iframe → punish iframe）中间层
被加两次，拖动坐标系统性偏移，滑块从未被真正抓住。本测试锁定单次取值语义。
"""
import os
import sys
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from app.auth.login import frame_offset


class _FakeEl:
    def __init__(self, box):
        self._box = box

    def bounding_box(self):
        return self._box


class _FakeFrame:
    """模拟 frame_element().bounding_box() 已含全部祖先偏移的语义。"""

    def __init__(self, box):
        self._box = box

    @property
    def frame_element(self):
        return lambda: _FakeEl(self._box)


class _FakePage:
    def __init__(self, frame):
        self.main_frame = object()   # 哨兵：任何 _FakeFrame 都不是主帧
        self._frame = frame


def test_frame_offset_takes_box_once(monkeypatch):
    """box 已是视口绝对坐标 → 偏移必须等于 box 本身，不得再叠加。"""
    f = _FakeFrame({"x": 120.0, "y": 240.0, "width": 300, "height": 40})
    off = frame_offset(_FakePage(f), f)
    assert off == (120.0, 240.0), f"期望单次取值 (120,240)，实际 {off}"


def test_frame_offset_main_frame_zero():
    page = type("P", (), {"main_frame": None})()
    f = type("F", (), {})()
    page.main_frame = f
    assert frame_offset(page, f) == (0.0, 0.0)


def test_frame_offset_no_element_falls_back_zero():
    class _NoEl:
        def frame_element(self):
            return None
    page = _FakePage(_NoEl())
    assert frame_offset(page, _NoEl()) == (0.0, 0.0)
