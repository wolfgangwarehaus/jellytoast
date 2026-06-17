"""Native Windows sizing frame for the frameless main window.

Background — the left/top-edge resize jitter
---------------------------------------------
The frameless main window (``FramelessWindowHint``; see jellytoast/app.py) used
to drive edge/corner resize through Qt's ``startSystemResize`` (via
``_ResizeEdgeFilter``). On Windows that runs Qt's *software* resize loop, which
on the TOP and LEFT edges moves the window origin a frame before the content
repaint catches up — the classic "content trails the origin" jitter
(QTBUG-40578; framelesshelper #29; Qt Forum "Window resizing on top & left
glitching out"). RIGHT/BOTTOM edges don't move the origin, so they already look
smooth (and the branch's resizeEvent fix keeps them that way by skipping the
per-tick DWM blur reshape).

The fix — port the qwindowkit / framelesshelper Windows technique
----------------------------------------------------------------
Give the HWND a *real* native sizing frame (``WS_THICKFRAME | WS_CAPTION``) so
Windows' own resize loop moves origin+size atomically, DWM-composited, with no
trailing. Then make that frame invisible by collapsing the non-client area in
``WM_NCCALCSIZE`` (client area == whole window). ``WM_NCHITTEST`` re-supplies the
resize-border hit zones (and Windows sets the resize cursors for free).

Maximize: a maximized frameless window would otherwise spill its (now
frame-collapsed) client over the taskbar and off-screen, because the different
maximize paths overflow by different amounts — Qt's ``showMaximized`` sizes the
*window* to the work area exactly, while a native maximize / Aero-snap sizes it
to work area + a frame-thickness overflow. So when maximized ``WM_NCCALCSIZE``
sets the client rect *directly to the monitor's work area* (``rcWork`` from
``MonitorFromWindow(NEAREST)``) — path-independent, fills correctly, never
covers the taskbar, multi-monitor aware — with a 1 px sliver carve-out on an
auto-hide taskbar edge so its pop-out still triggers. (A fixed frame-thickness
inset would only correct one of the two maximize paths; clamping to the work
area corrects both.)

Title-bar drag stays Qt-driven: the body hit-tests as ``HTCLIENT`` and
``JtTopBar`` calls ``startSystemMove`` on press, so the window controls keep
working and we never report ``HTCAPTION``.

All ctypes, Windows-only. Every entry point degrades to a no-op / "not handled"
off Windows or on any error — native resize is progressive enhancement, never a
hard dependency. Sources: qwindowkit ``win32windowcontext.cpp`` and the QA brief.
"""

from __future__ import annotations

import ctypes
import logging
from ctypes import wintypes

from jellytoast.platform_compat import IS_WINDOWS

logger = logging.getLogger(__name__)

# ── Win32 messages / styles / metrics / hit codes / flags ────────────────────
_WM_NCCALCSIZE = 0x0083
_WM_NCHITTEST = 0x0084

_GWL_STYLE = -16
_WS_THICKFRAME = 0x00040000  # the native sizing border (smooth resize loop)
_WS_CAPTION = 0x00C00000  # standard frame: drop shadow + native maximize anim

_SM_CXSIZEFRAME = 32
_SM_CXPADDEDBORDER = 92

# Non-client hit-test results (winuser.h).
_HTCLIENT = 1
_HTLEFT = 10
_HTRIGHT = 11
_HTTOP = 12
_HTTOPLEFT = 13
_HTTOPRIGHT = 14
_HTBOTTOM = 15
_HTBOTTOMLEFT = 16
_HTBOTTOMRIGHT = 17

_SWP_NOSIZE = 0x0001
_SWP_NOMOVE = 0x0002
_SWP_NOZORDER = 0x0004
_SWP_NOACTIVATE = 0x0010
_SWP_FRAMECHANGED = 0x0020

_MONITOR_DEFAULTTONEAREST = 0x00000002

# Shell appbar (auto-hide taskbar detection).
_ABM_GETSTATE = 0x00000004
_ABM_GETTASKBARPOS = 0x00000005
_ABS_AUTOHIDE = 0x0000001
_ABE_LEFT = 0
_ABE_TOP = 1
_ABE_RIGHT = 2
_ABE_BOTTOM = 3

# msg.lParam is a signed pointer-width int in ctypes.wintypes; a high
# user-space address reads back negative. Mask to unsigned pointer width
# before from_address().
_PTR_MASK = (1 << (8 * ctypes.sizeof(ctypes.c_void_p))) - 1


class _NCCALCSIZE_PARAMS(ctypes.Structure):
    # rgrc[0] is the proposed new window rect — modifying it in place sets the
    # client rect. (rgrc[1]/[2] + lppos are for child-rect preservation, unused
    # here.)
    _fields_ = [
        ("rgrc", wintypes.RECT * 3),
        ("lppos", ctypes.c_void_p),
    ]


class _MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
    ]


class _APPBARDATA(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uCallbackMessage", wintypes.UINT),
        ("uEdge", wintypes.UINT),
        ("rc", wintypes.RECT),
        ("lParam", wintypes.LPARAM),
    ]


