"""Windows blur backend for the Frosted theme.

The DEFAULT is real **Acrylic** blur-behind — live frosted glass — driven by
the legacy ``SetWindowCompositionAttribute`` accent policy
(``ACCENT_ENABLE_ACRYLICBLURBEHIND``); see ``apply_acrylic``. ``JT_NO_WIN_BLUR``
opts out to the DWM **Mica** system backdrop instead (an opaque, once-sampled
wallpaper tint — NOT a live blur). Both composite BEHIND the window, visible
through transparent Qt pixels — the Windows analog of KWin's blur-behind. The
main window pairs the Acrylic path with a NON-layered window (jellytoast/app.py's
``_win_blur`` drops ``WA_TranslucentBackground``); the layered mini player /
dialogs keep it.

``DwmSetWindowAttribute`` returns an HRESULT, so the Mica fallback has real
success feedback. Build gates for that fallback (verified against
learn.microsoft.com):

  * Windows 11 22H2+ (build >= 22621): the documented
    ``DWMWA_SYSTEMBACKDROP_TYPE`` (38) = ``DWMSBT_MAINWINDOW`` (2 = Mica).
  * Windows 11 21H2 (22000..22620): the undocumented ``DWMWA_MICA_EFFECT``
    (1029) = 1.
  * Windows 10 / older (< 22000): no backdrop — UNSUPPORTED → near-opaque body.

Mica only renders when Windows' "Transparency effects" toggle is on; when it's
off we'd paint a translucent body over nothing (see-through), so ``probe()``
reads that setting from the registry and reports UNSUPPORTED when it's off —
the Windows analog of KDE's ``kwinrc blurEnabled`` demotion.

See docs/research/portable_blur.md §5. The DWM calls are exercised on Windows
only; the build/transparency gating is unit-tested cross-platform.
"""

from __future__ import annotations

import ctypes
import os
import sys

from jellytoast.platform_compat import IS_WINDOWS

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
    return IS_WINDOWS and _build() >= _MIN_BUILD_MICA


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


# ── SetWindowCompositionAttribute accent policy (undocumented user32) ──────
# The legacy accent-policy API drives the real Acrylic blur-behind (see
# apply_acrylic). Undocumented user32, so every call is best-effort.
_WCA_ACCENT_POLICY = 19


class _ACCENT_POLICY(ctypes.Structure):
    _fields_ = [
        ("AccentState", ctypes.c_uint),
        ("AccentFlags", ctypes.c_uint),
        ("GradientColor", ctypes.c_uint),
        ("AnimationId", ctypes.c_uint),
    ]


class _WINCOMPATTRDATA(ctypes.Structure):
    _fields_ = [
        ("Attribute", ctypes.c_int),
        ("Data", ctypes.c_void_p),
        ("SizeOfData", ctypes.c_size_t),
    ]


def _set_wca(hwnd: int, attribute: int, payload) -> bool:
    """SetWindowCompositionAttribute(hwnd, &WINCOMPATTRDATA) — best-effort.
    ``payload`` is any ctypes object (ACCENT_POLICY struct or a c_int).

    Returns whether the call was *issued* successfully: the function returns a
    BOOL (nonzero = accepted), so we propagate it (rather than always claiming
    success) to give the Acrylic path the same honest 'issued' signal the Mica
    branch gets from its HRESULT. The undocumented API is best-effort either
    way — the visible blur is identical whatever this returns; only apply()'s
    return becomes truthful. False on a zero return, a missing export, or any
    error (including off-Windows, where ``ctypes.windll`` is absent)."""
    try:
        data = _WINCOMPATTRDATA()
        data.Attribute = attribute
        data.Data = ctypes.cast(ctypes.byref(payload), ctypes.c_void_p)
        data.SizeOfData = ctypes.sizeof(payload)
        fn = ctypes.windll.user32.SetWindowCompositionAttribute
        fn.restype = ctypes.c_int
        fn.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        return bool(fn(ctypes.c_void_p(hwnd), ctypes.byref(data)))
    except Exception:
        return False


# ── Acrylic blur-behind (real frosted glass) ──────────────────────────────
# Unlike Mica (opaque, wallpaper-sampled-once tint), Acrylic is a live
# frosted-glass blur. The maintained qframelesswindow drives it through the
# LEGACY accent-policy API (ACCENT_ENABLE_ACRYLICBLURBEHIND), NOT the modern
# DWMWA_SYSTEMBACKDROP_TYPE — the system-backdrop Acrylic (DWMSBT_TRANSIENT)
# is for transient surfaces. The GradientColor is the tint over the blur,
# packed AABBGGRR; alpha governs how much wallpaper-blur reads through.
_ACCENT_DISABLED = 0
_ACCENT_ENABLE_ACRYLICBLURBEHIND = 4
# Border/shadow flags qframelesswindow passes (draw the 4 edges).
_ACCENT_DRAW_ALL_BORDERS = 0x20 | 0x40 | 0x80 | 0x100
# Dark carries a heavier veil than light: at the shared 0x99 the dark
# theme read "too transparent — missing the weight of a dark themed
# app" over a bright wallpaper (eyeball-calibrated to 190/0xBE on the
# Windows 11 laptop, 2026-06-10; light stays at qframelesswindow's
# default, which calibrated as already right).
_ACRYLIC_TINT_DARK = 0xBE202020  # A=0xBE (190) over (32,32,32)
_ACRYLIC_TINT_LIGHT = 0x99F2F2F2  # qframelesswindow's default light tint


