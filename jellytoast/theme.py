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

Live theme switching IS wired: ``ui_helpers.refresh_theme()`` re-reads every
token in place and a ``PlayerBus.theme_changed`` emit re-stamps the whole app,
so a theme-mode change — and the OS-driven ``"auto"`` (follow-OS) swap — applies
with no restart. Only ``font_scale`` still needs a relaunch.
"""

import functools
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING

from jellytoast.platform_compat import IS_MACOS, IS_WINDOWS

if TYPE_CHECKING:
    from PySide6.QtGui import QColor

# On Windows a *fully* transparent (alpha-0) pixel of a layered window
# (frameless + WA_TranslucentBackground) is click-THROUGH — mouse events fall
# straight to whatever is behind it. A 0-alpha body therefore makes the
# frameless top bar's drag gaps and the translucent tooltips/popups stop
# receiving clicks (the window can't be dragged; popups flash as ghost
# windows). So the Windows body keeps a tiny floor alpha: visually still
# "Mica is the background", but every pixel stays hit-testable.
_WIN_BODY_FLOOR_ALPHA = 16
# Default Windows body tint when Acrylic is active. The old default sat at the
# hit-test floor (16, ~6%), which read as washed-out — the dark theme's fill
# barely showed over the lighter Acrylic backdrop. Default to a clearly darker
# glass (96, ~38%) so the dark theme reads as dark on Windows while staying
# translucent enough for the Acrylic blur to show through. Tune live with
# JT_WIN_GLASS_ALPHA, then bake the value you like.
_WIN_BODY_DEFAULT_ALPHA = 96


def _win_glass_alpha() -> int:
    """Windows-only frosted body alpha when Acrylic is active. Defaults to
    ``_WIN_BODY_DEFAULT_ALPHA`` (dark but translucent) and is clamped to
    ``[_WIN_BODY_FLOOR_ALPHA, 255]`` — the floor keeps every pixel hit-testable
    (a 0-alpha frameless body is click-through). Env-tunable both directions:
    ``JT_WIN_GLASS_ALPHA=40`` (lighter) … ``=160`` (heavier)."""
    try:
        v = int(os.environ.get("JT_WIN_GLASS_ALPHA", str(_WIN_BODY_DEFAULT_ALPHA)))
    except ValueError:
        v = _WIN_BODY_DEFAULT_ALPHA
    return max(_WIN_BODY_FLOOR_ALPHA, min(255, v))


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
    # The main window, the mini player, and the settings / cast dialogs
    # each paint their own body. The frosted themes give all three ONE
    # shared fill (cohesive glass — blur carries legibility); the solid
    # themes are fully opaque.
    body_color: tuple[int, int, int, int]  # main window
    mini_body_color: tuple[int, int, int, int]  # floating mini player
    dialog_body_color: tuple[int, int, int, int]  # settings + cast dialogs

    # ── Behaviour ─────────────────────────────────────────────────────
    # Whether this theme asks the compositor to blur behind the window.
    # True only for the frosted theme(s) — blurred glass is exactly what
    # separates Frosted from Solid. Applied via jellytoast/blur/; a silent
    # no-op where the compositor has no blur protocol.
    blur: bool

    # Frosted-only: the body alpha to fall back to when a real compositor
    # backdrop is NOT verified behind the window (blur disabled, no blur
    # protocol, non-KDE desktop, Windows < 11). The frosted body_color
    # above carries the *glass* alpha (~67%) that relies on blur for
    # legibility; without blur that reads as a broken see-through window,
    # so we swap in this near-opaque alpha (~92%) and the surface still
    # reads as a dark/light frosted panel. None on non-frosted themes —
    # status is irrelevant where nothing rides a backdrop. See
    # body_color_for() and jellytoast/blur/status().
    fallback_body_alpha: int | None = None

    # True for the dark family (frosted_dark / dark), False for the light
    # family (frosted_light / light). Drives the Windows Mica variant —
    # DWM's immersive dark/light backdrop — so light themes get light Mica.
    # Defaults True; the two light themes set it False.
    dark: bool = True


# ── Shared dark-family tokens ─────────────────────────────────────────
# The three dark themes differ only in surface/border depth and body
# opacity; every other token is identical. They all splat this dict so
# a value lives in exactly one place. A future light `Theme` provides
# its own — the constructor requires every field, so a half-authored
# light theme fails loudly instead of silently inheriting dark values.
# ── Elevated-surface shared knob (dark modes) ──────────────────────
# ONE source of truth for every "elevated" surface in dark themes —
# button hovers, list-row highlights, hover tooltips, dropdown popups,
# the volume slider panel. They all read this constant, so tweaking
# the alpha here moves all of them in unison.
#
# Sits on top of the (already-blurred-in-frosted) body as a soft LIGHT
# wash that LIFTS the surface a notch — the same look the Settings
# left-nav uses for the selected row (``ink_alpha(0.10)``). Reads as
# "panel lifted off the body" rather than "panel pressed into a hole",
# which is the visual the user picked as the project-wide hover/
# selection tone. Top-level popups (combo popups, QMenus, QToolTip)
# bake the body+wash COMPOSITE to opaque (``_DARK_ELEVATED_TOPLEVEL``)
# because Wayland compositor blur is fragile on those surfaces.
_DARK_ELEVATED_ALPHA = 0.10
_DARK_ELEVATED = f"rgba(255, 255, 255, {_DARK_ELEVATED_ALPHA})"
# Pressed sits a touch brighter than hover (lighter wash) so the
# pressed feedback still reads against a hovered button — same
# polarity as the light family.
_DARK_ELEVATED_PRESSED = f"rgba(255, 255, 255, {_DARK_ELEVATED_ALPHA + 0.05})"
# Frosted-theme TOP-LEVEL elevated fill — for tooltip / menu / combo
# popup surfaces that don't sit on top of the main window body. The
# value is the body+wash COMPOSITE expressed as a translucent rgba
# so the surface can ride compositor blur and read as ACTUAL frosted
# glass (matching how the volume popup, button hover, and Settings
# left-nav selected row look).
#
# The raw composite math (body 18,18,18,0.675 under _DARK_ELEVATED
# 255,255,255,0.10) yields rgba(51,51,51,0.71). But what's behind a
# top-level popup is the BLURRED MAIN WINDOW CONTENT (body + album
# art + text), which sits darker than a button-highlight surface
# composites over (just body + wallpaper). To make the tooltip read
# at the same tone as the in-window highlight, we lighten the
# painted colour a few points and drop the alpha so more wallpaper
# shows through — visually matching the highlight regardless of the
# darker content underneath the popup. Slight cool tint mirrors how
# the volume popup picks up wallpaper warmth/cool through its wash.
# NEUTRAL gray (no tint): the button hover highlight is a pure white
# wash with no hue, so every elevated popup (tooltip, menu, volume
# popup) matches it by staying neutral — an earlier cool/blue cast
# (64/67/74) read "cooler" than the button (reported 2026-06-07).
# Luminance preserved from that prior value.
_DARK_ELEVATED_TOPLEVEL = "rgba(67, 67, 67, 0.65)"
# Companion to _DARK_ELEVATED — the OPAQUE flavour used by every
# TOP-LEVEL elevated surface (QToolTip, QMenu, combo popups). These
# can't reliably be translucent + compositor-blurred on Wayland:
#   - QTipLabel inherits surface translucency from the owning widget
#     tree, so a translucent QSS composites directly with the desktop
#     for top-bar tooltips (reads as floating text with no backdrop).
#   - QComboBox popup windows + QMenus have several internal autofill
#     paths (popup QFrame, view, viewport) that paint opaque before
#     the QSS, defeating WA_TranslucentBackground.
# So they all read off this single opaque constant — tuned to LOOK
# like _DARK_ELEVATED would if it were composited over the blurred
# body (which is what volume-popup-style child widgets achieve for
# free). Adjust here to retint every menu, dropdown, and tooltip in
# unison.
# Neutral (no blue cast) to match the button hover highlight — see
# _DARK_ELEVATED_TOPLEVEL. Luminance preserved from the prior 28/30/34.
_DARK_POPUP_OPAQUE = "rgb(30, 30, 30)"


_DARK_TOKENS = dict(
    text="#ffffff",
    text_dim="rgba(255,255,255,0.7)",
    text_faint="rgba(255,255,255,0.4)",
    idle_text="#a8a8a8",
    error_fg="#f87171",
    warn_fg="#e0735c",
    bg_card="rgba(255,255,255,0.04)",
    # Every elevated dark-mode surface flows from _DARK_ELEVATED —
    # button hover, volume popup body, list-row highlight, dropdown
    # popup, tooltip. Adjust the constant above to retint all of them
    # together. ``hover_subtle`` stays a faint white wash because it
    # marks "could click this" on small inline targets where a dark
    # press-down read would compete with the body fill.
    wash_hover=_DARK_ELEVATED,
    wash_pressed=_DARK_ELEVATED_PRESSED,
    hover_subtle="rgba(255,255,255,0.06)",
    hover_list_row=_DARK_ELEVATED,
    selected_row=_DARK_ELEVATED,
    pressed_white="rgba(255,255,255,0.12)",
    surface_input="rgba(255,255,255,0.05)",
    surface_input_focus="rgba(255,255,255,0.07)",
    disabled_fg="rgba(255,255,255,0.30)",
    slider_groove="rgba(255,255,255,0.20)",
    overlay_dark="rgba(0,0,0,0.65)",
    overlay_dark_hover="rgba(0,0,0,0.85)",
    # Top-level popups go OPAQUE via _DARK_POPUP_OPAQUE rather than
    # the translucent _DARK_ELEVATED — Wayland surface translucency
    # for combo popups / QMenus / tooltips is too fragile (see
    # _DARK_POPUP_OPAQUE docstring). The opaque value is tuned to
    # match what _DARK_ELEVATED looks like over the blurred body, so
    # popups still read as cohesive with hover/highlight surfaces
    # that DO use the translucent path.
    popup_opaque_fill=_DARK_POPUP_OPAQUE,
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
    **{k: v for k, v in _DARK_TOKENS.items() if k != "popup_opaque_fill"},
    # Frosted-theme popup override: top-level elevated popups (combo
    # popups, QMenus, QToolTip) read an OPAQUE composited tone
    # (body + wash baked) rather than raw _DARK_ELEVATED. Reasons:
    #   - In-window elevated surfaces (Albums hover, volume popup)
    #     paint _DARK_ELEVATED on top of the body; raw _DARK_ELEVATED
    #     here reads LIGHTER because no body sits between wash and
    #     wallpaper.
    #   - Wayland compositor blur on top-level popups is fragile
    #     across QTipLabel reuse + parent-chain variation, so we don't
    #     depend on it — opaque rgb(9,9,9) gives a consistent result
    #     regardless of whether blur installed for that popup.
    popup_opaque_fill=_DARK_ELEVATED_TOPLEVEL,
    # Opacity ~67% body / ~83% dialog — see-through enough that the
    # wallpaper warms the chrome and the frosted feel reads clearly
    # even without KWin blur (we run native Wayland; `org_kde_kwin_blur`
    # has no PySide6 binding yet). Still opaque enough to stay legible.
    # Every frosted surface — window body, mini player, dialogs —
    # shares ONE fill so the whole UI reads as one cohesive sheet of
    # glass. Legibility comes from the compositor blur behind it, not
    # from stacking up opacity.
    body_color=(18, 18, 18, 172),
    mini_body_color=(18, 18, 18, 172),
    dialog_body_color=(18, 18, 18, 172),
    blur=True,  # frosted glass = blurred glass
    # No verified blur → paint ~92% instead of ~67% so the dark frosted
    # body never goes see-through. Still a hair translucent at the edges,
    # so it reads as a frosted panel rather than flat Solid dark.
    fallback_body_alpha=236,
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

# ── Shared light-family tokens ────────────────────────────────────────
# Mirror of _DARK_TOKENS for the light family. Authored as first-draft
# Phase-4 values (legible + structurally complete) and tuned live in
# the app since — not treated as final. "Ink" flips to near-black, so the
# ~170 literals routed through ink_alpha() invert automatically; these
# tokens cover everything ink_alpha() doesn't.

# ── Elevated-surface shared knob (light modes) ─────────────────────
# Light-family parallel to _DARK_ELEVATED. The dark family darkens
# the body to mark elevation ("darker blurred glass"); light flips
# the polarity and *brightens* the body with a translucent white
# wash, so combo popups, hovered list rows, the volume popup body,
# selected rows, etc. all read as "a pinch brighter than the body"
# without losing the frosted look. Tweaking the alpha here moves
# every elevated surface in the light family together.
_LIGHT_ELEVATED_ALPHA = 0.55
# Cool-tinted white wash, not pure (255,255,255). Pure white over a
# wallpaper-tinted frosted body washes out the body's tint entirely,
# so elevated regions stop reading as "frosted glass" and read as
# "solid white panel." Tinting the wash off-white preserves the
# frosted feel while still elevating.
_LIGHT_ELEVATED = f"rgba(248, 250, 254, {_LIGHT_ELEVATED_ALPHA})"
# Pressed sits a touch brighter than hover so press feedback reads
# against a hovered surface — same pattern as the dark family, just
# the opposite direction on the alpha axis.
_LIGHT_ELEVATED_PRESSED = f"rgba(248, 250, 254, {_LIGHT_ELEVATED_ALPHA + 0.15})"
# Light-family parallel to _DARK_ELEVATED_TOPLEVEL — translucent
# composited tone (body + wash) so top-level popups read as actual
# frosted glass when painted over compositor blur. Body
# (244,244,246,~0.55) under _LIGHT_ELEVATED (248,250,254,0.55)
# composites to ≈ (247,248,252) at ~80% alpha.
# Neutral (no cool cast) to match the button hover highlight — see
# _DARK_ELEVATED_TOPLEVEL. Luminance preserved from the prior value.
_LIGHT_ELEVATED_TOPLEVEL = "rgba(248, 248, 248, 0.80)"
# Opaque flavour for top-level popups (QToolTip, QMenu, combo popups).
# Same Wayland fragility as dark — translucent + compositor-blurred
# popups don't behave; an opaque value that LOOKS like
# _LIGHT_ELEVATED composited over the frosted body is the workaround.
# NEUTRAL (no cool cast) to match the button hover highlight, same as
# the dark family — see _DARK_ELEVATED_TOPLEVEL. Luminance preserved
# from the prior 234/238/246.
_LIGHT_POPUP_OPAQUE = "rgb(238, 238, 238)"

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
    # Every elevated light-mode surface flows from _LIGHT_ELEVATED —
    # mirror of the dark family. Hovered buttons, list-row highlights,
    # selected rows, the volume popup body, dropdown popups. Adjust
    # the constant above to retint all of them together.
    # ``hover_subtle`` stays a faint ink wash because it marks "could
    # click this" on small inline targets where the brighter elevated
    # wash would over-light a tiny region.
    wash_hover=_LIGHT_ELEVATED,
    wash_pressed=_LIGHT_ELEVATED_PRESSED,
    hover_subtle="rgba(0,0,0,0.05)",
    hover_list_row=_LIGHT_ELEVATED,
    selected_row=_LIGHT_ELEVATED,
    pressed_white="rgba(0,0,0,0.10)",
    surface_input="rgba(0,0,0,0.04)",
    surface_input_focus="rgba(0,0,0,0.06)",
    disabled_fg="rgba(0,0,0,0.30)",
    slider_groove="rgba(0,0,0,0.18)",
    # Cover-art overlays sit on album art, not the theme surface, so
    # they stay dark in both families for icon legibility over photos.
    overlay_dark="rgba(0,0,0,0.55)",
    overlay_dark_hover="rgba(0,0,0,0.72)",
    # Top-level popups go OPAQUE via _LIGHT_POPUP_OPAQUE rather than
    # the translucent _LIGHT_ELEVATED — see _DARK_POPUP_OPAQUE for the
    # Wayland-fragility background. Tuned to match what _LIGHT_ELEVATED
    # would look like over the frosted body.
    popup_opaque_fill=_LIGHT_POPUP_OPAQUE,
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
    **{k: v for k, v in _LIGHT_TOKENS.items() if k != "popup_opaque_fill"},
    # Frosted-theme popup override — see FROSTED_DARK for rationale.
    # Opaque composited (body + wash) tone so top-level popups read at
    # the same depth as in-window elevated surfaces without depending
    # on fragile Wayland compositor blur.
    popup_opaque_fill=_LIGHT_ELEVATED_TOPLEVEL,
    # One shared frosted fill across every surface — see FROSTED_DARK.
    # Lower alpha than the dark family: a light fill washes the
    # wallpaper toward white faster than a dark fill darkens it, so
    # light frosted needs to be more see-through to let the same
    # amount of desktop colour read through.
    body_color=(244, 244, 246, 140),
    mini_body_color=(244, 244, 246, 140),
    dialog_body_color=(244, 244, 246, 140),
    blur=True,  # frosted glass = blurred glass
    # Light frosted is more see-through than dark (alpha 140), so its
    # no-blur fallback needs to climb higher to stay legible as a panel.
    fallback_body_alpha=240,
    dark=False,
)

LIGHT = Theme(
    name="light",
    label="Solid light",
    dark=False,
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

THEMES: dict[str, Theme] = {
    FROSTED_DARK.name: FROSTED_DARK,
    DARK.name: DARK,
    FROSTED_LIGHT.name: FROSTED_LIGHT,
    LIGHT.name: LIGHT,
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


@functools.lru_cache(maxsize=256)
def _hex_to_rgb(hex_str: str) -> tuple[int, int, int]:
    """Parse ``#rrggbb`` (or ``rrggbb``) into an int triple. lru_cached
    because paint loops + lyrics restyle + QSS rebuilds call this
    thousands of times per second and the input set is tiny (a couple
    dozen theme tokens at most). The cache is keyed by the hex string,
    so when ``refresh_theme()`` rebinds ``ui_helpers.TEXT`` to a new
    hex the next call just sees a cache miss for the new key and
    populates — no manual invalidation needed."""
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
    "frosted_light": 0.40,
    "light": 0.50,
}


def os_color_scheme() -> str:
    """The OS light/dark preference as ``"dark"`` / ``"light"``, read via
    Qt's ``QStyleHints.colorScheme()`` — cross-platform (Windows registry,
    KDE/GNOME via the freedesktop appearance portal, macOS). Returns
    ``"dark"`` as a safe default when there's no QApplication yet or the
    scheme is Unknown. Used by the ``"auto"`` theme mode to follow the OS;
    pair with ``QStyleHints.colorSchemeChanged`` for live updates."""
    try:
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QGuiApplication

        app = QGuiApplication.instance()
        if app is None:
            return "dark"
        if app.styleHints().colorScheme() == Qt.ColorScheme.Light:
            return "light"
        return "dark"
    except Exception:
        return "dark"


def get_active_theme() -> Theme:
    """Return the Theme matching ``settings.theme_mode``, or the default
    if the saved name is unknown. ``settings.accent_color`` overrides
    the theme's ``accent`` / ``accent_deep`` / ``border_accent`` triple
    in-place so a user can pick a non-default accent without forking a
    whole theme.

    ``theme_mode == "auto"`` follows the OS light/dark setting, resolving
    to ``frosted_light`` / ``frosted_dark`` via ``os_color_scheme()``. Theme
    + accent are read live: ``ui_helpers.refresh_theme()`` + a
    ``PlayerBus.theme_changed`` emit re-stamp the whole app, so a theme (or
    OS-scheme) change applies without a restart.
    """
    from dataclasses import replace as _replace

    from jellytoast.settings import get_settings

    s = get_settings()
    mode = s.theme_mode
    if mode == "auto":
        mode = "frosted_light" if os_color_scheme() == "light" else "frosted_dark"
    base = THEMES.get(mode, DEFAULT_THEME)
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


_BODY_ATTR = {
    "main": "body_color",
    "mini": "mini_body_color",
    "dialog": "dialog_body_color",
}


def body_color_for(theme: "Theme", status, surface: str = "main") -> tuple:
    """The RGBA body fill for a painted surface, given the live blur
    ``status`` (a ``jellytoast.blur.BlurStatus``).

    For a frosted theme the body alpha is a FUNCTION of whether a real
    compositor/OS backdrop is verified behind the window:

      * ``status is ACTIVE`` → the theme's stored glass alpha (~67%); the
        body rides the blur and reads as true frosted glass.
      * otherwise → the theme's ``fallback_body_alpha`` (~92%) so the
        frosted body stays a legible dark/light panel instead of going
        see-through-broken. This is the fix for "Frosted dark renders
        transparent on a box without working blur."

    Non-frosted themes (``theme.blur`` False) and any theme without a
    fallback alpha return their stored body_color unchanged — ``status``
    is irrelevant where nothing rides a backdrop. ``surface`` selects the
    main window / mini player / dialog body. Never raises."""
    attr = _BODY_ATTR.get(surface, "body_color")
    base = getattr(theme, attr)
    if not theme.blur or theme.fallback_body_alpha is None:
        return base
    # Import here so theme.py stays importable before jellytoast.blur is ready
    # (ui_helpers imports theme very early in startup).
    from jellytoast.blur import BlurStatus

    if status is BlurStatus.ACTIVE:
        if IS_WINDOWS:
            # Windows Mica is subtler than KWin blur — cap the body alpha
            # lower so it reads through (see _win_glass_alpha). min() keeps a
            # theme that's already lighter than the cap.
            return (base[0], base[1], base[2], min(base[3], _win_glass_alpha()))
        return base
    # macOS: native vibrancy is off (faux-frost everywhere). The main window and
    # the mini player BOTH knock their frost base back ×0.85 in their own paint
    # (app._faux_frost_base / mini_player.paintEvent), so at the SAME
    # body_color_tuple alpha they already render identical glass — do NOT reduce
    # "main" here or it double-applies and the main reads more transparent than
    # the mini. Only push dialogs/popups the OTHER way — near-opaque so the
    # surface behind them doesn't bleed through. Mini + Linux/Windows unchanged.
    alpha = theme.fallback_body_alpha
    if IS_MACOS and surface == "dialog":
        alpha = min(255, alpha + 14)
    return (base[0], base[1], base[2], alpha)


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
        from jellytoast import ui_helpers

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
        from jellytoast import ui_helpers

        return _hex_to_rgb(ui_helpers.TEXT)
    except Exception:
        return (255, 255, 255)


def contrast_ink(bg) -> "QColor":
    """Foreground ink (white or near-black) that contrasts best with a colour
    fill `bg` (a QColor) — for a glyph drawn ON an accent/colour, e.g. the
    download badge's down-arrow or the eyedropper swatch glyph.

    Uses WCAG relative luminance, so it actually clears the contrast bar on the
    mid-luminance accent presets (green/teal/orange) where a hardcoded white
    arrow went sub-AA (~2.4–2.8:1). White only wins on quite dark fills
    (luminance < ~0.18); everything else gets near-black."""
    from PySide6.QtGui import QColor

    def _lin(c: float) -> float:
        cs = c / 255.0
        return cs / 12.92 if cs <= 0.03928 else ((cs + 0.055) / 1.055) ** 2.4

    lum = (
        0.2126 * _lin(bg.red())
        + 0.7152 * _lin(bg.green())
        + 0.0722 * _lin(bg.blue())
    )
    return QColor("#ffffff") if lum < 0.179 else QColor("#1a1a1a")
