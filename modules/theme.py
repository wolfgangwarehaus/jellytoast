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


FROSTED_DARK = Theme(
    name="frosted_dark", label="Frosted dark",
    accent="#00a4dc", accent_deep="#0085bd",
    bg="#101010", bg_panel="#202020",
    bg_card="rgba(255,255,255,0.04)",
    text="#ffffff",
    text_dim="rgba(255,255,255,0.7)",
    text_faint="rgba(255,255,255,0.4)",
    border="rgba(255,255,255,0.08)",
    border_accent="rgba(0,164,220,0.35)",
    body_color=(24, 24, 24, 184),
    mini_body_color=(28, 28, 28, 184),
    dialog_body_color=(22, 22, 22, 230),
)

DARK = Theme(
    name="dark", label="Solid dark",
    accent="#00a4dc", accent_deep="#0085bd",
    bg="#101010", bg_panel="#181818",
    bg_card="rgba(255,255,255,0.04)",
    text="#ffffff",
    text_dim="rgba(255,255,255,0.7)",
    text_faint="rgba(255,255,255,0.4)",
    border="rgba(255,255,255,0.10)",
    border_accent="rgba(0,164,220,0.45)",
    body_color=(16, 16, 16, 255),
    mini_body_color=(20, 20, 20, 255),
    dialog_body_color=(18, 18, 18, 255),
)

TRANSPARENT = Theme(
    name="transparent", label="Transparent",
    accent="#00a4dc", accent_deep="#0085bd",
    bg="#101010", bg_panel="#202020",
    bg_card="rgba(255,255,255,0.04)",
    text="#ffffff",
    text_dim="rgba(255,255,255,0.7)",
    text_faint="rgba(255,255,255,0.4)",
    border="rgba(255,255,255,0.06)",
    border_accent="rgba(0,164,220,0.30)",
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


def get_active_theme() -> Theme:
    """Return the Theme matching `settings.theme_mode`, or the default
    if the saved name is unknown (e.g. user picked Light before we ship
    a light palette)."""
    # Lazy import — settings.py imports QSettings which needs a
    # QApplication-friendly state to fully resolve. Theme is read at
    # import time of ui_helpers, so deferring keeps the cycle clean.
    from modules.settings import get_settings
    name = get_settings().theme_mode
    return THEMES.get(name, DEFAULT_THEME)
