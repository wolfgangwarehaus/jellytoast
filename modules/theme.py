"""
jellytoast theme registry.

A `Theme` is a frozen palette: the full set of semantic color tokens a
widget needs to style itself, plus the RGBA tuples used by paintEvent
body fills (which can't go through QSS because Qt stylesheets don't
reliably honor alpha on translucent QFrame children — see the long
note in `mini_player.py`).

The token set is named by *intent* (`wash_hover`, `surface_input`,
`idle_text`, …), not by the value it happens to hold. This is the
layer that swaps wholesale between a dark and a light theme — see
`docs/research/theming.md`. Every painted surface references these
tokens; the dark family shares one set of token values
(`_DARK_TOKENS`) and the light family another (`_LIGHT_TOKENS`); the
three themes in each family differ only in surface/border depth and
body opacity.

Adding a new theme: append a new `Theme(...)` constant and register it
in `THEMES`. `ui_helpers.py` reads `get_active_theme()` once at import
and re-exports its colors as module-level constants for back-compat.

Live theme switching is not yet wired up for the full token set — only
the accent re-stamps live today. Phase 3 of the theming rework broadens
that to every token; until then a theme-mode change prompts a restart.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Theme:
    name: str  # canonical key persisted to QSettings
    label: str  # human-readable name shown in the Settings dialog

    # ── Accent ────────────────────────────────────────────────────────
    accent: str
    accent_deep: str
    border_accent: str

    # ── Surfaces ──────────────────────────────────────────────────────
    bg: str
    bg_panel: str
    bg_card: str

    # ── Text ──────────────────────────────────────────────────────────
    text: str
    text_dim: str
    text_faint: str
    idle_text: str  # "Nothing playing" / empty-state labels
    error_fg: str  # inline error text (login failure, etc.)
    warn_fg: str  # warning marker (offline indicator)

    # ── Borders ───────────────────────────────────────────────────────
    border: str

    # ── Interactive washes ────────────────────────────────────────────
    # Hover / pressed fills for buttons, list rows, tiles.
    wash_hover: str  # icon-button hover, volume popup body
    wash_pressed: str  # icon-button pressed state
    hover_subtle: str  # ghost-button + library-tile hover
    hover_list_row: str  # list-row hover (cast dialog, settings sidebar)
    selected_row: str  # selected list row (non-accent variant)
    pressed_white: str  # white-press button state

    # ── Inputs ────────────────────────────────────────────────────────
    surface_input: str  # QLineEdit / QComboBox / QSpinBox fill
    surface_input_focus: str  # input :focus background tint
    disabled_fg: str  # disabled foreground (icon-button, placeholders)

    # ── Sliders ───────────────────────────────────────────────────────
    slider_groove: str  # slider track fill (volume / seek / EQ)

    # ── Overlays / popups ─────────────────────────────────────────────
    overlay_dark: str  # translucent overlay (cover-art heart bg)
    overlay_dark_hover: str  # translucent overlay on hover
    popup_opaque_fill: str  # opaque popup body (cast/sort menus, combos)

    # ── paintEvent body fills (used as `QColor(*tuple)`) ──────────────
    # Why three: the main window, the floating mini player, and the
    # settings dialog all paint their own bodies (rounded rect over a
    # translucent QWidget). The mini player runs a touch lighter than
    # the main window so the two surfaces don't read as the same depth.
    body_color: tuple[int, int, int, int]  # main window
    mini_body_color: tuple[int, int, int, int]  # floating mini player
    dialog_body_color: tuple[int, int, int, int]  # settings + cast dialogs

    # ── Behaviour ─────────────────────────────────────────────────────
    # Whether this theme asks the compositor to blur behind the window.
    # True only for the frosted theme(s) — blurred glass is exactly
    # what separates Frosted from Transparent (clear glass). Applied
    # via modules/blur/; a silent no-op where the compositor has no
    # blur protocol.
    blur: bool


# ── Shared dark-family tokens ─────────────────────────────────────────
# The three dark themes differ only in surface/border depth and body
# opacity; every other token is identical. They all splat this dict so
# a value lives in exactly one place. A future light `Theme` provides
# its own — the constructor requires every field, so a half-authored
# light theme fails loudly instead of silently inheriting dark values.
_DARK_TOKENS = dict(
    text="#ffffff",
    text_dim="rgba(255,255,255,0.7)",
    text_faint="rgba(255,255,255,0.4)",
    idle_text="#a8a8a8",
    error_fg="#f87171",
    warn_fg="#e0735c",
    bg_card="rgba(255,255,255,0.04)",
    # Interactive-control washes. The hover/pressed pair switched
    # 2026-05-17 from translucent-white to a mid-grey at 92% opacity so
    # volume / cast / mini-player highlights AND the volume popup
    # containers share one cohesive fill that pops cleanly off the dark
    # surface behind.
    wash_hover="rgba(58, 60, 68, 0.92)",
    wash_pressed="rgba(72, 74, 82, 0.92)",
    hover_subtle="rgba(255,255,255,0.06)",
    hover_list_row="rgba(255,255,255,0.04)",
    selected_row="rgba(255,255,255,0.10)",
    pressed_white="rgba(255,255,255,0.12)",
    surface_input="rgba(255,255,255,0.05)",
    surface_input_focus="rgba(255,255,255,0.07)",
    disabled_fg="rgba(255,255,255,0.30)",
    slider_groove="rgba(255,255,255,0.20)",
    overlay_dark="rgba(0,0,0,0.65)",
    overlay_dark_hover="rgba(0,0,0,0.85)",
    popup_opaque_fill="rgba(20,22,26,1.0)",
)


# Default accent: a slightly-subdued violet (#967de1). Was violet-400
# (#a78bfa) — that read as too bright on dark backgrounds where the
# accent shows up at full-bleed (Sign in button, accent icons,
# selected-row backgrounds). Each accent_color setting overrides
# this at runtime via get_active_theme().
_DEFAULT_ACCENT = "#967de1"
_DEFAULT_ACCENT_DEEP = "#7c66d0"


FROSTED_DARK = Theme(
    name="frosted_dark",
    label="Frosted dark",
    accent=_DEFAULT_ACCENT,
    accent_deep=_DEFAULT_ACCENT_DEEP,
    border_accent="rgba(150,125,225,0.35)",
    bg="#101010",
    bg_panel="#1a1a1a",
    border="rgba(255,255,255,0.08)",
    **_DARK_TOKENS,
    # Opacity ~67% body / ~83% dialog — see-through enough that the
    # wallpaper warms the chrome and the frosted feel reads clearly
    # even without KWin blur (we run native Wayland; `org_kde_kwin_blur`
    # has no PySide6 binding yet). Still opaque enough to stay legible.
    body_color=(18, 18, 18, 172),
    mini_body_color=(22, 22, 22, 184),
    # Dialogs (settings, cast) sit on top of the main window's body —
    # text-heavy and read in isolation, so they stay the most opaque
    # of the three so the boundary with the host reads cleanly.
    dialog_body_color=(12, 12, 12, 212),
    blur=True,  # frosted glass = blurred glass
)

DARK = Theme(
    name="dark",
    label="Solid dark",
    accent=_DEFAULT_ACCENT,
    accent_deep=_DEFAULT_ACCENT_DEEP,
    border_accent="rgba(150,125,225,0.45)",
    bg="#101010",
    bg_panel="#181818",
    border="rgba(255,255,255,0.10)",
    **_DARK_TOKENS,
    body_color=(16, 16, 16, 255),
    mini_body_color=(20, 20, 20, 255),
    dialog_body_color=(18, 18, 18, 255),
    blur=False,  # fully opaque — nothing behind to blur
)

TRANSPARENT = Theme(
    name="transparent",
    label="Transparent",
    accent=_DEFAULT_ACCENT,
    accent_deep=_DEFAULT_ACCENT_DEEP,
    border_accent="rgba(150,125,225,0.30)",
    bg="#101010",
    bg_panel="#202020",
    border="rgba(255,255,255,0.06)",
    **_DARK_TOKENS,
    # The main window is the base layer — it can be very see-through
    # (~43%) for the glass look. The mini player and dialogs *stack on
    # top* of the window (and other apps), so they need enough body
    # opacity to stay legible against whatever's behind them: the mini
    # player ~76%, settings/cast dialogs ~88% (text-heavy, read in
    # isolation). They still read as translucent — just not glass.
    body_color=(20, 20, 20, 110),
    mini_body_color=(24, 24, 24, 194),
    dialog_body_color=(20, 20, 20, 224),
    blur=False,  # clear glass — Transparent is deliberately un-blurred
)


# ── Shared light-family tokens ────────────────────────────────────────
# Mirror of _DARK_TOKENS for the light family. These are FIRST-DRAFT
# values — Phase 4 of the theming rework (see docs/TODO.md). They're
# authored to be legible and structurally complete, then tuned live in
# the app, not treated as final. "Ink" flips to near-black, so the
# ~170 literals routed through ink_alpha() invert automatically; these
# tokens cover everything ink_alpha() doesn't.
_LIGHT_TOKENS = dict(
    # Text + idle ink start at pure black: get every surface matched
    # and legible first, then dial back toward grey once the whole
    # light family reads consistently (Phase 4 tuning, 2026-05-22).
    text="#000000",
    text_dim="#000000",
    text_faint="#000000",
    idle_text="#000000",
    # Error / warning foregrounds are darkened vs the dark family —
    # the dark theme's #f87171 / #e0735c wash out on a light surface.
    error_fg="#dc2626",
    warn_fg="#c2410c",
    bg_card="rgba(0,0,0,0.04)",
    # Interactive washes are solid light-greys (zinc-200 / zinc-300)
    # so control highlights read cleanly off the white surface, the
    # mirror of the dark family's mid-grey washes.
    wash_hover="rgba(228,228,231,0.95)",
    wash_pressed="rgba(212,212,216,0.95)",
    hover_subtle="rgba(0,0,0,0.05)",
    hover_list_row="rgba(0,0,0,0.04)",
    selected_row="rgba(0,0,0,0.08)",
    pressed_white="rgba(0,0,0,0.10)",
    surface_input="rgba(0,0,0,0.04)",
    surface_input_focus="rgba(0,0,0,0.06)",
    disabled_fg="rgba(0,0,0,0.30)",
    slider_groove="rgba(0,0,0,0.18)",
    # Cover-art overlays sit on album art, not the theme surface, so
    # they stay dark in both families for icon legibility over photos.
    overlay_dark="rgba(0,0,0,0.55)",
    overlay_dark_hover="rgba(0,0,0,0.72)",
    popup_opaque_fill="rgba(250,250,252,1.0)",
)


FROSTED_LIGHT = Theme(
    name="frosted_light",
    label="Frosted light",
    accent=_DEFAULT_ACCENT,
    accent_deep=_DEFAULT_ACCENT_DEEP,
    border_accent="rgba(150,125,225,0.40)",
    bg="#f4f4f6",
    bg_panel="#ffffff",
    border="rgba(0,0,0,0.10)",
    **_LIGHT_TOKENS,
    # Light frosted glass — body see-through enough that the wallpaper
    # tints it; dialogs the most opaque of the three (text-heavy).
    # Opacity mirrors FROSTED_DARK: ~67% body / ~83% dialog.
    body_color=(244, 244, 246, 172),
    mini_body_color=(248, 248, 250, 184),
    dialog_body_color=(252, 252, 254, 212),
    blur=True,  # frosted glass = blurred glass
)

LIGHT = Theme(
    name="light",
    label="Solid light",
    accent=_DEFAULT_ACCENT,
    accent_deep=_DEFAULT_ACCENT_DEEP,
    border_accent="rgba(150,125,225,0.50)",
    bg="#f4f4f6",
    bg_panel="#ffffff",
    border="rgba(0,0,0,0.12)",
    **_LIGHT_TOKENS,
    body_color=(244, 244, 246, 255),
    mini_body_color=(250, 250, 252, 255),
    dialog_body_color=(252, 252, 254, 255),
    blur=False,  # fully opaque — nothing behind to blur
)

TRANSPARENT_LIGHT = Theme(
    name="transparent_light",
    label="Transparent light",
    accent=_DEFAULT_ACCENT,
    accent_deep=_DEFAULT_ACCENT_DEEP,
    border_accent="rgba(150,125,225,0.35)",
    bg="#f4f4f6",
    bg_panel="#ffffff",
    border="rgba(0,0,0,0.08)",
    **_LIGHT_TOKENS,
    # The window is the base layer — very see-through for the glass
    # look; mini player and dialogs stack on top so they keep enough
    # body to stay legible against whatever's behind.
    body_color=(248, 248, 250, 122),
    mini_body_color=(250, 250, 252, 200),
    dialog_body_color=(250, 250, 252, 228),
    blur=False,  # clear glass — deliberately un-blurred
)


THEMES: dict[str, Theme] = {
    FROSTED_DARK.name: FROSTED_DARK,
    DARK.name: DARK,
    TRANSPARENT.name: TRANSPARENT,
    FROSTED_LIGHT.name: FROSTED_LIGHT,
    LIGHT.name: LIGHT,
    TRANSPARENT_LIGHT.name: TRANSPARENT_LIGHT,
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
    ("Blue", "#0093c6"),  # was #00a4dc (Jellyfin classic)
    ("Teal", "#1eb1ab"),  # was #22c5be
    ("Green", "#2fbe8a"),  # was #34d399
    ("Pink", "#dc66a4"),  # was #f472b6
    ("Orange", "#e28336"),  # was #fb923c
    ("Red", "#d73d3d"),  # was #ef4444
]


def _hex_to_rgb(hex_str: str) -> tuple[int, int, int]:
    h = hex_str.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _darken(hex_str: str, factor: float = 0.85) -> str:
    r, g, b = _hex_to_rgb(hex_str)
    return f"#{int(r * factor):02x}{int(g * factor):02x}{int(b * factor):02x}"


def _border_accent_for(hex_str: str, alpha: float) -> str:
    r, g, b = _hex_to_rgb(hex_str)
    return f"rgba({r},{g},{b},{alpha})"


# Original border_accent alpha per theme — preserve when overriding so
# the relative emphasis stays intact across accent changes.
_BORDER_ALPHAS = {
    "frosted_dark": 0.35,
    "dark": 0.45,
    "transparent": 0.30,
    "frosted_light": 0.40,
    "light": 0.50,
    "transparent_light": 0.35,
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


def ink_alpha(a: float) -> str:
    """Return the active theme's foreground "ink" colour at alpha ``a``
    as a QSS ``rgba(...)`` string.

    "Ink" is the colour that contrasts the background — white on the
    dark themes, near-black on a light theme — taken from the theme's
    ``text`` token. Use this for every dimmed-text / subtle-wash /
    hairline-border value that used to be a hardcoded
    ``rgba(255,255,255,a)`` literal: on the dark themes it resolves to
    exactly that (no visual change), and on a light theme it flips to
    a dark tint automatically.

    Reads the live ``ui_helpers.TEXT`` token (which ``refresh_theme()``
    keeps current) rather than re-resolving the whole theme — this is
    called dozens of times per QSS rebuild, so it must stay cheap. A
    live theme swap is picked up via ``refresh_theme()``; callers that
    bake the result into a QSS string re-stamp on ``theme_changed``
    (the per-surface ``_reapply_accent`` contract).

    Never raises — a QSS-building helper that throws would take down
    widget construction. Any failure (e.g. ui_helpers mid-import)
    falls back to white, the dark-theme value."""
    try:
        from modules import ui_helpers

        r, g, b = _hex_to_rgb(ui_helpers.TEXT)
    except Exception:
        r, g, b = (255, 255, 255)
    return f"rgba({r},{g},{b},{a})"


def ink_rgb() -> tuple[int, int, int]:
    """The active theme's foreground "ink" as an ``(r, g, b)`` tuple —
    the QColor-paint counterpart of :func:`ink_alpha`.

    ``paintEvent`` code builds ``QColor(...)`` directly and can't take a
    QSS ``rgba()`` string, so a delegate that wants theme-aware ink does
    ``QColor(*ink_rgb(), alpha)``. White on the dark themes (no visual
    change from the old hardcoded ``QColor(255,255,255,a)``), near-black
    on a light theme. Reads the live ``ui_helpers.TEXT`` token, so a
    delegate that repaints on ``theme_changed`` flips for free.

    Never raises — falls back to white (the dark-theme value) on any
    failure, matching :func:`ink_alpha`."""
    try:
        from modules import ui_helpers

        return _hex_to_rgb(ui_helpers.TEXT)
    except Exception:
        return (255, 255, 255)