def _user32():
    """user32 with the argtypes/restypes we depend on pinned, so 64-bit
    LONG_PTR returns (window styles) don't get truncated to 32 bits — the
    classic ctypes-on-Windows footgun."""
    u = ctypes.windll.user32
    u.GetWindowLongPtrW.restype = ctypes.c_ssize_t
    u.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
    u.SetWindowLongPtrW.restype = ctypes.c_ssize_t
    u.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
    u.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    u.GetWindowRect.restype = wintypes.BOOL
    u.MonitorFromWindow.restype = wintypes.HMONITOR
    u.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
    u.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.POINTER(_MONITORINFO)]
    u.GetMonitorInfoW.restype = wintypes.BOOL
    return u


def _window_rect(hwnd: int):
    """The window's screen rect as (left, top, right, bottom) in physical px,
    or None. Split out so the hit-test geometry is unit-testable without the
    Win32 call."""
    u = _user32()
    r = wintypes.RECT()
    if not u.GetWindowRect(hwnd, ctypes.byref(r)):
        return None
    return r.left, r.top, r.right, r.bottom


def _work_area(hwnd: int):
    """Physical-pixel work area (rcWork) of the window's nearest monitor, or
    None. Multi-monitor aware via MonitorFromWindow(NEAREST)."""
    u = _user32()
    monitor = u.MonitorFromWindow(hwnd, _MONITOR_DEFAULTTONEAREST)
    info = _MONITORINFO()
    info.cbSize = ctypes.sizeof(_MONITORINFO)
    if not u.GetMonitorInfoW(monitor, ctypes.byref(info)):
        return None
    return info.rcWork


def _border_thickness(hwnd: int) -> int:
    """Resize-border thickness in PHYSICAL px = SM_CXSIZEFRAME + SM_CXPADDEDBORDER
    (≈8 px at 100% DPI). Uses the per-DPI metrics (Win10 1607+) so the grab
    band scales with the window's monitor; falls back to the global metrics."""
    u = ctypes.windll.user32
    try:
        dpi = u.GetDpiForWindow(hwnd) or 96
        cx = u.GetSystemMetricsForDpi(_SM_CXSIZEFRAME, dpi)
        pad = u.GetSystemMetricsForDpi(_SM_CXPADDEDBORDER, dpi)
        if cx + pad > 0:
            return cx + pad
    except Exception:
        pass
    return u.GetSystemMetrics(_SM_CXSIZEFRAME) + u.GetSystemMetrics(_SM_CXPADDEDBORDER)


def _autohide_edge() -> int | None:
    """If the primary taskbar is in auto-hide mode, return the screen edge it
    lives on (ABE_*), else None. A maximized window whose client fills the whole
    monitor edge-to-edge suppresses the auto-hide pop-out, so the maximize inset
    leaves a 1 px sliver on this edge. Best-effort (untested without an
    auto-hide taskbar); any failure → None → no sliver."""
    try:
        shell32 = ctypes.windll.shell32
        abd = _APPBARDATA()
        abd.cbSize = ctypes.sizeof(_APPBARDATA)
        state = shell32.SHAppBarMessage(_ABM_GETSTATE, ctypes.byref(abd))
        if not (int(state) & _ABS_AUTOHIDE):
            return None
        pos = _APPBARDATA()
        pos.cbSize = ctypes.sizeof(_APPBARDATA)
        shell32.SHAppBarMessage(_ABM_GETTASKBARPOS, ctypes.byref(pos))
        return int(pos.uEdge)
    except Exception:  # pragma: no cover — Windows-only
        return None


def enable(hwnd: int) -> None:
    """Add the native sizing frame (``WS_THICKFRAME | WS_CAPTION``) to the
    frameless HWND and force a frame recompute so ``WM_NCCALCSIZE`` collapses it
    immediately (no titlebar flash).

    Must be called AFTER ``show()`` — the HWND must exist, and Qt 6.8+ re-runs
    native window setup that would clobber a constructor-time call (same caveat
    as ``blur.apply``). Best-effort; never raises."""
    if not IS_WINDOWS or not hwnd:
        return
    try:
        u = _user32()
        style = u.GetWindowLongPtrW(hwnd, _GWL_STYLE)
        u.SetWindowLongPtrW(hwnd, _GWL_STYLE, style | _WS_THICKFRAME | _WS_CAPTION)
        # SWP_FRAMECHANGED re-sends WM_NCCALCSIZE so the just-added frame is
        # collapsed to the client area on this turn rather than the next paint.
        u.SetWindowPos(
            hwnd,
            0,
            0,
            0,
            0,
            0,
            _SWP_NOMOVE | _SWP_NOSIZE | _SWP_NOZORDER | _SWP_NOACTIVATE | _SWP_FRAMECHANGED,
        )
    except Exception as exc:  # pragma: no cover — Windows-only
        logger.debug("win_frameless.enable failed: %s", exc)


