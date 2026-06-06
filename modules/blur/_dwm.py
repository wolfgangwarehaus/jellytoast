"""Windows blur backend — DWM Mica (Windows 11) for the Frosted theme.

Applies a Mica system backdrop behind the window via ``DwmSetWindowAttribute``
(``dwmapi.dll`` ships with Windows — no PyPI dependency). Mica composites a
blurred, wallpaper-derived tint BEHIND the window, visible only through
transparent Qt pixels — so it pairs with ``WA_TranslucentBackground`` (already
set on our windows) plus the translucent body fill, the Windows analog of
KWin's blur-behind.

Unlike KWin, ``DwmSetWindowAttribute`` returns an HRESULT, so ``apply()`` has
real success feedback. Build gates (verified against learn.microsoft.com):

  * Windows 11 22H2+ (build >= 22621): the documented
    ``DWMWA_SYSTEMBACKDROP_TYPE`` (38) = ``DWMSBT_MAINWINDOW`` (2 = Mica).
  * Windows 11 21H2 (22000..22620): the undocumented ``DWMWA_MICA_EFFECT``
    (1029) = 1.
  * Windows 10 / older (< 22000): no Mica — UNSUPPORTED → near-opaque body.
    (Acrylic via ``SetWindowCompositionAttribute`` is too laggy on drag /
    resize and drops on maximise, so we deliberately don't wire it.)

Mica only renders when Windows' "Transparency effects" toggle is on; when it's
off we'd paint a translucent body over nothing (see-through), so ``probe()``
reads that setting from the registry and reports UNSUPPORTED when it's off —
the Windows analog of KDE's ``kwinrc blurEnabled`` demotion.

See docs/research/portable_blur.md §5. The DWM calls are exercised on Windows
only; the build/transparency gating is unit-tested cross-platform.
"""

from __future__ import annotations

import ctypes
import sys

# ── DWM attribute ids + backdrop enum (learn.microsoft.com) ──────────────
_DWMWA_USE_IMMERSIVE_DARK_MODE = 20  # dark native titlebar
_DWMWA_SYSTEMBACKDROP_TYPE = 38  # documented; build >= 22621
_DWMWA_MICA_EFFECT = 1029  # legacy undocumented; build 22000..22620
_DWMWA_WINDOW_CORNER_PREFERENCE = 33  # round a frameless window; build >= 22000
_DWMWCP_ROUND = 2  # DWMWCP_ROUND — round the corners
_DWMSBT_NONE = 1  # remove the backdrop
_DWMSBT_MAINWINDOW = 2  # Mica

_MIN_BUILD_MICA = 22000  # Windows 11 21H2
_MIN_BUILD_DOCUMENTED = 22621  # Windows 11 22H2 (documented attr 38)


def _build() -> int:
    """Windows build number, or 0 where unavailable (non-Windows / error)."""
    try:
        return int(sys.getwindowsversion().build)
    except Exception:
        return 0


def _transparency_enabled() -> bool:
    """Windows "Transparency effects" toggle (Settings → Personalization →
    Colors). Mica does not render when it's off, so we'd be painting a
    translucent body over nothing — read it to demote to the near-opaque
    fallback instead. HKCU\\…\\Themes\\Personalize\\EnableTransparency;
    defaults True when unreadable (apply stays best-effort). The Windows
    analog of KDE's ``kwinrc [Plugins] blurEnabled`` check."""
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
        ) as key:
            val, _ = winreg.QueryValueEx(key, "EnableTransparency")
            return bool(val)
    except Exception:
        return True


def is_supported() -> bool:
    """True on a Windows 11 build that can show Mica. A True here doesn't
    guarantee it renders (transparency could be off) — that's probe()'s job."""
    return sys.platform == "win32" and _build() >= _MIN_BUILD_MICA


class _MARGINS(ctypes.Structure):
    _fields_ = [
        ("cxLeftWidth", ctypes.c_int),
        ("cxRightWidth", ctypes.c_int),
        ("cyTopHeight", ctypes.c_int),
        ("cyBottomHeight", ctypes.c_int),
    ]


def _set_attr(hwnd: int, attr: int, value: int) -> int:
    """DwmSetWindowAttribute(hwnd, attr, &value, 4) → HRESULT.

    ``restype`` is ``c_long`` (signed) so ``E_INVALIDARG`` (0x80070057, high
    bit set) reads back as a negative failure rather than a huge positive."""
    fn = ctypes.windll.dwmapi.DwmSetWindowAttribute
    fn.restype = ctypes.c_long
    fn.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_uint]
    val = ctypes.c_int(value)
    return fn(
        ctypes.c_void_p(hwnd),
        ctypes.c_uint(attr),
        ctypes.byref(val),
        ctypes.c_uint(ctypes.sizeof(val)),
    )


