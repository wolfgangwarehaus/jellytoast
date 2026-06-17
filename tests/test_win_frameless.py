"""Pure-logic tests for the Windows native sizing frame (jellytoast.win_frameless).

The Win32 calls (window rect, border metrics, monitor work area, auto-hide
taskbar) are split behind helper functions, so the hit-test geometry and the
maximize work-area clamp are testable cross-platform by monkeypatching those
helpers — the same approach the blur-gating tests use. The live resize
smoothness itself can only be verified on Windows by eye (see the QA brief).
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes

from jellytoast import win_frameless as wf

WIN = (100, 100, 900, 700)  # window screen rect: left, top, right, bottom


class _StubWin:
    def __init__(self, maximized=False, fullscreen=False):
        self._max = maximized
        self._fs = fullscreen

    def isMaximized(self):
        return self._max

    def isFullScreen(self):
        return self._fs


def _lp(x, y):
    """Pack a screen point the way WM_NCHITTEST does (two signed 16-bit words)."""
    return ((y & 0xFFFF) << 16) | (x & 0xFFFF)


def _make_msg(message, wparam, lparam):
    msg = wintypes.MSG()
    msg.hWnd = 0
    msg.message = message
    msg.wParam = wparam
    msg.lParam = lparam
    return msg


def _geom(monkeypatch, bt=8):
    monkeypatch.setattr(wf, "_window_rect", lambda hwnd: WIN)
    monkeypatch.setattr(wf, "_border_thickness", lambda hwnd: bt)


# ── WM_NCHITTEST geometry ────────────────────────────────────────────────────
def test_hittest_single_edges(monkeypatch):
    _geom(monkeypatch)
    w = _StubWin()
    assert wf._hittest(w, 0, _lp(101, 400))[1] == wf._HTLEFT
    assert wf._hittest(w, 0, _lp(898, 400))[1] == wf._HTRIGHT
    assert wf._hittest(w, 0, _lp(500, 101))[1] == wf._HTTOP
    assert wf._hittest(w, 0, _lp(500, 698))[1] == wf._HTBOTTOM


def test_hittest_corners_are_fat(monkeypatch):
    _geom(monkeypatch)  # corner zone = 2*bt = 16 px
    w = _StubWin()
    assert wf._hittest(w, 0, _lp(112, 112))[1] == wf._HTTOPLEFT
    assert wf._hittest(w, 0, _lp(888, 112))[1] == wf._HTTOPRIGHT
    assert wf._hittest(w, 0, _lp(112, 688))[1] == wf._HTBOTTOMLEFT
    assert wf._hittest(w, 0, _lp(888, 688))[1] == wf._HTBOTTOMRIGHT


def test_hittest_interior_is_client(monkeypatch):
    _geom(monkeypatch)
    assert wf._hittest(_StubWin(), 0, _lp(500, 400))[1] == wf._HTCLIENT


def test_hittest_no_borders_when_maximized_or_fullscreen(monkeypatch):
    _geom(monkeypatch)
    # An edge point that would be HTLEFT on a normal window is HTCLIENT here.
    assert wf._hittest(_StubWin(maximized=True), 0, _lp(101, 400))[1] == wf._HTCLIENT
    assert wf._hittest(_StubWin(fullscreen=True), 0, _lp(101, 400))[1] == wf._HTCLIENT


def test_hittest_falls_through_without_window_rect(monkeypatch):
    monkeypatch.setattr(wf, "_window_rect", lambda hwnd: None)
    monkeypatch.setattr(wf, "_border_thickness", lambda hwnd: 8)
    assert wf._hittest(_StubWin(), 0, _lp(101, 400)) == (False, 0)


# ── handle() dispatch ────────────────────────────────────────────────────────
def test_handle_dispatches_hittest(monkeypatch):
    _geom(monkeypatch)
    msg = _make_msg(wf._WM_NCHITTEST, 0, _lp(101, 400))
    assert wf.handle(_StubWin(), ctypes.addressof(msg)) == (True, wf._HTLEFT)


def test_handle_unknown_message_passes_through():
    msg = _make_msg(0x0000, 0, 0)
    assert wf.handle(_StubWin(), ctypes.addressof(msg)) == (False, 0)


def test_handle_nccalcsize_false_wparam_passes_through():
    msg = _make_msg(wf._WM_NCCALCSIZE, 0, 0)
    assert wf.handle(_StubWin(), ctypes.addressof(msg)) == (False, 0)


def test_handle_nccalcsize_collapses_frame_when_normal():
    # wParam TRUE + not maximized: frame collapses (client == window), lParam
    # untouched, returns 0.
    msg = _make_msg(wf._WM_NCCALCSIZE, 1, 0)
    assert wf.handle(_StubWin(maximized=False), ctypes.addressof(msg)) == (True, 0)


# ── WM_NCCALCSIZE maximize clamp ─────────────────────────────────────────────
def _rect(left, t, r, b):
    rc = wintypes.RECT()
    rc.left, rc.top, rc.right, rc.bottom = left, t, r, b
    return rc


def test_nccalcsize_clamps_client_to_workarea_when_maximized(monkeypatch):
    monkeypatch.setattr(wf, "_work_area", lambda hwnd: _rect(0, 0, 1920, 1020))
    monkeypatch.setattr(wf, "_autohide_edge", lambda: None)
    params = wf._NCCALCSIZE_PARAMS()
    # An overflowed maximized window rect (native maximize path).
    params.rgrc[0].left, params.rgrc[0].top = -7, -7
    params.rgrc[0].right, params.rgrc[0].bottom = 1927, 1027
    msg = _make_msg(wf._WM_NCCALCSIZE, 1, ctypes.addressof(params))
    assert wf.handle(_StubWin(maximized=True), ctypes.addressof(msg)) == (True, 0)
    rc = params.rgrc[0]
    # Clamped exactly to the work area — taskbar uncovered, no off-screen spill.
    assert (rc.left, rc.top, rc.right, rc.bottom) == (0, 0, 1920, 1020)


def test_nccalcsize_autohide_taskbar_leaves_sliver(monkeypatch):
    # Auto-hide taskbar: work area spans the whole monitor; the client must stop
    # 1 px short of the taskbar edge so the pop-out still triggers.
    monkeypatch.setattr(wf, "_work_area", lambda hwnd: _rect(0, 0, 1920, 1080))
    monkeypatch.setattr(wf, "_autohide_edge", lambda: wf._ABE_BOTTOM)
    params = wf._NCCALCSIZE_PARAMS()
    params.rgrc[0].left, params.rgrc[0].top = 0, 0
    params.rgrc[0].right, params.rgrc[0].bottom = 1920, 1080
    msg = _make_msg(wf._WM_NCCALCSIZE, 1, ctypes.addressof(params))
    wf.handle(_StubWin(maximized=True), ctypes.addressof(msg))
    assert params.rgrc[0].bottom == 1079