def _acrylic_tint(dark: bool, elevated: bool = False) -> int:
    """Acrylic tint (AABBGGRR). JT_WIN_BLUR_ALPHA overrides just the alpha
    (0–255): lower = more blur reads through, higher = more solid tint.

    ``elevated`` (menus / dropdowns / volume popups / tooltips): these
    surfaces carry their own status-aware QSS frost fill — the same veil
    that KDE's UNTINTED KWin blur composites under on Linux. Acrylic's
    default tint stacked a second warm veil on top, so Windows popups
    read warmer + more opaque than the same popup on Linux (2026-06-10
    Windows round). Elevated surfaces therefore request a near-zero
    tint alpha — 0x01, not 0x00, because a fully transparent gradient
    disables the Acrylic material on some builds — leaving the QSS fill
    as the single tint source on every platform. JT_WIN_POPUP_BLUR_ALPHA
    tunes it live for eyeball calibration."""
    base = _ACRYLIC_TINT_DARK if dark else _ACRYLIC_TINT_LIGHT
    if elevated:
        try:
            a = int(os.environ.get("JT_WIN_POPUP_BLUR_ALPHA", "1"))
        except ValueError:
            a = 1
        return (max(1, min(255, a)) << 24) | (base & 0x00FFFFFF)
    try:
        a = int(os.environ.get("JT_WIN_BLUR_ALPHA", ""))
    except ValueError:
        return base
    return (max(0, min(255, a)) << 24) | (base & 0x00FFFFFF)


def apply_acrylic(hwnd: int, dark: bool, enabled: bool = True, elevated: bool = False) -> bool:
    """Apply (or remove) the legacy Acrylic blur-behind accent policy — the
    qframelesswindow recipe for genuine frosted glass. Best-effort; returns
    whether the accent-policy call was issued successfully (propagated from
    ``_set_wca``), mirroring the Mica branch's HRESULT check."""
    accent = _ACCENT_POLICY()
    if enabled:
        accent.AccentState = _ACCENT_ENABLE_ACRYLICBLURBEHIND
        accent.AccentFlags = _ACCENT_DRAW_ALL_BORDERS
        accent.GradientColor = _acrylic_tint(dark, elevated=elevated)
    else:
        accent.AccentState = _ACCENT_DISABLED
    return _set_wca(hwnd, _WCA_ACCENT_POLICY, accent)


def apply(
    widget,
    enabled: bool,
    corner_radius: int = 0,
    dark: bool = True,
    elevated: bool = False,
) -> bool:
    """Apply (``enabled``) or remove (``not enabled``) the Windows backdrop
    behind ``widget`` — real Acrylic blur by default, the Mica system backdrop
    when ``JT_NO_WIN_BLUR`` is set. ``corner_radius > 0`` additionally asks DWM to round
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
    if not IS_WINDOWS:
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
        # Real frosted-glass blur — the DEFAULT on Windows (JT_NO_WIN_BLUR
        # opts out to the Mica system-backdrop below). Drive the legacy
        # Acrylic accent policy instead of Mica (Mica is an opaque
        # once-sampled tint, not a live blur). The main window pairs this with
        # a NON-layered window (jellytoast/app.py `_win_blur` drops
        # WA_TranslucentBackground); the accent path also blurs the layered
        # mini player / dialogs. enabled=False (a Solid theme) removes it.
        if not os.environ.get("JT_NO_WIN_BLUR"):
            # Propagate the accent-policy result instead of an unconditional
            # True — symmetric with the Mica branch below (`_set_attr(...) == 0`)
            # so apply()'s "issued" return is honest on BOTH paths. Safe to
            # change: no caller reads this return (it's best-effort by contract,
            # see blur/__init__.py), and the visible blur is unaffected.
            return apply_acrylic(hwnd, dark, enabled, elevated=elevated)
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
    see-through). See jellytoast/blur/__init__.py."""
    from jellytoast.blur import BlurStatus

    if not IS_WINDOWS or _build() < _MIN_BUILD_MICA:
        return BlurStatus.UNSUPPORTED
    if not _transparency_enabled():
        return BlurStatus.UNSUPPORTED
    return BlurStatus.ACTIVE


def reason(status) -> str:
    """Human-readable explanation of the Windows blur status — for the boot
    log + Settings hint. Mirrors the other backends' ``reason(status)``; reads
    the build + transparency facts so the message is actionable. Never raises."""
    from jellytoast.blur import BlurStatus

    if not IS_WINDOWS:
        return "not running on Windows"
    if _build() < _MIN_BUILD_MICA:
        return "Windows 10 has no Mica backdrop — using a near-opaque body"
    if not _transparency_enabled():
        return (
            "Windows 'Transparency effects' is off (Settings → Personalization "
            "→ Colors) — using a near-opaque body"
        )
    if status == BlurStatus.ACTIVE:
        # Default is the real Acrylic accent blur; JT_NO_WIN_BLUR falls back
        # to the (flat) Mica system-backdrop.
        return (
            "Windows 11 Mica backdrop active"
            if os.environ.get("JT_NO_WIN_BLUR")
            else "Windows 11 Acrylic blur active"
        )
    return "blur unavailable — using a near-opaque body"
