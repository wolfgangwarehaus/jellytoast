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
import os
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


# ── Host-backdrop accent policy (undocumented user32) ─────────────────────
# DWMWA_SYSTEMBACKDROP_TYPE alone draws Mica behind the frame; to make it
# composite behind the whole CLIENT area of a frameless window, the
# host-backdrop accent policy is also flipped on — the recipe the
# battle-tested PyQt-Frameless-Window uses. Undocumented user32 API, so every
# call is best-effort (never raises). Gated by JT_WIN_MICA in apply().
_WCA_ACCENT_POLICY = 19
_WCA_USEDARKMODECOLORS = 26
_ACCENT_ENABLE_HOSTBACKDROP = 5


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


def _set_wca(hwnd: int, attribute: int, payload) -> None:
    """SetWindowCompositionAttribute(hwnd, &WINCOMPATTRDATA) — best-effort.
    ``payload`` is any ctypes object (ACCENT_POLICY struct or a c_int)."""
    try:
        data = _WINCOMPATTRDATA()
        data.Attribute = attribute
        data.Data = ctypes.cast(ctypes.byref(payload), ctypes.c_void_p)
        data.SizeOfData = ctypes.sizeof(payload)
        fn = ctypes.windll.user32.SetWindowCompositionAttribute
        fn(ctypes.c_void_p(hwnd), ctypes.byref(data))
    except Exception:
        pass


def _enable_host_backdrop(hwnd: int, dark: bool) -> None:
    """Flip on the host-backdrop accent policy (+ dark-mode colors) so Mica
    composites behind the frameless client area. Best-effort."""
    accent = _ACCENT_POLICY()
    accent.AccentState = _ACCENT_ENABLE_HOSTBACKDROP
    _set_wca(hwnd, _WCA_ACCENT_POLICY, accent)
    _set_wca(hwnd, _WCA_USEDARKMODECOLORS, ctypes.c_int(1 if dark else 0))


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
_ACRYLIC_TINT_DARK = 0x99202020  # A=0x99 over (32,32,32)
_ACRYLIC_TINT_LIGHT = 0x99F2F2F2  # qframelesswindow's default light tint


def _acrylic_tint(dark: bool) -> int:
    """Acrylic tint (AABBGGRR). JT_WIN_BLUR_ALPHA overrides just the alpha
    (0–255): lower = more blur reads through, higher = more solid tint."""
    base = _ACRYLIC_TINT_DARK if dark else _ACRYLIC_TINT_LIGHT
    try:
        a = int(os.environ.get("JT_WIN_BLUR_ALPHA", ""))
    except ValueError:
        return base
    return (max(0, min(255, a)) << 24) | (base & 0x00FFFFFF)


def apply_acrylic(hwnd: int, dark: bool, enabled: bool = True) -> None:
    """Apply (or remove) the legacy Acrylic blur-behind accent policy — the
    qframelesswindow recipe for genuine frosted glass. Best-effort."""
    accent = _ACCENT_POLICY()
    if enabled:
        accent.AccentState = _ACCENT_ENABLE_ACRYLICBLURBEHIND
        accent.AccentFlags = _ACCENT_DRAW_ALL_BORDERS
        accent.GradientColor = _acrylic_tint(dark)
    else:
        accent.AccentState = _ACCENT_DISABLED
    _set_wca(hwnd, _WCA_ACCENT_POLICY, accent)


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
        # Real frosted-glass blur (JT_WIN_BLUR): drive the legacy Acrylic
        # accent policy instead of the Mica system-backdrop (Mica is an opaque
        # once-sampled tint, not a live blur). Requires a NON-layered window —
        # jellytoast.py `_win_blur` drops WA_TranslucentBackground, since
        # DWM/accent backdrops never composite behind a layered window.
        if os.environ.get("JT_WIN_BLUR"):
            apply_acrylic(hwnd, dark, enabled)
            return True
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