def _extend_frame(hwnd: int) -> None:
    """Extend the window frame across the whole client area (margins all -1)
    so the Mica backdrop fills it. Required on the legacy 1029 path and
    harmless-recommended on 22621+."""
    fn = ctypes.windll.dwmapi.DwmExtendFrameIntoClientArea
    fn.restype = ctypes.c_long
    fn.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    margins = _MARGINS(-1, -1, -1, -1)
    fn(ctypes.c_void_p(hwnd), ctypes.byref(margins))


def apply(widget, enabled: bool, corner_radius: int = 0, dark: bool = True) -> bool:
    """Apply (``enabled``) or remove (``not enabled``) the Mica backdrop
    behind ``widget``. ``corner_radius > 0`` additionally asks DWM to round
    the window's corners — needed for the frameless, self-painted surfaces
    (mini player + dialogs, and the main window once it goes frameless on
    Windows), because Windows does NOT clip a frameless translucent HWND to
    the painted rounded body, so without this the corners read square. The
    pixel radius itself is DWM's choice (Win11's standard ~8 px), so the value
    only acts as a "round me" flag; it's harmless on the native-framed window
    (already rounded).

    Must be called AFTER ``show()`` (``winId()`` needs a real HWND, and Qt
    6.8+ re-runs native window setup that would clobber a constructor-time
    call). Returns True if the backdrop request was accepted (HRESULT S_OK),
    False on any non-Windows / pre-22000 / not-yet-shown / error case. Never
    raises — blur is progressive enhancement."""
    if sys.platform != "win32":
        return False
    build = _build()
    if build < _MIN_BUILD_MICA:
        return False
    try:
        hwnd = int(widget.winId())
        if not hwnd:
            return False  # no native window yet
        # Frameless surfaces self-paint a rounded body but Windows leaves the
        # HWND square; ask DWM to round it (Win11 22000+, the build we already
        # gated on). Runs whether or not Mica is enabled so a Solid-theme
        # frameless dialog still gets rounded corners.
        if corner_radius > 0:
            _set_attr(hwnd, _DWMWA_WINDOW_CORNER_PREFERENCE, _DWMWCP_ROUND)
        # Match the titlebar AND the Mica backdrop variant to the theme:
        # immersive-dark on for dark themes (dark Mica), off for light
        # themes (light, wallpaper-tinted Mica). Follows the OS live when
        # the theme_mode is "auto".
        _set_attr(hwnd, _DWMWA_USE_IMMERSIVE_DARK_MODE, 1 if dark else 0)
        _extend_frame(hwnd)
        if build >= _MIN_BUILD_DOCUMENTED:
            attr = _DWMWA_SYSTEMBACKDROP_TYPE
            value = _DWMSBT_MAINWINDOW if enabled else _DWMSBT_NONE
        else:
            attr = _DWMWA_MICA_EFFECT  # legacy: 1 = Mica, 0 = off
            value = 1 if enabled else 0
        return _set_attr(hwnd, attr, value) == 0
    except Exception:
        return False


def probe():
    """Verified BlurStatus for Windows. Mica availability is a build-version
    fact (no window needed): Windows 11 22000+ with Transparency effects on
    gets a real backdrop → ACTIVE (translucent body rides Mica). Pre-22000,
    or transparency disabled, → UNSUPPORTED (near-opaque body, never
    see-through). See modules/blur/__init__.py."""
    from modules.blur import BlurStatus

    if sys.platform != "win32" or _build() < _MIN_BUILD_MICA:
        return BlurStatus.UNSUPPORTED
    if not _transparency_enabled():
        return BlurStatus.UNSUPPORTED
    return BlurStatus.ACTIVE


def reason(status) -> str:
    """Human-readable explanation of the Windows blur status — for the boot
    log + Settings hint. Mirrors the other backends' ``reason(status)``; reads
    the build + transparency facts so the message is actionable. Never raises."""
    from modules.blur import BlurStatus

    if sys.platform != "win32":
        return "not running on Windows"
    if _build() < _MIN_BUILD_MICA:
        return "Windows 10 has no Mica backdrop — using a near-opaque body"
    if not _transparency_enabled():
        return (
            "Windows 'Transparency effects' is off (Settings → Personalization "
            "→ Colors) — using a near-opaque body"
        )
    if status == BlurStatus.ACTIVE:
        return "Windows 11 Mica backdrop active"
    return "Mica unavailable — using a near-opaque body"
