"""
JellyToast theme registry.

A `Theme` is a frozen palette: every color a widget needs to style itself,
plus the RGBA tuples used by paintEvent body fills (which can't go through
QSS because Qt stylesheets don't reliably honor alpha on translucent
QFrame children — see the long note in `mini_player.py`).

Adding a new theme: append a new `Theme(...)` constant and register it in
`THEMES`. `ui_helpers.py` reads `get_active_theme()` once at import and
re-exports its colors as module-level constants for back-compat.

Live theme switching is not wired up yet — Qt stylesheets are baked at
import and at widget construction. For now, theme changes prompt the
user to restart. A future pass can add a `theme_changed` signal on the
PlayerBus and have widgets re-stylesheet on receipt.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Theme:
    name: str          # canonical key persisted to QSettings
    label: str         # human-readable name shown in the Settings dialog

    # ── QSS palette (consumed by stylesheets) ─────────────────────────
    accent: str
    accent_deep: str
    bg: str
    bg_panel: str
    bg_card: str
    text: str
    text_dim: str
    text_faint: str
    border: str
    border_accent: str

    # ── paintEvent body fills (used as `QColor(*tuple)`) ──────────────
    # Why three: the main window, the floating mini player, and the
    # settings dialog all paint their own bodies (rounded rect over a
    # translucent QWidget). The mini player runs a touch lighter than
    # the main window so the two surfaces don't read as the same depth.
    body_color: tuple[int, int, int, int]          # main window
    mini_body_color: tuple[int, int, int, int]     # floating mini player
    dialog_body_color: tuple[int, int, int, int]   # settings + cast dialogs


# Default accent: a slightly-subdued violet (#967de1). Was violet-400
# (#a78bfa) — that read as too bright on dark backgrounds where the
# accent shows up at full-bleed (Sign in button, accent icons,
# selected-row backgrounds). Each accent_color setting overrides
# this at runtime via get_active_theme().
_DEFAULT_ACCENT = "#967de1"
_DEFAULT_ACCENT_DEEP = "#7c66d0"


FROSTED_DARK = Theme(
    name="frosted_dark", label="Frosted dark",
    accent=_DEFAULT_ACCENT, accent_deep=_DEFAULT_ACCENT_DEEP,
    bg="#101010", bg_panel="#1a1a1a",
    bg_card="rgba(255,255,255,0.04)",
    text="#ffffff",
    text_dim="rgba(255,255,255,0.7)",
    text_faint="rgba(255,255,255,0.4)",
    border="rgba(255,255,255,0.08)",
    border_accent="rgba(150,125,225,0.35)",
    # Opacity ~91% body / ~97% dialog. Without KWin blur (we run native
    # Wayland by default; `org_kde_kwin_blur` has no PySide6 binding
    # yet), translucency alone reads as "wallpaper bleeds through."
    # These values still leave a hint of the desktop showing for the
    # frosted feel without the colors pushing through.
    body_color=(18, 18, 18, 232),
    mini_body_color=(22, 22, 22, 232),
    # Dialogs (settings, cast) sit on top of the main window's body —
    # text-heavy and meant to be read in isolation. Push them darker
    # and very nearly solid so the underlying chrome doesn't bleed
    # through and the boundary between dialog and host reads cleanly.
    dialog_body_color=(12, 12, 12, 252),
)

DARK = Theme(
    name="dark", label="Solid dark",
    accent=_DEFAULT_ACCENT, accent_deep=_DEFAULT_ACCENT_DEEP,
    bg="#101010", bg_panel="#181818",
    bg_card="rgba(255,255,255,0.04)",
    text="#ffffff",
    text_dim="rgba(255,255,255,0.7)",
    text_faint="rgba(255,255,255,0.4)",
    border="rgba(255,255,255,0.10)",
    border_accent="rgba(150,125,225,0.45)",
    body_color=(16, 16, 16, 255),
    mini_body_color=(20, 20, 20, 255),
    dialog_body_color=(18, 18, 18, 255),
)

TRANSPARENT = Theme(
    name="transparent", label="Transparent",
    accent=_DEFAULT_ACCENT, accent_deep=_DEFAULT_ACCENT_DEEP,
    bg="#101010", bg_panel="#202020",
    bg_card="rgba(255,255,255,0.04)",
    text="#ffffff",
    text_dim="rgba(255,255,255,0.7)",
    text_faint="rgba(255,255,255,0.4)",
    border="rgba(255,255,255,0.06)",
    border_accent="rgba(150,125,225,0.30)",
    body_color=(20, 20, 20, 110),
    mini_body_color=(24, 24, 24, 110),
    dialog_body_color=(20, 20, 20, 160),
)


THEMES: dict[str, Theme] = {
    FROSTED_DARK.name: FROSTED_DARK,
    DARK.name: DARK,
    TRANSPARENT.name: TRANSPARENT,
}

DEFAULT_THEME = FROSTED_DARK


# Curated accent presets surfaced in Settings → Display. Order matters —
# this is also the swatch row order. Each entry: (label, hex). Tied to
# the user's preferred order: purple (default), blue (Jellyfin classic),
# teal, green, pink, orange, red.
ACCENT_PRESETS = [
    # Each preset is ~10% darker than its Tailwind-/Jellyfin-default
    # baseline so it reads as a deliberate dark-mode accent instead
    # of competing with the bright text and album art for the eye.
    # Hex values computed as floor(channel * 0.9).
    ("Purple", "#967de1"),  # was #a78bfa (violet-400)
    ("Blue",   "#0093c6"),  # was #00a4dc (Jellyfin classic)
    ("Teal",   "#1eb1ab"),  # was #22c5be
    ("Green",  "#2fbe8a"),  # was #34d399
    ("Pink",   "#dc66a4"),  # was #f472b6
    ("Orange", "#e28336"),  # was #fb923c
    ("Red",    "#d73d3d"),  # was #ef4444
]


def _hex_to_rgb(hex_str: str) -> tuple[int, int, int]:
    h = hex_str.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _darken(hex_str: str, factor: float = 0.85) -> str:
    r, g, b = _hex_to_rgb(hex_str)
    return f"#{int(r*factor):02x}{int(g*factor):02x}{int(b*factor):02x}"


def _border_accent_for(hex_str: str, alpha: float) -> str:
    r, g, b = _hex_to_rgb(hex_str)
    return f"rgba({r},{g},{b},{alpha})"


# Original border_accent alpha per theme — preserve when overriding so
# the relative emphasis stays intact across accent changes.
_BORDER_ALPHAS = {
    "frosted_dark": 0.35,
    "dark":         0.45,
    "transparent":  0.30,
}


def get_active_theme() -> Theme:
    """Return the Theme matching ``settings.theme_mode``, or the default
    if the saved name is unknown. ``settings.accent_color`` overrides
    the theme's ``accent`` / ``accent_deep`` / ``border_accent`` triple
    in-place so a user can pick a non-default accent without forking a
    whole theme.

    Theme + accent are read once per ``ui_helpers`` import — live theme
    swap isn't wired yet, so changes prompt a restart in the dialog.
    """
    from dataclasses import replace as _replace
    from modules.settings import get_settings
    s = get_settings()
    base = THEMES.get(s.theme_mode, DEFAULT_THEME)
    accent = (s.accent_color or base.accent).strip()
    if not accent or accent.lower() == base.accent.lower():
        return base
    try:
        accent_deep = _darken(accent)
        alpha = _BORDER_ALPHAS.get(base.name, 0.35)
        border_accent = _border_accent_for(accent, alpha)
    except (ValueError, IndexError):
        # Bad hex — fall back to the theme's defaults.
        return base
    return _replace(
        base,
        accent=accent,
        accent_deep=accent_deep,
        border_accent=border_accent,
    )