def is_enabled(hwnd: int) -> bool:
    """True if the native sizing frame is fully on the HWND. Used to re-assert
    ``enable()`` if Qt strips the style on a later native-setup pass. Checks
    BOTH bits ``enable()`` sets: WS_THICKFRAME (resize) and WS_CAPTION (drop
    shadow + native maximize animation) — so a pass that strips only WS_CAPTION
    still triggers a re-assert rather than silently losing the shadow."""
    if not IS_WINDOWS or not hwnd:
        return False
    try:
        u = _user32()
        want = _WS_THICKFRAME | _WS_CAPTION
        return (u.GetWindowLongPtrW(hwnd, _GWL_STYLE) & want) == want
    except Exception:  # pragma: no cover — Windows-only
        return False


def handle(window, msg_addr: int) -> tuple[bool, int]:
    """Dispatch a ``windows_generic_MSG`` for the frameless main window.

    Returns ``(handled, lresult)``. ``handled=False`` lets Qt's default
    ``nativeEvent`` run. ``window`` is the ``QMainWindow`` (used for
    maximized/fullscreen state)."""
    try:
        msg = wintypes.MSG.from_address(msg_addr)
    except Exception:  # pragma: no cover — Windows-only
        return False, 0
    m = msg.message
    if m == _WM_NCCALCSIZE:
        return _nccalcsize(window, msg.hWnd, msg.wParam, int(msg.lParam))
    if m == _WM_NCHITTEST:
        return _hittest(window, msg.hWnd, int(msg.lParam))
    return False, 0


def _nccalcsize(window, hwnd: int, wparam, lparam: int) -> tuple[bool, int]:
    # wParam FALSE: single-RECT form, return value ignored — let Qt handle.
    if not wparam:
        return False, 0
    # wParam TRUE: rgrc[0] is the proposed window rect; making it the client
    # rect unmodified collapses the frame (client == whole window).
    if window.isMaximized() and not window.isFullScreen():
        # Clamp the client to the monitor's work area. This is path-independent:
        # Qt's showMaximized sizes the window to the work area exactly (no
        # overflow), while a NATIVE maximize (Aero Snap, Win+Up, taskbar menu)
        # sizes it to work area + a frame-thickness overflow. A fixed inset
        # would only fix one case (and leave a gap in the other); setting the
        # client directly to the work area lands correctly for both — fills the
        # screen, never covers the taskbar, nothing clipped off-screen.
        try:
            work = _work_area(hwnd)
            if work is not None:
                params = _NCCALCSIZE_PARAMS.from_address(lparam & _PTR_MASK)
                params.rgrc[0].left = work.left
                params.rgrc[0].top = work.top
                params.rgrc[0].right = work.right
                params.rgrc[0].bottom = work.bottom
                # Auto-hide taskbar: rcWork spans the whole monitor, so the
                # client would fill it edge-to-edge and suppress the pop-out.
                # Leave a 1 px sliver on the taskbar's edge.
                edge = _autohide_edge()
                if edge == _ABE_BOTTOM:
                    params.rgrc[0].bottom -= 1
                elif edge == _ABE_TOP:
                    params.rgrc[0].top += 1
                elif edge == _ABE_LEFT:
                    params.rgrc[0].left += 1
                elif edge == _ABE_RIGHT:
                    params.rgrc[0].right -= 1
        except Exception as exc:  # pragma: no cover — Windows-only
            logger.debug("nccalcsize maximize clamp failed: %s", exc)
    return True, 0


def _hittest(window, hwnd: int, lparam: int) -> tuple[bool, int]:
    # No resize borders on a maximized / fullscreen window — the whole surface
    # is client (matches _ResizeEdgeFilter's bail-out on Linux).
    if window.isMaximized() or window.isFullScreen():
        return True, _HTCLIENT
    # lParam packs the cursor's screen position as two signed 16-bit words
    # (signed so off-primary / negative-coord monitors work).
    x = ctypes.c_short(lparam & 0xFFFF).value
    y = ctypes.c_short((lparam >> 16) & 0xFFFF).value
    wr = _window_rect(hwnd)
    if wr is None:
        return False, 0
    left, top, right, bottom = wr
    bt = _border_thickness(hwnd)
    corner = bt * 2  # fatter diagonal grab, mirroring _ResizeEdgeFilter.CORNER
    near_l = x < left + bt
    near_r = x >= right - bt
    near_t = y < top + bt
    near_b = y >= bottom - bt
    corner_l = x < left + corner
    corner_r = x >= right - corner
    corner_t = y < top + corner
    corner_b = y >= bottom - corner
    # Corners first (generous), then single edges (tight) — same ordering as
    # the Linux edge filter so the feel matches across platforms.
    if corner_t and corner_l:
        return True, _HTTOPLEFT
    if corner_t and corner_r:
        return True, _HTTOPRIGHT
    if corner_b and corner_l:
        return True, _HTBOTTOMLEFT
    if corner_b and corner_r:
        return True, _HTBOTTOMRIGHT
    if near_l:
        return True, _HTLEFT
    if near_r:
        return True, _HTRIGHT
    if near_t:
        return True, _HTTOP
    if near_b:
        return True, _HTBOTTOM
    # Interior: report client so Qt owns the click — titlebar drag
    # (startSystemMove), buttons, content. Never HTCAPTION.
    return True, _HTCLIENT
