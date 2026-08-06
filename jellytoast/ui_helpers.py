"""
Shared UI helpers: theme, async image loader, formatting, common widgets.
"""

import contextlib
import logging
import shutil
import subprocess
from collections import OrderedDict
from typing import Callable, Optional

logger = logging.getLogger(__name__)

from PySide6.QtCore import (
    Property,
    QCoreApplication,
    QEvent,
    QPropertyAnimation,
    QRect,
    QRectF,
    Qt,
    QTimer,
    QUrl,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QFont,
    QGuiApplication,
    QImage,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPalette,
    QPixmap,
)
from PySide6.QtNetwork import QNetworkReply, QNetworkRequest
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QScrollArea,
    QScrollBar,
    QSlider,
    QStyle,
    QStyleOptionSlider,
    QVBoxLayout,
    QWidget,
)

from jellytoast import image_cache as _disk_image_cache
from jellytoast.async_io import get_qnam

# ── Theme ────────────────────────────────────────────────────────────────────
# Palette + body fills come from the active Theme (jellytoast/theme.py).
# Constants are re-exported so existing `from jellytoast.ui_helpers import
# TEXT, ACCENT, ...` callers don't have to change.
#
# These names are mutated in place by ``refresh_theme()`` when the user
# picks a new accent — the live-apply path: settings → refresh_theme()
# → bus.theme_changed → subscribers re-pull the current values. Since
# callers typically grab the constants once at __init__ and splat them
# into QSS strings, the subscriber has to re-run its styling code; the
# new module-level values are what it'll read.
from jellytoast.design_tokens import rad
from jellytoast.icon_button import IconButton
from jellytoast.platform_compat import IS_MACOS
from jellytoast.theme import get_active_theme, ink_alpha  # noqa: F401  (re-exported)

_THEME = get_active_theme()


def _popup_fill_opaque_on_macos(fill: str) -> str:
    """Force a popup body fill near-opaque on macOS.

    The themed ``popup_opaque_fill`` carries ~0.65 alpha so it reads as frosted
    glass over a compositor blur. macOS has no app-controllable window blur and
    its menu / combobox popup windows are translucent, so that 0.65 lets the
    surface behind a dropdown bleed through — busy, hard-to-read popups. Bump it
    near-opaque on macOS so every popup reads clean (covers both the
    GLOBAL_STYLE bare QMenu/QComboBox path and the blur-aware
    ``popup_body_fill``). No-op off macOS, where the fill rides real blur or an
    opaque popup window. Accepts an ``rgba(r, g, b, a)`` literal; returns it
    unchanged if it can't parse. Never raises."""
    if not IS_MACOS:
        return fill
    s = fill.strip()
    if s.startswith("rgba(") and s.endswith(")"):
        parts = [p.strip() for p in s[5:-1].split(",")]
        if len(parts) == 4:
            return f"rgba({parts[0]}, {parts[1]}, {parts[2]}, 0.97)"
    return fill


# Every UI color now lives on the active Theme as a semantic token
# (jellytoast/theme.py). The constants below are flat re-exports of those
# tokens — named for the SEMANTIC they serve, not the value they hold —
# so the whole set swaps wholesale when the theme mode changes. They're
# mutated in place by ``refresh_theme()`` (see its docstring) and may be
# further overlaid by user color-token overrides via ``color_tokens``.

# ── Accent ─────────────────────────────────────────────────────────────
ACCENT = _THEME.accent
ACCENT_DEEP = _THEME.accent_deep
BORDER_ACCENT = _THEME.border_accent

# ── Surfaces ───────────────────────────────────────────────────────────
BG = _THEME.bg
BG_PANEL = _THEME.bg_panel
BG_CARD = _THEME.bg_card

# ── Text ───────────────────────────────────────────────────────────────
TEXT = _THEME.text
TEXT_DIM = _THEME.text_dim
TEXT_FAINT = _THEME.text_faint
IDLE_TEXT = _THEME.idle_text  # "Nothing playing" / idle-state foreground
ERROR_FG = _THEME.error_fg  # inline error text (login failed, etc.)
WARN_FG = _THEME.warn_fg  # warning marker — offline-mode indicator

# ── Borders ────────────────────────────────────────────────────────────
BORDER = _THEME.border

# ── Interactive washes ─────────────────────────────────────────────────
# Hover / press fills for buttons, list rows, tiles — interpolated into
# QSS. Sourcing them from one place keeps wash strength uniform across
# every surface instead of each stylesheet hardcoding its own near-but-
# not-identical value.
WASH_HOVER = _THEME.wash_hover  # icon-button hover, volume popup body
WASH_PRESSED = _THEME.wash_pressed  # icon-button pressed state
HOVER_SUBTLE = _THEME.hover_subtle  # ghost-button + library-tile hover
HOVER_LIST_ROW = _THEME.hover_list_row  # list-row hover (cast/settings)
SELECTED_ROW = _THEME.selected_row  # selected list row (non-accent)
PRESSED_WHITE = _THEME.pressed_white  # white-press button state

# ── Inputs ─────────────────────────────────────────────────────────────
SURFACE_INPUT = _THEME.surface_input  # QLineEdit / QComboBox / QSpinBox
SURFACE_INPUT_FOCUS = _THEME.surface_input_focus  # input :focus tint
DISABLED_FG = _THEME.disabled_fg  # disabled icon-button color

# ── Sliders ────────────────────────────────────────────────────────────
SLIDER_GROOVE = _THEME.slider_groove  # slider track (volume / seek / EQ)

# ── Overlays / popups ──────────────────────────────────────────────────
OVERLAY_DARK = _THEME.overlay_dark  # cover-art heart bg + downloads chip
OVERLAY_DARK_HOVER = _THEME.overlay_dark_hover  # overlay on hover
POPUP_OPAQUE_FILL = _popup_fill_opaque_on_macos(
    _THEME.popup_opaque_fill
)  # opaque popup body (near-opaque on macOS — no blur to ride)

# ── Painted body fills ─────────────────────────────────────────────────
# Used as `QColor(*BODY_COLOR)` inside paintEvent. Three slots because
# the main window, mini player, and dialogs each paint their own
# surface and read at slightly different depths.
BODY_COLOR = _THEME.body_color
MINI_BODY_COLOR = _THEME.mini_body_color
DIALOG_BODY_COLOR = _THEME.dialog_body_color


def body_color_tuple(surface: str = "main") -> tuple:
    """Status-aware RGBA body fill for a frosted painted surface.

    The single source of truth behind "Frosted never renders see-through":
    on a frosted theme the body alpha tracks whether real compositor blur is
    *verified* behind the window — glass (~67%) when it is, a near-opaque
    frosted panel (~92%) when it isn't. Non-frosted themes (Solid /
    Transparent) return their fixed body unchanged. The main window, mini
    player, and every frosted dialog all read this so they degrade together.

    Reads the live active theme + the cached blur status, so it picks up a
    theme switch and a post-show blur re-probe for free. ``surface`` selects
    main / mini / dialog. Does NOT apply the main window's JT_OPAQUE override
    (that's main-window-only; the caller handles it). Never raises."""
    from jellytoast import blur
    from jellytoast.theme import body_color_for, get_active_theme

    theme = get_active_theme()
    status = blur.status() if theme.blur else blur.BlurStatus.DISABLED
    return body_color_for(theme, status, surface)


def frosted_fallback_active() -> bool:
    """True when a frosted theme is NOT riding real compositor blur — i.e. the
    surface would otherwise paint a flat near-opaque body and should instead
    paint the faux-frost backdrop (``jellytoast/blur/_faux_frost.py``). Shared
    by the main window and mini player so they fall back identically. (The main
    window additionally suppresses it under the JT_OPAQUE override.)"""
    from jellytoast import blur
    from jellytoast.theme import get_active_theme

    if not get_active_theme().blur:
        return False
    return blur.status() is not blur.BlurStatus.ACTIVE


# Materialize the check-mark SVG to a cache file so QSS can reference
# it via image:url(...).
#
# Rasterised to PNG, not SVG: Qt's QStyleSheetStyle silently fails to
# render `image: url(file.svg)` on KDE Fusion / some Wayland builds
# (the indicator shows as solid-fill with no visible glyph). PNG
# loads via QPixmap which is the well-tested path.
#
# The stroke color is parameterised — we render one PNG per
# distinct stroke color the app asks for and cache by (color, size).
# `_check_url_for(color)` returns the on-disk path for a given hex
# color; it generates the PNG lazily on first request.
def _render_check_png(color_hex: str, size: int = 24) -> str:
    """Rasterise the checkmark SVG to a transparent PNG in the given
    stroke color and return the cached path. Caches by (color, size)
    so an accent change uses a different file → Qt picks up the new
    image instead of returning a stale cached pixmap.

    Returns empty string when called before QApplication exists
    (early imports during module load) — QPixmap requires a running
    QGuiApplication and would SIGABRT otherwise. The first post-boot
    call rasterises and caches; subsequent calls hit the disk cache
    even before QApplication if the file already exists from a
    previous run."""
    try:
        import hashlib
        import os

        from PySide6.QtCore import QByteArray, Qt
        from PySide6.QtGui import QGuiApplication, QPainter, QPixmap
        from PySide6.QtSvg import QSvgRenderer

        svg_src = (
            f'<svg viewBox="0 0 16 16" xmlns="http://www.w3.org/2000/svg">'
            f'<path d="M3 8 L7 12 L13 4" stroke="{color_hex}" '
            f'stroke-width="1.6" fill="none" stroke-linecap="round" '
            f'stroke-linejoin="round"/></svg>'
        )
        cache_dir = os.path.expanduser(
            "~/.cache/PySideApp/qss_icons"
        )
        os.makedirs(cache_dir, exist_ok=True)
        digest = hashlib.sha1(
            (svg_src + f"@{size}").encode("utf-8")
        ).hexdigest()
        out_path = os.path.join(cache_dir, f"check_{digest}.png")
        if not os.path.exists(out_path):
            # QPixmap requires QGuiApplication. If we're called
            # during module load (before main() has constructed it),
            # bail out — the file will be generated on the first
            # post-boot call.
            if QGuiApplication.instance() is None:
                return ""
            renderer = QSvgRenderer(QByteArray(svg_src.encode("utf-8")))
            pix = QPixmap(size, size)
            pix.fill(Qt.GlobalColor.transparent)
            painter = QPainter(pix)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            renderer.render(painter)
            painter.end()
            pix.save(out_path, "PNG")
        return out_path.replace("\\", "/")
    except Exception:
        return ""


def check_url_for_accent() -> str:
    """Return the PNG path for a check mark in the current ACCENT
    color. Computed at call time so a fresh URL is generated when
    the accent changes — the differing path also defeats Qt's
    QSS image-pixmap cache that otherwise sticks to the previous
    render."""
    return _render_check_png(ACCENT)


# Back-compat for any caller still referencing _CHECK_URL — empty
# at module-load time (QPixmap requires QApplication which doesn't
# exist yet at import). All callers should use check_url_for_accent()
# which lazy-renders on first call.
_CHECK_URL = ""


def _accent_rgb_tuple() -> tuple[int, int, int]:
    """Parse the active ACCENT hex into (r, g, b) so QSS rules can
    build accent-derived rgba() colours without hard-coding the
    default purple. Falls back to purple if the hex is malformed."""
    from jellytoast.theme import _hex_to_rgb

    try:
        return _hex_to_rgb(ACCENT)
    except Exception:
        return (150, 125, 225)


def _hex_to_rgb_safe(hex_value: str) -> tuple[int, int, int]:
    """Safe (r, g, b) for any hex string. Falls back to neutral grey
    if the input doesn't parse. Public helper so other modules can
    derive rgba() colours from arbitrary token values without each
    re-implementing the fallback."""
    from jellytoast.theme import _hex_to_rgb

    try:
        return _hex_to_rgb(hex_value)
    except Exception:
        return (128, 128, 128)


def _tooltip_fill_opaque() -> str:
    """Tooltip backdrop colour. Returns ``POPUP_OPAQUE_FILL`` directly,
    which the active theme picks: frosted themes diverge to a
    translucent wash (backstopped by ``apply_elevated_blur`` on
    show), solid + transparent themes keep an opaque value."""
    return POPUP_OPAQUE_FILL


def _ui_font_family_stack() -> str:
    """CSS font-family stack for the global style. Prefixes the user's chosen
    family (Settings → Display → Font) onto the built-in Inter stack; an empty
    selection ('') uses the built-in stack. Icons are SVG (not font glyphs), so
    this only ever affects text."""
    base = "'Inter', 'Segoe UI', 'Noto Sans', sans-serif"
    try:
        from jellytoast.settings import get_settings

        fam = (get_settings().font_family or "").strip()
    except Exception:
        fam = ""
    return f"'{fam}', {base}" if fam else base


# Snapshot of the platform/Qt default UI font, captured at boot BEFORE the
# user's font_family (if any) is installed via app.setFont — so a live switch
# back to "System default" can restore it (QFont("") does NOT reset the app
# font to the platform default). Set by app.main() via set_boot_default_font.
_BOOT_DEFAULT_FONT = None


def set_boot_default_font(font) -> None:
    """Record the app's default font at boot (see _BOOT_DEFAULT_FONT)."""
    global _BOOT_DEFAULT_FONT
    _BOOT_DEFAULT_FONT = font


def apply_font_settings_live() -> None:
    """Re-apply the current ``ui/font_family`` AND ``ui/font_scale`` to the
    running app WITHOUT a restart. Recomputes the size-scaled design tokens
    (``design_tokens.refresh_fonts``) and rebinds them across every module that
    imported them by value (``_propagate_font_constants``) so a scale change is
    actually seen; re-installs the application font for the family (so
    painter-drawn delegate text, which inherits the app font, follows); then
    broadcasts ``theme_changed`` — each surface re-runs its ``type_qss`` /
    ``_build_fonts`` and picks up the new size + family. The whole fan-out is
    wrapped in ``theme_swap_guard`` so it lands as a single repaint. Reads the
    settings live, so a caller persists ``settings.font_family`` / ``font_scale``
    first, then calls this."""
    from PySide6.QtGui import QFont
    from PySide6.QtWidgets import QApplication

    from jellytoast import design_tokens as _dt
    from jellytoast.player_state import PlayerBus
    from jellytoast.settings import get_settings

    app = QApplication.instance()
    if app is None:
        return
    fam = (get_settings().font_family or "").strip()
    with theme_swap_guard():
        # Rebuild the size tokens + rebind them across modules BEFORE the emit,
        # so the first theme_changed slot re-stamps from the fresh sizes.
        _dt.refresh_fonts()
        _propagate_font_constants()
        # app.setFont MUST also precede the emit — delegates rebuild their bare
        # QFont() during the fan-out and must resolve against the new app font.
        app.setFont(QFont(fam) if fam else (_BOOT_DEFAULT_FONT or QFont()))
        PlayerBus.get().theme_changed.emit()


def _build_global_style() -> str:
    # Radii flow through the rad()-resolved tokens so the "Square corners"
    # setting zeros every corner here in lockstep with the rest of the UI.
    # rad(N) handles the few non-token literals (a 3px checkbox tick, 1px
    # slider groove). The circular slider HANDLE is left at a fixed radius so
    # it stays a round dot when corners are squared.
    from jellytoast.design_tokens import RADIUS_LG, RADIUS_MD, RADIUS_SM, rad

    ar, ag, ab = _accent_rgb_tuple()
    # Regenerate the check-mark PNG for the current accent (lazy +
    # cached per color). Embedding the path into the QSS string here
    # means the next stamp picks up the new path automatically.
    check_url = check_url_for_accent()
    font_stack = _ui_font_family_stack()
    # NB hover tooltips are drawn by our custom popup (jellytoast/custom_tooltip),
    # NOT Qt's QTipLabel — the QToolTip QSS rule below is a defensive fallback
    # for any stray native tooltip and stays `background: transparent`, so the
    # global style doesn't derive a tooltip fill here.
    return f"""
* {{
    color: {TEXT};
    font-family: {font_stack};
}}
QMainWindow, QDialog, QWidget {{
    background: {BG};
}}
QCheckBox {{
    color: {TEXT};
    spacing: 8px;
    background: transparent;
}}
QCheckBox::indicator {{
    width: 16px;
    height: 16px;
    border: 1px solid {BORDER};
    border-radius: {rad(3)}px;
    background: {ink_alpha(0.04)};
}}
QCheckBox::indicator:hover {{
    border-color: {ink_alpha(0.30)};
}}
QCheckBox::indicator:checked {{
    background: rgba({ar},{ag},{ab},0.15);
    border: 1px solid rgba({ar},{ag},{ab},0.45);
    image: url({check_url});
}}
QCheckBox::indicator:checked:hover {{
    background: rgba({ar},{ag},{ab},0.28);
    border-color: rgba({ar},{ag},{ab},0.65);
}}
QCheckBox::indicator:disabled {{
    border-color: {ink_alpha(0.10)};
    background: {ink_alpha(0.02)};
}}
QScrollArea {{ background: transparent; border: none; }}
QScrollBar:vertical {{
    background: {ink_alpha(0.03)}; width: 8px; border-radius: {RADIUS_SM}px;
    margin: 2px;
}}
QScrollBar::handle:vertical {{
    background: rgba({ar},{ag},{ab},0.4); border-radius: {RADIUS_SM}px; min-height: 24px;
}}
QScrollBar::handle:vertical:hover {{ background: {ACCENT}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar:horizontal {{ height: 8px; background: transparent; }}
QScrollBar::handle:horizontal {{
    background: rgba({ar},{ag},{ab},0.4); border-radius: {RADIUS_SM}px; min-width: 24px;
}}
QLineEdit {{
    background: {ink_alpha(0.05)};
    border: 1px solid {BORDER};
    border-radius: {RADIUS_LG}px;
    padding: 8px 12px;
    color: {TEXT};
    selection-background-color: {ACCENT_DEEP};
}}
QLineEdit:focus {{ border-color: {ACCENT}; background: {ink_alpha(0.07)}; }}
QPushButton {{
    background: {ink_alpha(0.05)};
    color: {TEXT};
    border: 1px solid {BORDER};
    border-radius: {RADIUS_LG}px;
    padding: 8px 14px;
}}
QPushButton:hover {{ background: rgba({ar},{ag},{ab},0.15); border-color: {BORDER_ACCENT}; }}
QPushButton:pressed {{ background: rgba({ar},{ag},{ab},0.3); }}
QPushButton#accent {{
    background: {ACCENT_DEEP}; border: 1px solid {ACCENT}; color: white;
}}
QPushButton#accent:hover {{ background: {ACCENT}; }}
QPushButton#ghost {{
    background: transparent; border: none;
}}
QPushButton#ghost:hover {{ background: {ink_alpha(0.06)}; }}
QComboBox {{
    background: {ink_alpha(0.05)};
    border: 1px solid {BORDER};
    border-radius: {RADIUS_LG}px;
    padding: 6px 12px;
    min-height: 22px;
}}
QComboBox::drop-down {{ border: none; width: 20px; }}
QComboBox QAbstractItemView {{
    background: {POPUP_OPAQUE_FILL};
    border: none;
    border-radius: {RADIUS_MD}px;
    selection-background-color: rgba({ar},{ag},{ab},0.25);
    padding: 4px;
}}
QListWidget {{
    background: transparent;
    border: 1px solid {BORDER};
    border-radius: {RADIUS_LG}px;
    outline-style: none;
}}
QListWidget::item {{
    padding: 8px 10px; border-radius: {RADIUS_MD}px; margin: 1px 2px;
}}
QListWidget::item:selected {{ background: rgba({ar},{ag},{ab},0.18); }}
QListWidget::item:hover {{ background: {ink_alpha(0.04)}; }}
QTabWidget::pane {{ border: none; background: transparent; }}
QTabBar::tab {{
    background: transparent;
    color: {TEXT_DIM};
    padding: 8px 18px;
    border: none;
    border-bottom: 2px solid transparent;
    font-weight: 500;
}}
QTabBar::tab:hover {{ color: {TEXT}; }}
QTabBar::tab:selected {{
    color: {ACCENT}; border-bottom: 2px solid {ACCENT};
}}
QSlider::groove:horizontal {{
    height: 3px; background: {ink_alpha(0.12)}; border-radius: {rad(1)}px;
}}
QSlider::sub-page:horizontal {{ background: {ACCENT}; border-radius: {rad(1)}px; }}
QSlider::handle:horizontal {{
    width: 12px; height: 12px; margin: -5px 0;
    background: {TEXT}; border-radius: 6px;
}}
QSlider::handle:horizontal:hover {{ background: {ACCENT}; }}
QMenu {{
    background: {POPUP_OPAQUE_FILL};
    border: none;
    border-radius: {RADIUS_LG}px;
    padding: 4px;
}}
QMenu::item {{
    padding: 8px 24px 8px 14px; border-radius: {RADIUS_SM}px;
}}
QMenu::item:selected {{ background: rgba({ar},{ag},{ab},0.2); }}
QMenu::separator {{
    height: 1px; background: {BORDER}; margin: 4px 8px;
}}
QToolTip {{
    /* Hover tooltips are drawn by jellytoast/custom_tooltip (a top-level
       translucent widget that Source-paints popup_paint_qcolor and rides
       KWin blur). This rule only styles any stray native QTipLabel that
       slips past the filter — keep it transparent + minimal so such a
       fallback still reads as a plain pill. */
    background: transparent; color: {TEXT};
    border: none; padding: 4px 8px; border-radius: {RADIUS_MD}px;
}}
"""


# Initial value — rebuilt by refresh_theme() when the accent changes.
GLOBAL_STYLE = _build_global_style()


def refresh_theme() -> str:
    """Re-read the active theme (after a settings.accent_color or
    settings.theme_mode change) and update every module-level theme
    constant in place. Rebuilds and returns the new GLOBAL_STYLE so
    the caller can push it onto the QApplication. Pair with
    ``icons.refresh_theme()`` (to refresh ICON_ACCENT) and
    ``PlayerBus.theme_changed.emit()`` (to notify subscribers).

    Module-level constants stay the same object identities — we mutate
    the names in place via ``globals()`` so any caller that did
    ``from jellytoast.ui_helpers import ACCENT`` keeps a STALE reference,
    but anyone re-importing or reading ``ui_helpers.ACCENT`` directly
    sees the new value. Subscribers to theme_changed should re-read
    the constant they need on the signal, not cache it in their own
    instance state from __init__.
    """
    global _THEME
    global ACCENT, ACCENT_DEEP, BORDER_ACCENT
    global BG, BG_PANEL, BG_CARD
    global TEXT, TEXT_DIM, TEXT_FAINT, IDLE_TEXT, ERROR_FG, WARN_FG
    global BORDER
    global WASH_HOVER, WASH_PRESSED, HOVER_SUBTLE, HOVER_LIST_ROW
    global SELECTED_ROW, PRESSED_WHITE
    global SURFACE_INPUT, SURFACE_INPUT_FOCUS, DISABLED_FG
    global SLIDER_GROOVE
    global OVERLAY_DARK, OVERLAY_DARK_HOVER, POPUP_OPAQUE_FILL
    global BODY_COLOR, MINI_BODY_COLOR, DIALOG_BODY_COLOR
    global GLOBAL_STYLE
    _THEME = get_active_theme()
    ACCENT = _THEME.accent
    ACCENT_DEEP = _THEME.accent_deep
    BORDER_ACCENT = _THEME.border_accent
    BG = _THEME.bg
    BG_PANEL = _THEME.bg_panel
    BG_CARD = _THEME.bg_card
    TEXT = _THEME.text
    TEXT_DIM = _THEME.text_dim
    TEXT_FAINT = _THEME.text_faint
    IDLE_TEXT = _THEME.idle_text
    ERROR_FG = _THEME.error_fg
    WARN_FG = _THEME.warn_fg
    BORDER = _THEME.border
    WASH_HOVER = _THEME.wash_hover
    WASH_PRESSED = _THEME.wash_pressed
    HOVER_SUBTLE = _THEME.hover_subtle
    HOVER_LIST_ROW = _THEME.hover_list_row
    SELECTED_ROW = _THEME.selected_row
    PRESSED_WHITE = _THEME.pressed_white
    SURFACE_INPUT = _THEME.surface_input
    SURFACE_INPUT_FOCUS = _THEME.surface_input_focus
    DISABLED_FG = _THEME.disabled_fg
    SLIDER_GROOVE = _THEME.slider_groove
    OVERLAY_DARK = _THEME.overlay_dark
    OVERLAY_DARK_HOVER = _THEME.overlay_dark_hover
    POPUP_OPAQUE_FILL = _popup_fill_opaque_on_macos(_THEME.popup_opaque_fill)
    BODY_COLOR = _THEME.body_color
    MINI_BODY_COLOR = _THEME.mini_body_color
    DIALOG_BODY_COLOR = _THEME.dialog_body_color
    # Re-overlay any user color-token overrides on top of the freshly-
    # read theme defaults. Without this, switching theme mode (or
    # picking a new accent preset) would wipe overrides the user set
    # via Settings → Colors for unrelated tokens (e.g. they overrode
    # WASH_HOVER, then picked a green accent — without this re-overlay,
    # WASH_HOVER snaps back to the default). The accent picker
    # explicitly clears the ACCENT override before calling us so the
    # picker's pick wins for that one token.
    try:
        from jellytoast import color_tokens as _ct

        _ct.load_persisted_overrides()
    except Exception:
        # color_tokens may not be importable in odd boot orders; the
        # original theme defaults remain in place.
        pass
    GLOBAL_STYLE = _build_global_style()
    _propagate_theme_constants()
    apply_app_palette()
    return GLOBAL_STYLE


# Token names mirrored into other modules' namespaces — every constant
# a surface might have imported `from jellytoast.ui_helpers import …` and
# baked into a stylesheet at construction.
_PROPAGATED_TOKENS = (
    "ACCENT", "ACCENT_DEEP", "BORDER_ACCENT",
    "BG", "BG_PANEL", "BG_CARD",
    "TEXT", "TEXT_DIM", "TEXT_FAINT", "IDLE_TEXT", "ERROR_FG", "WARN_FG",
    "BORDER",
    "WASH_HOVER", "WASH_PRESSED", "HOVER_SUBTLE", "HOVER_LIST_ROW",
    "SELECTED_ROW", "PRESSED_WHITE",
    "SURFACE_INPUT", "SURFACE_INPUT_FOCUS", "DISABLED_FG",
    "SLIDER_GROOVE",
    "OVERLAY_DARK", "OVERLAY_DARK_HOVER", "POPUP_OPAQUE_FILL",
    "BODY_COLOR", "MINI_BODY_COLOR", "DIALOG_BODY_COLOR",
    "GLOBAL_STYLE",
)


def _propagate_theme_constants() -> None:
    """Rebind the theme-token constants in every ``jellytoast.*`` module
    that imported them by value.

    The documented contract is "re-read ``ui_helpers.X`` on
    theme_changed", but in practice many surfaces did
    ``from jellytoast.ui_helpers import TEXT`` and bake it into a
    stylesheet at construction. A light↔dark switch changes every
    token, so those stale module-level copies have to be refreshed
    too — otherwise a surface's theme_changed re-stamp rebuilds its
    QSS from the *previous* palette. Centralising it here means each
    surface's handler only has to re-run its own styling; it doesn't
    also have to re-import constants."""
    import sys

    src = sys.modules[__name__]
    values = {n: getattr(src, n) for n in _PROPAGATED_TOKENS}
    for mod_name, mod in list(sys.modules.items()):
        if mod is None or mod is src or not mod_name.startswith("jellytoast."):
            continue
        for name, value in values.items():
            if hasattr(mod, name):
                setattr(mod, name, value)


# Font-scale tokens are frozen dataclasses in design_tokens, baked from
# FONT_SCALE at import; surfaces did `from design_tokens import TYPE_BODY,
# BTN_PRIMARY, …` and hold those objects by value. A live font-scale change
# rebuilds them (design_tokens.refresh_fonts), so — exactly like the color
# tokens above — the by-value copies across every module must be rebound or a
# surface's theme_changed re-stamp would read the OLD size.
_PROPAGATED_FONT_TOKENS = (
    "TYPE_DISPLAY", "TYPE_TITLE", "TYPE_HEADING", "TYPE_SUBHEAD",
    "TYPE_BODY", "TYPE_CAPTION", "TYPE_TINY", "TYPE_MICRO", "TYPE",
    "BTN_PRIMARY", "BTN_SECONDARY", "BTN_GHOST", "BTN_ICON", "BTN_DESTRUCTIVE",
    "BUTTON",
)


def _propagate_font_constants() -> None:
    """Rebind the rebuilt ``design_tokens`` typography/button tokens into every
    ``jellytoast.*`` module that imported them by value — the font-scale analog
    of :func:`_propagate_theme_constants`. Call after
    ``design_tokens.refresh_fonts()`` and before emitting ``theme_changed``."""
    import sys

    from jellytoast import design_tokens as _dt

    values = {n: getattr(_dt, n) for n in _PROPAGATED_FONT_TOKENS}
    for mod_name, mod in list(sys.modules.items()):
        if mod is None or mod is _dt or not mod_name.startswith("jellytoast."):
            continue
        for name, value in values.items():
            if hasattr(mod, name):
                setattr(mod, name, value)


@contextlib.contextmanager
def theme_swap_guard():
    """Wrap a live theme/accent swap so it doesn't read as a freeze.

    The swap fan-out (``refresh_theme`` + the ~33 ``theme_changed`` slots, each
    re-stamping QSS) is all-synchronous on the GUI thread — the event loop
    can't paint until it returns. This (a) shows a busy cursor (the honest
    signal, since a spinner can't animate while the loop is blocked) and (b)
    suspends repaints on the visible top-levels so the many intermediate
    ``setStyleSheet`` / re-polish calls collapse into ONE repaint at the end
    instead of flickering through half-restyled states. Best-effort: always
    restores the cursor + updates, even if the swap raises."""
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    suspended = []
    if app is not None:
        app.setOverrideCursor(Qt.CursorShape.BusyCursor)
        for w in app.topLevelWidgets():
            if w.isVisible() and w.updatesEnabled():
                w.setUpdatesEnabled(False)
                suspended.append(w)
    try:
        yield
    finally:
        for w in suspended:
            try:
                w.setUpdatesEnabled(True)
            except RuntimeError:
                pass  # a top-level was deleted mid-swap (e.g. dialog rebuild)
        if app is not None:
            app.restoreOverrideCursor()


def apply_app_palette() -> None:
    """Push a QPalette derived from the active theme onto the
    QApplication.

    GLOBAL_STYLE's QSS only reaches the widget tree it's set on (the
    main window). Separate top-levels — the Settings / cast dialogs,
    QMenu / QToolTip popups — don't inherit it, so any text Qt paints
    from the *palette* rather than from an explicit QSS ``color:`` rule
    falls back to the desktop palette. On a dark desktop that's white
    text, which is invisible on a light jellytoast theme (the dark
    themes never exposed this — the desktop palette happened to match).

    Most backgrounds stay with QSS / per-widget paint so window
    translucency isn't disturbed. The tooltip text roles
    (``ToolTipText``) are pushed so any stray native tooltip still
    reads in the theme ink; hover tooltips themselves are drawn by our
    custom popup (jellytoast/custom_tooltip), which carries its own colour.

    Safe to call before the QApplication exists (no-op)."""
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is None:
        return
    ink = QColor(TEXT)
    pal = app.palette()
    for role in (
        QPalette.ColorRole.WindowText,
        QPalette.ColorRole.Text,
        QPalette.ColorRole.ButtonText,
        QPalette.ColorRole.ToolTipText,
    ):
        pal.setColor(role, ink)
    # Tooltip backdrop — TRANSPARENT. Hover tooltips are our custom popup now,
    # not QTipLabel, so ToolTipBase only governs any stray native tooltip; keep
    # it transparent so QStyle never paints an opaque RECTANGLE behind the pill
    # (the dark-block-at-the-corners bug on dialog-owned / separate-top-level
    # tooltips that the QSS `QToolTip{background:transparent}` rule didn't reach).
    pal.setColor(QPalette.ColorRole.ToolTipBase, QColor(0, 0, 0, 0))
    pal.setColor(QPalette.ColorRole.Highlight, QColor(ACCENT))
    pal.setColor(QPalette.ColorRole.HighlightedText, QColor("#ffffff"))
    # Disabled foreground — the ink at low alpha.
    disabled = QColor(ink)
    disabled.setAlpha(110)
    for role in (
        QPalette.ColorRole.WindowText,
        QPalette.ColorRole.Text,
        QPalette.ColorRole.ButtonText,
    ):
        pal.setColor(QPalette.ColorGroup.Disabled, role, disabled)
    app.setPalette(pal)


# ── KDE Plasma window-manager hints ─────────────────────────────────────────

_XPROP_OK: Optional[bool] = None


def skip_taskbar_x11(widget: QWidget):
    """
    Tell EWMH-aware window managers (KWin/Mutter/i3/etc.) to keep `widget` out
    of the taskbar and pager. Uses xprop to set _NET_WM_STATE atoms.
    Silently no-ops if xprop is missing or we're on native Wayland (the
    xprop subprocess can't address Wayland surfaces; mini_player.py uses
    the Qt.Tool window flag on Wayland instead).
    """
    global _XPROP_OK
    # Off-X11 (Wayland, Windows, macOS): xprop can't address the surface;
    # bail before subprocessing.
    from jellytoast.platform_compat import is_x11

    if not is_x11():
        return
    if _XPROP_OK is False:
        return
    if _XPROP_OK is None:
        _XPROP_OK = shutil.which("xprop") is not None
        if not _XPROP_OK:
            return

    try:
        wid = int(widget.winId())
    except Exception:
        return
    if wid <= 0:
        return

    def _run():
        subprocess.run(
            [
                "xprop",
                "-id",
                str(wid),
                "-f",
                "_NET_WM_STATE",
                "32a",
                "-set",
                "_NET_WM_STATE",
                "_NET_WM_STATE_SKIP_TASKBAR,_NET_WM_STATE_SKIP_PAGER,_NET_WM_STATE_ABOVE",
            ],
            check=False,
            timeout=2,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    # One-shot blocking subprocess — exactly what the shared pool is for
    # (this was the one raw threading.Thread with no structural excuse).
    from jellytoast.async_io import run_async

    run_async(_run, on_error=lambda _e: None)


# ── HiDPI helpers ───────────────────────────────────────────────────────────


def screen_dpr(widget: "Optional[QWidget]" = None) -> float:
    """Effective device-pixel ratio for ``widget``'s screen, or the
    primary screen if no widget is given.

    ``QWidget.devicePixelRatioF()`` is the right answer on multi-monitor
    setups (it tracks the screen the widget's window is currently on)
    once the widget has been mapped; it returns 0.0 before that, which
    we fall through to the primary-screen DPR. Use this everywhere
    cover artwork or any other pixmap is scaled — passing logical
    sizes to Qt at fractional / 2× / 3× scales without DPR-multiplying
    the request size produces soft pixmaps.
    """
    if widget is not None:
        try:
            dpr = widget.devicePixelRatioF()
        except Exception:
            dpr = 0.0
        if dpr >= 1.0:
            return dpr
    s = QGuiApplication.primaryScreen()
    return s.devicePixelRatio() if s is not None else 1.0


# Fixed bucket set for cache-key DPR quantization. Wayland fractional
# scaling reports values like 1.5999999 that drift across launches —
# using the raw DPR in a cache key fragments the disk cache so a
# "loaded" library re-hits the network on every reload. Use the closest
# bucket below for fetch-size + cache-key calculations; keep the raw
# screen_dpr() for the actual scale-pixmap-for-dpr tag (so rendering
# stays sharp).
_DPR_BUCKETS = (1.0, 1.5, 2.0, 3.0)


def dpr_bucket(dpr: float) -> float:
    """Snap ``dpr`` to the nearest entry in ``_DPR_BUCKETS``. Use for
    cache-key + fetch-size math; pass the raw ``screen_dpr()`` to the
    actual pixmap scaling so DPR drift across launches doesn't
    fragment the cover cache."""
    if dpr >= _DPR_BUCKETS[-1]:
        return _DPR_BUCKETS[-1]
    return min(_DPR_BUCKETS, key=lambda b: abs(b - dpr))


def scale_pixmap_for_dpr(
    pix: "QPixmap",
    logical_size: int,
    dpr: "Optional[float]" = None,
) -> "QPixmap":
    """Return a DPR-tagged square pixmap sized for ``logical_size``
    logical points. Scales ``pix`` to ``round(logical_size * dpr)``
    physical pixels via ``KeepAspectRatioByExpanding`` (so square
    targets fill cleanly without letterboxing), centre-crops if one
    axis overshoots, and calls ``setDevicePixelRatio(dpr)`` so Qt
    paints at ``logical_size × logical_size`` logical points using
    the full-resolution texture.

    On a 1.0× display this is a single scale + a no-op DPR tag; on
    1.25× / 1.5× / 2× / 3× displays it's the only thing keeping
    album art from looking soft after Qt's paint-time downscale
    from logical-sized bytes to the physical surface.
    """
    if pix is None or pix.isNull():
        return pix
    if dpr is None:
        dpr = screen_dpr()
    target = max(logical_size, int(round(logical_size * dpr)))
    from PySide6.QtCore import QSize

    scaled = pix.scaled(
        target,
        target,
        Qt.AspectRatioMode.KeepAspectRatioByExpanding,
        Qt.TransformationMode.SmoothTransformation,
    )
    # KeepAspectRatioByExpanding always meets-or-exceeds the requested
    # size on both axes for non-square source aspect ratios; centre-crop
    # the overflow so the result is exactly target × target.
    if scaled.size() != QSize(target, target):
        x = max(0, (scaled.width() - target) // 2)
        y = max(0, (scaled.height() - target) // 2)
        scaled = scaled.copy(x, y, target, target)
    scaled.setDevicePixelRatio(dpr)
    return scaled


# ── Async image loader ──────────────────────────────────────────────────────

# LRU bound on the decoded-pixmap cache. QPixmaps are GPU-side textures
# (~30-60kB each at typical 200x200 cover sizes plus rounded-corner
# variants), so an unbounded dict balloons VRAM on big libraries.
# 256 is generous enough that a typical browse never repeats a fetch
# but caps growth to single-digit megabytes.
_IMAGE_CACHE_MAX = 256
_image_cache: "OrderedDict[str, QPixmap]" = OrderedDict()


def clear_image_caches():
    """Drop every pixmap + raw image held in memory. Called on
    sign-out / server-switch so artwork resolved against the
    previous user / server doesn't bleed into the next session —
    cache keys collide on item id for short-id providers like
    Subsonic, where the new server may have entirely different
    art behind the same id."""
    global _raw_cache_bytes

    _image_cache.clear()
    _raw_image_cache.clear()
    _raw_cache_bytes = 0


# In-flight QNetworkReply objects keyed to the load context the slot
# needs. Qt deletes reply objects whose Python refs are dropped, so we
# must hold them across the async hop — the slot pops the entry and
# calls `reply.deleteLater()` once decoding is done.
_pending_replies: dict = {}

# Per-cache-key list of waiting (callback, on_error) tuples. A second
# caller for an in-flight key piggybacks on the existing fetch instead
# of opening a duplicate network connection. Crucial when the
# now-playing bar, mini player, and np page all wake up on the same
# track-change and want the same album art — pre-dedup, that was three
# parallel GETs for one image.
_inflight_subscribers: dict = {}

# Concurrency gate for grid-style cover loads. QNAM enforces ~6
# connections per host, but it ACCEPTS unlimited requests and queues the
# overflow internally WITH each request's transfer-timeout timer already
# running. So on a big/slow library a viewport burst (50+ tiles) hands
# QNAM far more than it can dispatch, and the tail of that queue trips its
# transfer timeout before a socket ever opens — then gets retried and
# abandoned. That is the "loads a few covers then stops" report from the
# first external user (a ~5,200-album Navidrome).
#
# Fix: cap how many NORMAL/LOW loads we hand QNAM at once, so nothing sits
# in its internal queue with a live timer; the overflow waits HERE (no
# timer) and is dispatched as replies complete. HIGH priority (now-playing
# bar cover, hover prewarm — rare and user-facing) bypasses the gate so it
# never waits behind a grid sweep. 5 leaves one of QNAM's ~6 sockets free
# for a high-priority burst. Two queues keep NORMAL (visible tiles) ahead of
# LOW (off-screen prefetch) when a slot frees.
#
# The queues are ORDERED DICTS keyed by cache_key, not FIFO lists — the
# 2026-07 "art stops loading mid-scroll" audit found session-length
# starvation: a fast A→K scroll enqueued every intermediate row, and the
# rows actually ON SCREEN coalesced onto entries parked at the BACK of the
# line, draining 5-at-a-time behind hundreds of stale rows. Key-addressable
# entries let a repeat request for a deferred key PROMOTE it to the front
# (see _promote_deferred_key) — the viewport always wins. Front of each
# dict = next to fire.
_GATED_MAX_INFLIGHT = 5
_gated_in_flight = 0
_deferred_normal: "OrderedDict[str, tuple]" = OrderedDict()
_deferred_low: "OrderedDict[str, tuple]" = OrderedDict()
# LOW backlog cap. The grid's prefetch window re-anchors on every scroll,
# so an unbounded low queue can hold hundreds of rows the user scrolled
# away from. Overflow drops the OLDEST (frontmost = longest-parked) entry
# SILENTLY — subscribers are discarded without callbacks; the visible
# pass's has_cover() re-arm reloads any dropped row the moment it matters
# (low priority is only used by the grid prefetch, which tolerates this).
_DEFERRED_LOW_MAX = 64

# Adaptive "this server is slow at resizing" latch (#cover-stall). We ask the
# server to resize covers (getCoverArt `size=` / Jellyfin maxWidth) because a
# sized thumbnail is a small download. But a big library on an underpowered
# server (Skope's ~5,200-album Navidrome in Docker) can take longer than the
# transfer timeout to GENERATE each thumbnail — every cover then times out and
# never loads ("loads some then stops"). VERIFIED locally against a
# CPU-throttled 5,200-album Navidrome: sized requests hit the 30s timeout.
# So once a couple of sized requests time out, we stop asking THIS session's
# server to resize and fetch the ORIGINAL instead (served straight off disk, no
# CPU) — we already re-scale locally (_derive_pixmap), so quality is identical.
# A healthy server never trips this (timeouts don't happen), so it keeps the
# bandwidth-efficient sized path. Resets on relaunch; worst case if wrongly
# tripped is heavier downloads, never breakage.
_RESIZE_TIMEOUT_TRIP = 2
_resize_timeouts = 0
_prefer_original_covers = False

# L2 "raw decoded source" cache. Keyed by the SEMANTIC key (the part of
# the caller's `key` before the first `|`, typically an item id /
# AlbumId), stores the pre-scale source QImage from the network. Lets
# a later caller asking for a different size + radius of the SAME
# image (e.g. now-playing bar wants 256 of an album the album grid
# tile already loaded at 360) derive its target locally — scale +
# round are sub-millisecond — instead of paying for another network
# round-trip.
#
# Bounded by BYTES (with a loose entry cap as a secondary guard), because
# entry-counting silently blows up: a decoded source is ~1.2 MB at the
# usual 540 px, but the slow-server latch (_prefer_original_covers)
# fetches originals, and 32 entries of a 3000×3000 master is ~1.1 GB.
# _RAW_MAX_DIM caps what we keep: the largest target any surface asks for
# is the mini player's 960 px (320 × 3 DPR), so 1280 leaves headroom
# while cutting an oversized master ~5×. Raising a consumer past
# _RAW_MAX_DIM is safe but makes it miss this tier (it refetches rather
# than deriving) — raise the constant with it.
_RAW_MAX_DIM = 1280
_RAW_CACHE_BUDGET_BYTES = 48 * 1024 * 1024
_RAW_IMAGE_CACHE_MAX = 64
_raw_cache_bytes = 0
_raw_image_cache: "OrderedDict[str, QImage]" = OrderedDict()

# A cached raw source is reused to derive a target even when it's
# slightly smaller than the request — deriving means a sub-ms upscale,
# the alternative is a 200ms-2s network round-trip for an image we
# already have. The bound keeps quality honest: a raw at >=75% of the
# target on both axes upscales imperceptibly for cover art (e.g. a
# 324px raw serving a 360px tile), while a genuinely undersized cache
# entry (a 360px raw vs the mini player's 800px request) still falls
# through to the network. Without this, any cross-session size drift
# — a cover cached at one DPR, requested at another — silently
# refetched every previously-loaded image on reload.
_RAW_DERIVE_MIN_RATIO = 0.75


def _raw_covers_target(src_w: int, src_h: int, target_w: int, target_h: int) -> bool:
    """True if a raw source sized ``src_w×src_h`` is close enough to
    ``target_w×target_h`` to derive from instead of refetching — at
    or above :data:`_RAW_DERIVE_MIN_RATIO` on both axes."""
    return src_w >= target_w * _RAW_DERIVE_MIN_RATIO and src_h >= target_h * _RAW_DERIVE_MIN_RATIO


def art_stem(item_id: str, tag: str = "") -> str:
    """Cache-key stem for a cover: the id plus the server's art VERSION
    token when we have one.

    Covers are cached by item id, which never changes when the artwork
    behind it does — so re-tagging an album (or fixing its cover in
    Navidrome / Jellyfin) left the OLD art on screen indefinitely, with
    no way back short of wiping the cache. Both providers hand us a
    version token in the same place and we simply weren't using it:
      * Jellyfin ``ImageTags.Primary`` is a content hash of the image;
      * Navidrome/Subsonic ``coverArt`` is ``al-<albumId>_<hash>``, whose
        suffix likewise changes with the art.
    Both are adapted into ``ImageTags.Primary``, so folding that into the
    stem makes new art land on a fresh cache slot automatically — no
    revalidation request, no TTL, no manual refresh.

    Keeps the id in front so entries stay identifiable in logs and two
    items that happen to share identical artwork keep separate slots
    (matching today's per-item behaviour). The URL is still built from
    the item id — only cache IDENTITY carries the token."""
    tag = (tag or "").strip()
    return f"{item_id}@{tag}" if tag else item_id


def np_art_stem(np) -> str:
    """``art_stem`` for a NowPlaying — its cover identity (album art for
    audio, else the item) plus the server's art version token. The
    player surfaces all key their cover cache off this."""
    image_id = getattr(np, "image_id", "") or getattr(np, "item_id", "")
    return art_stem(image_id, getattr(np, "art_tag", ""))


def _semantic_key(key: str) -> str:
    """Extract the shared identity portion of a load_image_async key —
    everything before the first `|` separator. Callers follow the
    convention `{semantic_id}|{consumer_tag}` (e.g. `albumX|npbar`,
    `albumX|albumtile`), so all surfaces that load the same image
    end up with the same semantic key even though their cache_keys
    differ on size and rounding."""
    return key.split("|", 1)[0] if "|" in key else key


def _derive_pixmap(src: "QImage", target_w: int, target_h: int, radius: int) -> QPixmap:
    """Scale + round a decoded source QImage to a target's exact
    pixmap. Used both at network-finish time (just-decoded src) and
    on L2 cache hits (src from a previous consumer of the same
    semantic key)."""
    scaled = src.scaled(
        target_w,
        target_h,
        Qt.AspectRatioMode.KeepAspectRatioByExpanding,
        Qt.TransformationMode.SmoothTransformation,
    )
    pix = QPixmap.fromImage(scaled)
    if radius > 0:
        pix = _round_corners(pix, radius)
    return pix


def _img_bytes(img: "QImage") -> int:
    """Decoded footprint of a QImage — width × height × 4 (ARGB32)."""
    return img.width() * img.height() * 4


def _store_raw(sem_key: str, src: "QImage"):
    """Cache the decoded source image under its semantic key, but only
    if it's at least as big as anything already there — a later caller
    asking for a smaller variant can downscale, but we never upscale a
    small cached source for a larger requester.

    Bounded in BYTES, not entries. A plain count cap is fine while
    sources are the usual ~540 px thumbnails (~1.2 MB each), but the
    slow-server latch fetches FULL-SIZE originals, and 32 entries of a
    3000×3000 master is ~1.1 GB resident. (Same failure shape as the
    grid's cover LRU — audit #234 finding 9 — which this cache never
    got converted for.) Oversized sources are also capped at
    ``_RAW_MAX_DIM``; the pooled decode normally pre-caps, this is the
    guard for raws read back from an older on-disk cache."""
    if not sem_key or src is None or src.isNull():
        return
    if max(src.width(), src.height()) > _RAW_MAX_DIM:
        src = src.scaled(
            _RAW_MAX_DIM,
            _RAW_MAX_DIM,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
    global _raw_cache_bytes
    existing = _raw_image_cache.get(sem_key)
    if existing is not None and (
        existing.width() >= src.width() and existing.height() >= src.height()
    ):
        _raw_image_cache.move_to_end(sem_key)
        return
    if existing is not None:
        _raw_cache_bytes -= _img_bytes(existing)
    _raw_image_cache[sem_key] = src
    _raw_cache_bytes += _img_bytes(src)
    _raw_image_cache.move_to_end(sem_key)
    # Keep at least one entry resident even if a single source somehow
    # exceeds the whole budget — evicting what we just stored would make
    # the L2 tier useless for that image.
    while (
        _raw_cache_bytes > _RAW_CACHE_BUDGET_BYTES
        or len(_raw_image_cache) > _RAW_IMAGE_CACHE_MAX
    ) and len(_raw_image_cache) > 1:
        _, evicted = _raw_image_cache.popitem(last=False)
        _raw_cache_bytes -= _img_bytes(evicted)


def load_image_async(
    key: str,
    url: str,
    target_w: int,
    target_h: int,
    callback: Callable[[QPixmap], None],
    rounded_radius: int = 0,
    on_error: Optional[Callable[[], None]] = None,
    priority: str = "normal",
):
    """
    Fetch + scale image asynchronously via Qt's network stack, decoding
    on the GUI thread once the reply lands. No raw threads, no `requests`,
    no cross-thread QObject GC pinning — QNAM owns connection pooling
    and per-host parallelism, and the entire pipeline runs on the Qt
    event loop.

    Three-tier lookup: in-memory LRU → disk cache → network. The disk
    tier is keyed by ``cache_key`` (not URL), so Subsonic-style
    auth-rotated URLs still hit a stable slot.

    ``on_error``: optional. If provided, invoked when the network
    fetch fails or the body fails to decode — and ``callback`` is
    *not* invoked with a placeholder. Lets callers (e.g. LibraryGrid)
    decide whether to retry, show a custom fallback, or let their
    widget's own placeholder show through. If omitted, the caller
    receives the dark placeholder pixmap (legacy behavior) so older
    sites stay correct without changes.

    ``priority``: "high" (user-action requests like the now-playing
    bar cover, hover prewarm), "normal" (visible tiles, queue
    prefetch — the default), or "low" (off-screen prefetches that
    can wait). High/normal never queue behind low; low is gated to
    `_LOW_PRIO_MAX_INFLIGHT` concurrent so a background prefetch
    burst can't choke an in-flight user-action request. Maps to
    QNetworkRequest priority hints for HTTP/2 connections; on
    HTTP/1.1 the gate alone provides the ordering benefit.
    """
    cache_key = f"{key}|{target_w}x{target_h}|r={rounded_radius}"
    cached = _image_cache.get(cache_key)
    if cached is not None:
        _image_cache.move_to_end(cache_key)
        callback(cached)
        return

    # L2 (in-memory): a different consumer (album tile, mini player,
    # np page) may have loaded the same image at a different size —
    # derive ours from their decoded source instead of going to disk
    # or network. This is what makes "click-an-album-whose-tile-is-on-
    # screen" feel instant in the bar: tile cached source at 360px,
    # bar wants 256px, L2 hit takes ~1ms. Only valid when the cached
    # source is at LEAST as big as our target on both axes — upscaling
    # a smaller cached source would look blurry, so we fall through.
    sem_key = _semantic_key(key)
    raw = _raw_image_cache.get(sem_key)
    if raw is not None and _raw_covers_target(raw.width(), raw.height(), target_w, target_h):
        _raw_image_cache.move_to_end(sem_key)
        pix = _derive_pixmap(raw, target_w, target_h, rounded_radius)
        _image_cache[cache_key] = pix
        _image_cache.move_to_end(cache_key)
        while len(_image_cache) > _IMAGE_CACHE_MAX:
            _image_cache.popitem(last=False)
        _disk_image_cache.put(cache_key, pix)
        callback(pix)
        return

    # L2 (on-disk): a previous SESSION may have stashed the raw source
    # under this semantic key. Without this branch the very first
    # cover load of a freshly-launched app always goes to network even
    # when other surfaces (tiles) loaded the image previously, because
    # the per-consumer disk pixmaps are pre-rounded and per-size — they
    # can't be reused as a derivation source. Loading the raw, warming
    # the in-memory L2, then deriving locally is ~30ms (file read +
    # decode + scale) vs 200ms-2s for a Navidrome cold cover request.
    # Both disk reads run POOLED — each file read + QImage decode (and
    # the AV scan it triggers on Windows) on the GUI thread summed to
    # an ~8s blocked main thread during a Windows cold boot
    # (2026-06-12 stall tracebacks). The lookup returns QImages
    # (thread-safe); pixmap conversion happens back here on the GUI
    # thread. A miss falls through to the original offline-gate +
    # network path via the continuation below.
    def _disk_lookup():
        raw = _disk_image_cache.get_raw(sem_key)
        if raw is not None and _raw_covers_target(
            raw.width(), raw.height(), target_w, target_h
        ):
            return ("raw", raw)
        img = _disk_image_cache.get_image(cache_key)
        if img is not None:
            return ("exact", img)
        return None

    def _on_disk_result(result):
        if result is not None:
            kind, img = result
            if kind == "raw":
                _store_raw(sem_key, img)
                pix = _derive_pixmap(img, target_w, target_h, rounded_radius)
                _disk_image_cache.put(cache_key, pix)
            else:
                pix = QPixmap.fromImage(img)
            _image_cache[cache_key] = pix
            _image_cache.move_to_end(cache_key)
            while len(_image_cache) > _IMAGE_CACHE_MAX:
                _image_cache.popitem(last=False)
            callback(pix)
            return
        _after_disk_miss(
            cache_key,
            sem_key,
            url,
            target_w,
            target_h,
            rounded_radius,
            callback,
            on_error,
            priority,
        )

    from jellytoast.async_io import run_async

    run_async(
        _disk_lookup,
        on_result=_on_disk_result,
        on_error=lambda _e: _on_disk_result(None),
    )


def _after_disk_miss(
    cache_key: str,
    sem_key: str,
    url: str,
    target_w: int,
    target_h: int,
    rounded_radius: int,
    callback: Callable[[QPixmap], None],
    on_error: Optional[Callable[[], None]],
    priority: str,
):
    """Continuation of ``load_image_async`` after every local tier
    missed — the offline gate, in-flight coalescing, and the network
    fetch. Split out because the disk tiers resolve on the pool."""
    # Offline gate: every local cache tier has been tried above. Don't
    # let a cover-load wait for a network timeout in offline mode —
    # either let the caller handle it (on_error) or hand back the
    # placeholder.
    from jellytoast import offline as _offline

    if _offline.is_offline_mode():
        if on_error is not None:
            on_error()
            return
        ph = _placeholder_image(target_w, target_h)
        ph_pix = QPixmap.fromImage(ph)
        if rounded_radius > 0:
            ph_pix = _round_corners(ph_pix, rounded_radius)
        callback(ph_pix)
        return

    # Coalesce: another caller may have already kicked off this exact
    # cache_key. Stack our (callback, on_error) onto the existing
    # waiter list and bail — the in-flight reply will fan out to all
    # subscribers when it finishes. If the earlier request is still
    # PARKED in a deferred queue, a normal/high repeat means the user
    # is looking at it NOW — promote it to the front of the line (the
    # anti-starvation half of the 2026-07 art audit).
    waiters = _inflight_subscribers.get(cache_key)
    if waiters is not None:
        waiters.append((callback, on_error))
        if priority != "low":
            _promote_deferred_key(cache_key)
        return
    _inflight_subscribers[cache_key] = [(callback, on_error)]
    _ensure_cover_diag()

    # Concurrency gate: HIGH fires immediately. NORMAL/LOW go through the
    # shared in-flight cap (see _GATED_MAX_INFLIGHT) so a grid sweep can't
    # flood QNAM's internal queue and trip transfer timeouts on requests
    # that never got a socket. A deferred load waits HERE with no live
    # timer and is promoted as replies finish — NORMAL ahead of LOW.
    if priority != "high":
        global _gated_in_flight
        if _gated_in_flight >= _GATED_MAX_INFLIGHT:
            queue = _deferred_low if priority == "low" else _deferred_normal
            # Entry keeps the geometry so promotion can fan the failure
            # out to this load's subscribers if the deferred fire itself
            # blows up (see _promote_next_deferred).
            queue[cache_key] = (
                target_w,
                target_h,
                rounded_radius,
                lambda: _fire_image_request(
                    cache_key,
                    sem_key,
                    url,
                    target_w,
                    target_h,
                    rounded_radius,
                    priority,
                ),
            )
            if queue is _deferred_low:
                while len(_deferred_low) > _DEFERRED_LOW_MAX:
                    # Drop the longest-parked prefetch SILENTLY (no error
                    # fan-out — see _DEFERRED_LOW_MAX). Subscribers must
                    # still be forgotten or the key wedges as "in flight"
                    # and every future request coalesces onto nothing.
                    stale_key, _entry = _deferred_low.popitem(last=False)
                    _inflight_subscribers.pop(stale_key, None)
            return
        _gated_in_flight += 1

    try:
        _fire_image_request(
            cache_key,
            sem_key,
            url,
            target_w,
            target_h,
            rounded_radius,
            priority,
        )
    except Exception:
        # The request failed to even start (QNAM teardown, bad URL) AFTER we
        # registered as the in-flight owner — both halves of that claim must
        # be unwound or the session degrades permanently (audit #234 #7):
        #  * the subscriber entry: left in place, every future load of this
        #    cache_key coalesces onto a reply that doesn't exist, wedging
        #    the key (and its widgets) for the whole session;
        #  * the gate slot (gated priorities only — HIGH never took one):
        #    5 leaks would ratchet the gate shut. And since the finish
        #    handler that normally promotes the next deferred will never
        #    run for THIS load, promote here too — if this was the last
        #    in-flight load, the deferred queue would otherwise strand.
        _fail_inflight(cache_key, target_w, target_h, rounded_radius)
        if priority != "high":
            _gated_in_flight -= 1
            _promote_next_deferred()
        raise


def toggle_favorite_async(item_id: str, new_state: bool, on_rollback=None) -> None:
    """Dispatch a favorite flip to the provider WITH rollback.

    Every heart in the app flips optimistically at its call site (and
    broadcasts ``favorite_toggled`` so the other surfaces repaint) — but
    until #234 finding 8 a failed server write was swallowed, leaving the
    heart silently diverged until the next metadata refetch. Both
    providers now raise on failure; this helper is the one place that
    handles it: restore the shared NowPlaying flag, run the call site's
    own state restore (``on_rollback``), re-broadcast the OLD state so
    every subscribed surface flips back, and toast the active window.
    """
    from jellytoast.async_io import run_async
    from jellytoast.player_state import PlayerBus, get_now_playing
    from jellytoast.providers import get_provider

    def _on_error(_exc) -> None:
        old_state = not new_state
        np = get_now_playing()
        if np.item_id == item_id:
            np.is_favorite = old_state
        if on_rollback is not None:
            try:
                on_rollback()
            except Exception:
                pass
        PlayerBus.get().favorite_toggled.emit(item_id, old_state)
        try:
            from PySide6.QtCore import QCoreApplication
            from PySide6.QtWidgets import QApplication

            from jellytoast.toast import show_toast

            win = QApplication.activeWindow()
            if win is not None:
                show_toast(
                    win,
                    QCoreApplication.translate(
                        "UiHelpers",
                        "Couldn't update favorite — check your connection.",
                    ),
                )
        except Exception:
            pass

    run_async(get_provider().toggle_favorite, item_id, new_state, on_error=_on_error)


def _fail_inflight(cache_key: str, target_w: int, target_h: int, radius: int) -> None:
    """Fan a start-failure out to every subscriber of ``cache_key`` and
    forget the key. on_error subscribers get their callback; legacy
    callers get the placeholder pixmap (never cached), mirroring the
    finish handler's failure fan-out."""
    waiters = _inflight_subscribers.pop(cache_key, [])
    ph_pix = None
    for cb, err in waiters:
        try:
            if err is not None:
                err()
                continue
            if ph_pix is None:
                ph = _placeholder_image(target_w, target_h)
                ph_pix = QPixmap.fromImage(ph)
                if radius > 0:
                    ph_pix = _round_corners(ph_pix, radius)
            cb(ph_pix)
        except Exception:
            pass


def cover_pipeline_stats() -> dict:
    """Live pipeline counters — the JT_COVER_DIAG surface, also handy in
    tests and the stress rigs. Cheap: reads module globals only."""
    return {
        "gate_in_flight": _gated_in_flight,
        "deferred_normal": len(_deferred_normal),
        "deferred_low": len(_deferred_low),
        "inflight_keys": len(_inflight_subscribers),
        "prefer_original_covers": _prefer_original_covers,
        "resize_timeouts": _resize_timeouts,
        "mem_pixmaps": len(_image_cache),
        "mem_raws": len(_raw_image_cache),
        "mem_raw_mb": round(_raw_cache_bytes / 1048576, 1),
    }


_diag_timer = None


def _ensure_cover_diag() -> None:
    """JT_COVER_DIAG=1: log the pipeline counters every 5 s (only while
    something is pending, so an idle app stays quiet). Mirrors the
    JT_BLUR_DIAG pattern — field reports of 'art stopped loading' become
    diagnosable from a log tail instead of a repro session."""
    global _diag_timer
    import os

    if _diag_timer is not None or os.environ.get("JT_COVER_DIAG") != "1":
        return
    from PySide6.QtCore import QTimer

    _diag_timer = QTimer()
    _diag_timer.setInterval(5000)

    def _tick():
        s = cover_pipeline_stats()
        if s["gate_in_flight"] or s["deferred_normal"] or s["deferred_low"]:
            logger.info("cover-diag: %s", s)

    _diag_timer.timeout.connect(_tick)
    _diag_timer.start()


def _promote_deferred_key(cache_key: str) -> None:
    """Move a parked load to the FRONT of the normal queue. Called when a
    normal/high request coalesces onto a still-deferred key: the user is
    looking at that tile right now, so it must not wait behind rows they
    scrolled past — and a low-priority prefetch entry is UPGRADED to
    normal, or the prefetched-then-scrolled-to row starves the same way."""
    entry = _deferred_low.pop(cache_key, None)
    if entry is None:
        if cache_key not in _deferred_normal:
            return  # already in flight (holds a gate slot) — nothing to do
        entry = _deferred_normal.pop(cache_key)
    _deferred_normal[cache_key] = entry
    _deferred_normal.move_to_end(cache_key, last=False)


def _promote_next_deferred() -> None:
    """Claim a gate slot and fire the next deferred load — NORMAL before
    LOW, so a visible tile beats a prefetch. Loops on start-failure: the
    failed load's subscribers get the error fan-out and the next deferred
    is tried, so one bad URL can't strand the rest of the queue."""
    global _gated_in_flight
    while True:
        if _deferred_normal:
            cache_key, entry = _deferred_normal.popitem(last=False)
        elif _deferred_low:
            cache_key, entry = _deferred_low.popitem(last=False)
        else:
            return
        w, h, radius, fire = entry
        _gated_in_flight += 1
        try:
            fire()
            return
        except Exception:
            _gated_in_flight -= 1
            _fail_inflight(cache_key, w, h, radius)


# Query params that ask the server to RESIZE the image before sending it.
# Covers both dialects: Subsonic getCoverArt (`size`) and Jellyfin /Images
# (`maxWidth`/`fillWidth`/…). Stripping them yields the ORIGINAL asset, which
# the server just serves off disk instead of generating on demand.
_RESIZE_QUERY_KEYS = frozenset(
    {"size", "width", "height", "maxwidth", "maxheight", "fillwidth", "fillheight"}
)


def _redact_url(url: str) -> str:
    """Drop auth/token query params so a cover URL is safe to log — Subsonic
    carries `p`/`t`/`s`/`u`, Jellyfin an `api_key`. Keeps host + path + the
    non-secret bits (id, size) which are what we care about when debugging."""
    try:
        from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

        secret = {"p", "t", "s", "u", "api_key", "apikey", "token", "password"}
        parts = urlparse(url)
        kept = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                if k.lower() not in secret]
        return urlunparse(parts._replace(query=urlencode(kept)))
    except Exception:
        return "<url>"


def _strip_resize_params(url: str) -> Optional[str]:
    """Return ``url`` with any server-side-resize query params removed, or
    None if there were none to remove. Used as a fallback when a sized cover
    request fails: some servers are slow to generate thumbnails (a big library
    on first touch can exceed the transfer timeout) or reject the resize param
    outright, while the original asset serves instantly. We re-scale locally
    anyway (_derive_pixmap), so the size hint is an optimisation, not a need —
    dropping it is strictly safer on the failure path. (#cover-stall)"""
    try:
        from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

        parts = urlparse(url)
        kept = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                if k.lower() not in _RESIZE_QUERY_KEYS]
        if len(kept) == len(parse_qsl(parts.query, keep_blank_values=True)):
            return None  # nothing stripped
        return urlunparse(parts._replace(query=urlencode(kept)))
    except Exception:
        return None


def _fire_image_request(
    cache_key: str,
    sem_key: str,
    url: str,
    target_w: int,
    target_h: int,
    rounded_radius: int,
    priority: str,
    resize_fallback_done: bool = False,
):
    """Actually open the QNetworkReply for this load. Split out so the
    low-priority gate can defer-then-fire without duplicating the
    QNetworkRequest setup."""
    # Once this session's server has proven slow at resizing, skip the sized
    # request entirely — go straight to the original (#cover-stall).
    if _prefer_original_covers and not resize_fallback_done:
        stripped = _strip_resize_params(url)
        if stripped is not None:
            url = stripped
            resize_fallback_done = True  # already original — no further fallback
    req = QNetworkRequest(QUrl(url))
    # The app-level gate (_GATED_MAX_INFLIGHT) now means a request only
    # reaches QNAM when a socket is actually free, so this timeout covers
    # the live transfer alone — not in-queue wait. 30s tolerates a cold
    # Navidrome reading + resizing embedded art from a large library on
    # first touch (it caches the thumbnail after), without holding a truly
    # stuck reply forever.
    req.setTransferTimeout(30000)
    if priority == "high":
        req.setPriority(QNetworkRequest.Priority.HighPriority)
    elif priority == "low":
        req.setPriority(QNetworkRequest.Priority.LowPriority)
    reply = get_qnam().get(req)
    _pending_replies[reply] = (
        cache_key,
        sem_key,
        target_w,
        target_h,
        rounded_radius,
        priority,
        url,
        resize_fallback_done,
    )
    reply.finished.connect(lambda r=reply: _on_image_reply_finished(r))


def _decode_and_scale(data: bytes, target_w: int, target_h: int):
    """POOL THREAD: decode the reply body and pre-scale it.

    Returns ``(raw_for_cache, scaled_for_target)`` as QImages — QImage is
    thread-safe, QPixmap is not, so the GUI thread does only the pixmap
    conversion (see ``_deliver_decoded``). Returns None if the body isn't
    a decodable image (a server error page, an unsupported format).

    Both steps used to run on the GUI thread at reply-finish time. That
    is fine for a 540px thumbnail, but the slow-server latch
    (``_prefer_original_covers``) fetches FULL-SIZE originals, and
    decoding + smooth-scaling five concurrent multi-megapixel JPEGs on
    the GUI thread is exactly the jank a user feels while scrolling. The
    disk tier was moved to the pool for this same reason.

    ``raw_for_cache`` is capped at ``_RAW_MAX_DIM``: nothing in the app
    requests a target bigger than that, so keeping a 3000px master would
    cost ~36 MB per entry to serve a 540px tile."""
    src = QImage()
    if not src.loadFromData(data) or src.isNull():
        return None
    raw = src
    if max(src.width(), src.height()) > _RAW_MAX_DIM:
        raw = src.scaled(
            _RAW_MAX_DIM,
            _RAW_MAX_DIM,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
    scaled = src.scaled(
        target_w,
        target_h,
        Qt.AspectRatioMode.KeepAspectRatioByExpanding,
        Qt.TransformationMode.SmoothTransformation,
    )
    return raw, scaled


def _on_image_reply_finished(reply: QNetworkReply):
    ctx = _pending_replies.pop(reply, None)
    if ctx is None:
        reply.deleteLater()
        return
    cache_key, sem_key, target_w, target_h, radius, priority, url, fb_done = ctx

    net_err = reply.error()
    http_status = reply.attribute(
        QNetworkRequest.Attribute.HttpStatusCodeAttribute
    )
    # Read the body HERE (the reply is GUI-thread-owned and about to be
    # deleted), then hand the bytes to the pool to decode. A transport
    # success can still fail to decode, so the decode result — not just
    # the transport status — decides success below.
    data = (
        bytes(reply.readAll())
        if net_err == QNetworkReply.NetworkError.NoError
        else b""
    )
    reply.deleteLater()
    if not data:
        _after_image_decode(
            None, ctx, net_err, http_status
        )
        return

    from jellytoast.async_io import run_async

    run_async(
        lambda: _decode_and_scale(data, target_w, target_h),
        on_result=lambda res: _after_image_decode(res, ctx, net_err, http_status),
        on_error=lambda _e: _after_image_decode(None, ctx, net_err, http_status),
    )


def _after_image_decode(res, ctx, net_err, http_status):
    """GUI thread, once the pooled decode resolves. ``res`` is None when
    the fetch failed or the body wasn't decodable — both take the same
    recovery path (resize-fallback, then failure fan-out), which is why
    the decision lives here rather than at reply-finish time."""
    cache_key, sem_key, target_w, target_h, radius, priority, url, fb_done = ctx
    ok = res is not None
    raw_img, scaled_img = res if ok else (None, None)

    # FALLBACK: a sized cover request that FAILED (slow server-side resize
    # past the transfer timeout, a server that rejects the size param, or an
    # error page) — retry ONCE for the ORIGINAL asset (resize params stripped),
    # which the server serves off disk. We re-scale locally, so quality is
    # unchanged. Reuses this load's gate slot + waiters; nothing is faned out
    # yet. (#cover-stall — the recurring "loads some art then stops".)
    if not ok and not fb_done:
        stripped = _strip_resize_params(url)
        if stripped is not None:
            # A TIMEOUT specifically means the server is too slow to GENERATE
            # the resized thumbnail. After a couple of those, latch the whole
            # session onto original-fetch so we stop paying the timeout per
            # cover (#cover-stall).
            if net_err == QNetworkReply.NetworkError.TimeoutError:
                global _resize_timeouts, _prefer_original_covers
                _resize_timeouts += 1
                if _resize_timeouts >= _RESIZE_TIMEOUT_TRIP and not _prefer_original_covers:
                    _prefer_original_covers = True
                    logger.warning(
                        "server slow at cover resize (%d timeouts) — fetching "
                        "originals for the rest of this session", _resize_timeouts,
                    )
            logger.info(
                "cover load failed (%s, http=%s) — retrying without server "
                "resize: %s", net_err.name, http_status, _redact_url(url),
            )
            # Keep the slot: re-fire under the same cache_key; _inflight_
            # subscribers[cache_key] is untouched so waiters still get served.
            try:
                _fire_image_request(
                    cache_key, sem_key, stripped, target_w, target_h,
                    radius, priority, resize_fallback_done=True,
                )
                return
            except Exception:
                # Couldn't even open the retry — fall through to the normal
                # failure fan-out below (and release the slot there).
                logger.warning("cover resize-fallback failed to start")

    if not ok:
        logger.info(
            "cover load failed (%s, http=%s)%s: %s",
            net_err.name, http_status,
            " [after resize-fallback]" if fb_done else "",
            _redact_url(url),
        )

    # Release the gate slot for a gated (normal/low) load and promote the
    # next waiter — NORMAL before LOW so a visible tile beats a prefetch.
    # One at a time keeps the queue draining at the completion rate.
    if priority != "high":
        global _gated_in_flight
        _gated_in_flight -= 1
        _promote_next_deferred()
    if ok:
        _deliver_decoded(cache_key, sem_key, raw_img, scaled_img, radius)
    else:
        _fail_inflight(cache_key, target_w, target_h, radius)


def _deliver_decoded(
    cache_key: str,
    sem_key: str,
    raw_img: "QImage",
    scaled_img: "QImage",
    radius: int,
) -> None:
    """GUI-THREAD tail of a successful load. Everything expensive (decode
    + smooth downscale) already ran on the pool; what's left genuinely
    has to be here — QPixmap isn't thread-safe, and neither is the
    QPainter pass that rounds the corners.

    Stashes the (size-capped) source in L2 (in-memory + on-disk) so a
    future caller wanting a different size of the same image — including
    across launches — derives locally instead of refetching. The on-disk
    raw is what makes the bar fast on a fresh launch when the album-grid
    tile loaded this image in a prior session."""
    _store_raw(sem_key, raw_img)
    _disk_image_cache.put_raw(sem_key, raw_img)
    pix = QPixmap.fromImage(scaled_img)
    if radius > 0:
        pix = _round_corners(pix, radius)
    # Only cache real artwork — an older version cached the placeholder
    # on failure too, which made a transient server hiccup wedge the slot
    # permanently (every retry returned the cached placeholder until 256
    # newer entries evicted it, which on a real library never happens).
    _image_cache[cache_key] = pix
    _image_cache.move_to_end(cache_key)
    while len(_image_cache) > _IMAGE_CACHE_MAX:
        _image_cache.popitem(last=False)
    _disk_image_cache.put(cache_key, pix)
    for cb, _err in _inflight_subscribers.pop(cache_key, []):
        # Guard each subscriber independently: many widgets coalesce onto
        # one in-flight reply, so a single callback raising (typically a
        # deleted-widget RuntimeError when the widget was torn down
        # mid-fetch) must NOT abort the loop and starve the rest.
        try:
            cb(pix)
        except Exception:
            pass


def _placeholder_image(w: int, h: int) -> QImage:
    """Flat tinted fallback when the network fetch or decode fails. Host
    widgets show their own placeholder over this most of the time; the
    tint just prevents a transparent gap if they don't."""
    img = QImage(w, h, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(QColor("#1a1a2e"))
    return img


def _round_corners(pix: QPixmap, radius: int) -> QPixmap:
    out = QPixmap(pix.size())
    out.fill(Qt.GlobalColor.transparent)
    p = QPainter(out)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    path = QPainterPath()
    path.addRoundedRect(0, 0, pix.width(), pix.height(), radius, radius)
    p.setClipPath(path)
    p.drawPixmap(0, 0, pix)
    p.end()
    return out


# ── Formatting ──────────────────────────────────────────────────────────────


def fmt_time(ms: int) -> str:
    if ms < 0:
        ms = 0
    s = ms // 1000
    m, s = divmod(s, 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def fmt_duration_ticks(ticks: int) -> str:
    return fmt_time(ticks // 10_000)


_APP_ICON_CACHE: dict[int, QPixmap] = {}
_APP_ICON_RENDERER: "Optional[QSvgRenderer]" = None


def _load_app_icon_svg_bytes() -> bytes:
    """Read the brand-mark SVG from inside the package via
    ``importlib.resources`` so it resolves in a built/installed wheel
    (a ``Path(__file__).parent.parent / "packaging"`` reference points
    outside the package and is wheel-excluded → blank icon). Returns
    the raw SVG bytes, or ``b""`` if the resource is missing/unreadable
    (the caller then draws a placeholder)."""
    try:
        import importlib.resources as _ir

        res = _ir.files("jellytoast.assets").joinpath("jellytoast.svg")
        if not res.is_file():
            return b""
        return res.read_bytes()
    except Exception:
        return b""


def _draw_placeholder_icon(size: int) -> QPixmap:
    """Last-ditch brand mark when the SVG can't be loaded — a rounded
    accent square so an installed build never renders a blank/empty
    icon. Drawn (not QStyle-based) so it works without a live QStyle."""
    pix = QPixmap(size, size)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    try:
        from jellytoast.theme import get_active_theme as _gt

        accent = _gt().accent
    except Exception:
        accent = "#e0a44c"
    path = QPainterPath()
    radius = size * 0.22
    inset = size * 0.12
    path.addRoundedRect(QRectF(inset, inset, size - 2 * inset, size - 2 * inset), radius, radius)
    p.fillPath(path, QColor(accent))
    p.end()
    return pix


def make_app_icon(size: int = 64) -> QPixmap:
    """jellytoast logo, rasterized from the bundled
    ``jellytoast/assets/jellytoast.svg`` at the requested pixel size.
    Single source of truth for the brand mark — edits to the SVG flow
    to every surface (window decoration, tray, QApplication app icon)
    on next launch. Loaded via ``importlib.resources`` so it ships in
    the wheel; falls back to a drawn placeholder if the SVG is missing
    or the renderer is invalid, so an installed build never renders a
    blank icon. Cached per size since the icon is requested 3+ times
    during launch (QApplication, JellytoastWindow, TrayController) and
    the pixmap is immutable."""
    cached = _APP_ICON_CACHE.get(size)
    if cached is not None:
        return cached
    global _APP_ICON_RENDERER
    if _APP_ICON_RENDERER is None:
        from PySide6.QtCore import QByteArray

        svg_bytes = _load_app_icon_svg_bytes()
        _APP_ICON_RENDERER = QSvgRenderer(QByteArray(svg_bytes)) if svg_bytes else QSvgRenderer()
    if not _APP_ICON_RENDERER.isValid():
        # Don't cache the placeholder under the per-size key — if the
        # renderer later becomes valid (it won't here, but keep the
        # contract clean) we'd want a real render. Placeholder is cheap.
        return _draw_placeholder_icon(size)
    pix = QPixmap(size, size)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    _APP_ICON_RENDERER.render(p, QRectF(0, 0, size, size))
    p.end()
    _APP_ICON_CACHE[size] = pix
    return pix


# ── Scrubbable slider ──────────────────────────────────────────────────────
# Used by every slider that should "feel like a music player slider":
# clicking anywhere in the groove jumps to that value, dragging continues
# to scrub. Stock QSlider only page-steps when you click off the handle,
# which is the wrong default for progress / volume / seek bars.
#
# Also kills the focus rectangle — Qt's default focus indicator paints
# blue notches at the slider edges that read as "brackets" against a
# hairline groove. NoFocus removes them and removes the slider from the
# tab order (transport sliders are mouse-only by design).


class ScrubbableSlider(QSlider):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

    def _value_at(self, pos: int) -> int:
        # Horizontal sliders read from x; vertical from y. Pick whichever
        # axis matches the current orientation so this class works for
        # both without a separate subclass.
        #
        # ``QStyle.sliderValueFromPosition`` is orientation-naïve — it
        # just maps a 1D pixel position to a value range. Qt's default
        # vertical QSlider visual is *top = max* (volume-control
        # convention), but with ``upsideDown=False`` the function maps
        # position 0 → min, which would flip drag direction relative to
        # the visual. So vertical sliders need ``upsideDown=True`` to
        # match the default visual; ``invertedAppearance`` then flips
        # back as expected. Horizontal sliders pass it straight through.
        if self.orientation() == Qt.Orientation.Horizontal:
            span = max(1, self.width())
            upside_down = self.invertedAppearance()
        else:
            span = max(1, self.height())
            upside_down = not self.invertedAppearance()
        return QStyle.sliderValueFromPosition(
            self.minimum(),
            self.maximum(),
            pos,
            span,
            upside_down,
        )

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            pos = (
                int(e.position().x())
                if self.orientation() == Qt.Orientation.Horizontal
                else int(e.position().y())
            )
            v = self._value_at(pos)
            # setSliderDown so consumer-side position-update slots that
            # gate on isSliderDown() pause their writes during the scrub
            # — otherwise the playback timer fights the user's drag.
            self.setSliderDown(True)
            self.setValue(v)
            self.sliderMoved.emit(v)
            e.accept()
            return
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if e.buttons() & Qt.MouseButton.LeftButton and self.isSliderDown():
            pos = (
                int(e.position().x())
                if self.orientation() == Qt.Orientation.Horizontal
                else int(e.position().y())
            )
            v = self._value_at(pos)
            self.setValue(v)
            self.sliderMoved.emit(v)
            e.accept()
            return
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton and self.isSliderDown():
            self.setSliderDown(False)
            e.accept()
            return
        super().mouseReleaseEvent(e)


# ── Marquee label ────────────────────────────────────────────────────────


class MarqueeLabel(QLabel):
    """QLabel that scrolls its text horizontally when the text exceeds
    the label's width. Pauses briefly at the start of each cycle so the
    beginning of the text is readable before it moves.

    Pacing: 30fps repaint (smooth) at a sub-pixel speed (slow). The 0.5
    px/tick ≈ 15 px/sec — about a third of typical marquee speed, tuned
    for ambient/glanceable use rather than pulling the eye. Timer is
    only running while a scroll is actually needed; widening the label
    so the text fits cancels the timer."""

    SPEED_PX_PER_TICK = 0.5
    GAP_PX = 48
    PAUSE_TICKS = 90  # ~3s at 33ms tick — longer dwell on the start
    TICK_MS = 33
    FADE_PX = 16  # soft edge so overflow dissolves instead of hard-clipping

    def __init__(self, text: str = "", parent=None):
        super().__init__(parent)
        self._marquee_text = ""
        self._marquee_offset_f = 0.0
        self._marquee_offset = 0
        self._pause = self.PAUSE_TICKS
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.setInterval(self.TICK_MS)
        if text:
            self.setText(text)

    def setText(self, text: str):
        if text == self._marquee_text:
            return
        self._marquee_text = text or ""
        self._marquee_offset_f = 0.0
        self._marquee_offset = 0
        self._pause = self.PAUSE_TICKS
        super().setText(self._marquee_text)
        self._update_marquee_state()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._update_marquee_state()

    def _text_width(self) -> int:
        return self.fontMetrics().horizontalAdvance(self._marquee_text)

    def _needs_scroll(self) -> bool:
        return bool(self._marquee_text) and self._text_width() > self.width()

    def _update_marquee_state(self):
        if self._needs_scroll():
            if not self._timer.isActive():
                self._timer.start()
        else:
            self._timer.stop()
            self._marquee_offset_f = 0.0
            self._marquee_offset = 0
            self.update()

    def _tick(self):
        if self._pause > 0:
            self._pause -= 1
            return
        cycle = self._text_width() + self.GAP_PX
        self._marquee_offset_f = (self._marquee_offset_f + self.SPEED_PX_PER_TICK) % cycle
        if self._marquee_offset_f < self.SPEED_PX_PER_TICK:
            self._pause = self.PAUSE_TICKS
        self._marquee_offset = int(self._marquee_offset_f)
        self.update()

    def paintEvent(self, e):
        if not self._needs_scroll():
            super().paintEvent(e)
            return
        w, h = self.width(), self.height()
        if w <= 0 or h <= 0:
            return
        # Render the scrolling text into a transparent layer, then dissolve its
        # edges with an alpha mask. A hard right clip (drawText into the widget)
        # looks like a broken mid-word elide in a still frame — and the ~3s
        # start-pause shows that clip live after every track change. The mask is
        # applied to the ISOLATED layer (DestinationIn), so it only touches the
        # text and never punches a hole in the frosted bar painted behind us.
        dpr = self.devicePixelRatioF()
        buf = QPixmap(max(1, round(w * dpr)), max(1, round(h * dpr)))
        buf.setDevicePixelRatio(dpr)
        buf.fill(Qt.GlobalColor.transparent)
        bp = QPainter(buf)
        bp.setPen(self.palette().color(self.foregroundRole()))
        bp.setFont(self.font())
        fm = bp.fontMetrics()
        baseline = (h + fm.ascent() - fm.descent()) // 2
        text_w = fm.horizontalAdvance(self._marquee_text)
        x = -self._marquee_offset
        bp.drawText(x, baseline, self._marquee_text)
        bp.drawText(x + text_w + self.GAP_PX, baseline, self._marquee_text)
        fade = min(self.FADE_PX, w // 2)
        if fade > 0:
            bp.setCompositionMode(
                QPainter.CompositionMode.CompositionMode_DestinationIn
            )
            rg = QLinearGradient(w - fade, 0, w, 0)
            rg.setColorAt(0.0, QColor(0, 0, 0, 255))
            rg.setColorAt(1.0, QColor(0, 0, 0, 0))
            bp.fillRect(QRectF(w - fade, 0, fade, h), rg)
            # Fade the left edge too, but only once scrolled past the start, so
            # the head isn't faded during the opening dwell (when it sits next
            # to the cover art).
            if self._marquee_offset > 0:
                lg = QLinearGradient(0, 0, fade, 0)
                lg.setColorAt(0.0, QColor(0, 0, 0, 0))
                lg.setColorAt(1.0, QColor(0, 0, 0, 255))
                bp.fillRect(QRectF(0, 0, fade, h), lg)
        bp.end()
        p = QPainter(self)
        p.drawPixmap(0, 0, buf)


# ── Cover-overlay button ─────────────────────────────────────────────────


def overlay_disc_colors() -> tuple[str, str]:
    """``(normal, hover)`` fill for a circular button that floats over
    album art — the favourite heart, the mini-player close button, the
    album-tile play / download overlays.

    The disc is the OPPOSITE tone to the ink: a light disc on a light
    theme, a dark disc on a dark theme — exactly inverted, same alpha
    both ways. The glyph on top is theme-ink (black on light,
    near-white on dark), so an inverse-tone disc keeps it readable on
    any cover. Deliberately translucent — the cover reads through."""
    r, g, b = _hex_to_rgb_safe(TEXT)
    base = "255,255,255" if r + g + b < 384 else "0,0,0"
    return f"rgba({base},0.50)", f"rgba({base},0.66)"


def overlay_disc_qcolor(hover: bool = False) -> QColor:
    """QColor form of :func:`overlay_disc_colors` for ``paintEvent`` /
    delegate code (album-tile corner buttons, the download progress
    ring). Same inverse-of-ink logic + alpha as the QSS form."""
    r, g, b = _hex_to_rgb_safe(TEXT)
    v = 255 if r + g + b < 384 else 0
    return QColor(v, v, v, 168 if hover else 128)


class CoverOverlayButton(IconButton):
    """Small circular button pinned to the bottom-right of its parent
    widget — used by the now-playing surfaces to overlay a heart on
    the album art. Repositions on parent resize and only shows while
    the cursor is hovering the cover.

    The visibility tracking uses ``parent.underMouse()`` — which Qt
    treats as true when the cursor is anywhere within the parent's
    geometric bounds *including* descendant widgets. That means the
    overlay button itself doesn't trigger a hide when the cursor moves
    onto it: the parent's Leave fires (Qt routes mouse to the child),
    we schedule a hide with a small grace, then ``underMouse`` reports
    true and we cancel.
    """

    DEFAULT_SIZE = 28
    DEFAULT_MARGIN = 8
    HIDE_GRACE_MS = 80

    def __init__(
        self,
        parent: QWidget,
        size: int = DEFAULT_SIZE,
        margin: int = DEFAULT_MARGIN,
        bordered: bool = True,
    ):
        super().__init__(parent)
        self._anchor_margin = margin
        self._bordered = bordered
        self.setFixedSize(size, size)
        self._apply_circle_style()
        # Re-tone the disc on a live theme switch (light disc on a
        # light theme, dark on a dark one). Lazy import dodges the
        # ui_helpers ↔ player_state import cycle.
        try:
            from jellytoast.player_state import PlayerBus

            PlayerBus.get().theme_changed.connect(self._apply_circle_style)
        except Exception:
            pass
        self.hide()
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.setInterval(self.HIDE_GRACE_MS)
        self._hide_timer.timeout.connect(self._maybe_hide)
        parent.installEventFilter(self)
        self._reposition()

    def _apply_circle_style(self):
        """(Re)build the disc QSS. Theme-aware via overlay_disc_colors()
        — ``bordered=False`` (the mini player) drops the faint rim."""
        radius = self.width() // 2
        normal, hover = overlay_disc_colors()
        if self._bordered:
            ir, ig, ib = _hex_to_rgb_safe(TEXT)
            border = f"1px solid rgba({ir},{ig},{ib},0.18)"
            hover_border = f"    border-color: rgba({ir},{ig},{ib},0.35);\n"
        else:
            border = "none"
            hover_border = ""
        self.setStyleSheet(f"""
            QPushButton {{
                background: {normal};
                border: {border};
                border-radius: {radius}px;
            }}
            QPushButton:hover {{
                background: {hover};
{hover_border}            }}
        """)

    def eventFilter(self, obj, event):
        et = event.type()
        if et == QEvent.Type.Resize:
            self._reposition()
        elif et == QEvent.Type.Enter:
            self._hide_timer.stop()
            self.show()
            self.raise_()
        elif et == QEvent.Type.Leave:
            self._hide_timer.start()
        return False

    def _maybe_hide(self):
        p = self.parentWidget()
        if p is None:
            return
        if not p.underMouse():
            self.hide()

    def _reposition(self):
        p = self.parentWidget()
        if p is None:
            return
        x = p.width() - self.width() - self._anchor_margin
        y = p.height() - self.height() - self._anchor_margin
        self.move(max(0, x), max(0, y))
        self.raise_()


# ── Empty-state widget ──────────────────────────────────────────────────


class EmptyState(QWidget):
    """Centered glyph + headline + optional sub-line + optional action
    button. Drop into any scroll area, grid, or list whose data set
    can be legitimately empty (no albums on the server, queue empty,
    no search results, etc.). Replaces "blank viewport" failure modes
    that read as "is this loading or broken?" with an intentional
    "this is empty, here's why" affordance.

    Use ``set_state(headline=..., sub=..., glyph=...)`` to repurpose
    the same instance for different empty conditions on one surface.
    The ``action_clicked`` signal fires when the optional button is
    pressed — callers wire it to whatever recovery action makes sense
    (Retry, Browse, etc.)."""

    GLYPH_PX = 64  # default glyph point size
    VPAD = 18  # spacing between rows

    action_clicked = Signal()

    def __init__(
        self,
        glyph: str = "♪",  # ♪ — default to "nothing playing" semantic
        headline: str = "",
        sub: str = "",
        action_label: Optional[str] = None,
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, False)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 24, 24, 24)
        outer.setSpacing(self.VPAD)
        outer.addStretch(1)

        # Glyph — large muted character. Unicode rather than an SVG so
        # the widget has no external resource dependency and renders
        # at any size without re-rasterising.
        self._glyph_label = QLabel(glyph)
        gf = QFont()
        gf.setPixelSize(self.GLYPH_PX)
        self._glyph_label.setFont(gf)
        self._glyph_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        outer.addWidget(self._glyph_label, 0, Qt.AlignmentFlag.AlignHCenter)

        self._headline_label = QLabel(headline)
        self._headline_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._headline_label.setWordWrap(True)
        outer.addWidget(self._headline_label)

        self._sub_label = QLabel(sub)
        self._sub_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._sub_label.setWordWrap(True)
        outer.addWidget(self._sub_label)
        if not sub:
            self._sub_label.hide()

        # Action row — button is created up front but hidden unless
        # action_label is provided so callers can flip it on later
        # via set_state without rebuilding the widget.
        action_row = QHBoxLayout()
        action_row.setContentsMargins(0, 0, 0, 0)
        action_row.addStretch(1)
        self._action_btn = QPushButton(action_label or "")
        self._action_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._action_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._action_btn.clicked.connect(self.action_clicked.emit)
        action_row.addWidget(self._action_btn)
        action_row.addStretch(1)
        outer.addLayout(action_row)
        if not action_label:
            self._action_btn.hide()

        outer.addStretch(1)

        self._apply_styling()
        # Live-accent: re-stamp the baked QSS on every theme/accent swap so
        # a visible overlay isn't left in the old palette (e.g. a white
        # headline on a light body after a dark->light swap = invisible).
        # Per-surface re-stamp contract; see architecture_live_accent.md.
        # PySide6 auto-disconnects this bound-method slot when the widget is
        # destroyed, so call sites that recreate the overlay don't leak.
        from jellytoast.player_state import PlayerBus

        PlayerBus.get().theme_changed.connect(self._apply_styling)

    def _apply_styling(self) -> None:
        """(Re-)stamp the per-widget QSS from the current theme tokens.
        Called at construction and on every ``PlayerBus.theme_changed``.
        Reads the ui_helpers module-level tokens by name so each call
        picks up the values ``refresh_theme()`` rebound in place."""
        from jellytoast.design_tokens import TYPE_BODY, TYPE_CAPTION, type_qss

        # Theme ink at low alpha — faint glyph, legible on either theme.
        self._glyph_label.setStyleSheet(f"color: {ink_alpha(0.22)};")
        self._headline_label.setStyleSheet(
            f"color: {TEXT}; {type_qss(TYPE_BODY)} font-weight: 500;"
        )
        self._sub_label.setStyleSheet(f"color: {TEXT_DIM}; {type_qss(TYPE_CAPTION)}")
        self._action_btn.setStyleSheet(f"""
            QPushButton {{
                background: {WASH_HOVER};
                border: 1px solid {ink_alpha(0.10)};
                border-radius: {rad(8)}px;
                padding: 6px 14px;
                color: {TEXT};
                font-weight: 500;
            }}
            QPushButton:hover {{ background: {WASH_PRESSED}; }}
        """)

    def set_state(
        self,
        *,
        glyph: Optional[str] = None,
        headline: Optional[str] = None,
        sub: Optional[str] = None,
        action_label: Optional[str] = None,
    ):
        """Update any subset of the visible content. Pass ``""`` for
        ``sub`` or ``action_label`` to hide those rows; pass ``None``
        (default) to leave them untouched."""
        if glyph is not None:
            self._glyph_label.setText(glyph)
        if headline is not None:
            self._headline_label.setText(headline)
        if sub is not None:
            self._sub_label.setText(sub)
            self._sub_label.setVisible(bool(sub))
        if action_label is not None:
            self._action_btn.setText(action_label)
            self._action_btn.setVisible(bool(action_label))


# ── Popup menu helpers ──────────────────────────────────────────────────


def apply_elevated_blur(widget, corner_radius: int = 0) -> bool:
    """Install compositor blur behind ``widget`` when the active theme
    asks for it (any theme with ``blur=True`` — the frosted modes).

    Top-level "elevated" surfaces (combo popups, QMenus, hover
    tooltips, the volume popup window) read this so the frosted-glass
    look extends past the main window body — without it the body is
    blurred but a popup floating free over the wallpaper would be a
    flat translucent rectangle. No-op for non-frosted themes (nothing
    to blur, the surface stays whatever its fill says).

    Idempotent; callers may invoke on every show. ``widget`` must
    have a platform window (``windowHandle()``) — call after the
    popup is shown, or via ``showEvent`` / ``aboutToShow``.
    """
    try:
        from jellytoast import blur as _blur
        from jellytoast.theme import get_active_theme

        if not get_active_theme().blur:
            return False
        # elevated=True: these surfaces carry their own QSS frost fill,
        # so a backend with a tinted blur material (Windows Acrylic)
        # drops its tint instead of double-veiling the popup.
        return _blur.apply(widget, True, corner_radius=corner_radius, elevated=True)
    except Exception:
        return False


def opaque_menu(parent=None, *, menu_cls=None, blur_corner_radius: int = 4) -> "QMenu":
    """``QMenu`` that's guaranteed opaque even when the parent window
    has ``WA_TranslucentBackground`` set. On Wayland a popup-class
    window inherits the ancestor's translucency attribute at QWindow
    creation, and Qt 6 doesn't reliably honour a later
    ``setAttribute(WA_TranslucentBackground, False)`` because the
    surface was already constructed as ARGB. The result: ghost text
    bleeds through the menu over content beneath.

    The fix is layered — every layer is defensive against a different
    failure mode, and together they produce opaque pixels even if any
    single mechanism misbehaves:

    - ``WA_TranslucentBackground=False`` + ``WA_NoSystemBackground=
      False`` ask the platform plugin for an opaque surface.
    - ``WA_OpaquePaintEvent=True`` skips Qt's pre-paint clear pass.
    - ``setAutoFillBackground(True)`` + opaque palette ``Window`` /
      ``Base`` colours fill the widget rect with solid pixels before
      QSS paints — even if the surface ends up ARGB, the autofill
      writes alpha=255 across the whole popup.
    - The stylesheet then paints over those filled pixels with the
      menu's visual treatment. Selection uses the accent colour at
      moderate alpha (we lift the accent live from the active theme
      so a runtime accent change takes effect on the next menu open).

    Use this everywhere you'd otherwise call ``QMenu(parent)`` so the
    fix lives in one spot. Pass ``menu_cls`` to harden a ``QMenu``
    subclass instead of a vanilla ``QMenu`` (e.g. a stay-open multi-select
    menu) while keeping the same opacity/blur treatment. ``blur_corner_radius``
    shapes the compositor blur region to the menu's rounded rect — pass the
    same radius the caller's QSS uses (the top-bar dropdowns override to 8 px)
    so the blur doesn't bleed past the visible corners into a square halo;
    defaults to 4 to match this function's own QSS ``border-radius``.
    """
    from jellytoast.theme import _hex_to_rgb

    menu = (menu_cls or QMenu)(parent)
    # Keep the menu surface translucent so its QSS rgba background composites
    # over compositor blur (the lifted-frosted-glass look) ONLY when blur is
    # verified active behind it. On solid / transparent themes — or a frosted
    # theme on a box where blur didn't land — there's nothing to backstop
    # see-through, so harden to an opaque panel instead of a thin pill.
    if popup_blur_active():
        menu.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        # corner_radius matches the QMenu QSS border-radius (4 px)
        # below, so KWin's blur region is shaped to the rounded pill
        # instead of the rectangular bounding box. Without this the
        # corners read as SQUARE — blurred wallpaper shows through
        # outside the QSS clip but inside the blur-rect.
        #
        # Deferred via QTimer.singleShot(0, ...) so the blur runs
        # AFTER Qt has finished laying out the menu in this event-
        # loop tick — at aboutToShow time the menu's width/height may
        # still be stale, making the rounded blur region too small
        # and leaving most of the menu surface unblurred.
        from PySide6.QtCore import QTimer

        def _do_blur(m=menu):
            # Round the BLUR region a touch tighter than the QSS corner so
            # its (1-bit, aliased) rounded edge tucks UNDER the menu's
            # smooth antialiased QSS corner instead of peeking past it as a
            # jagged sliver — that mismatch is what read as "weird corners".
            apply_elevated_blur(m, corner_radius=blur_corner_radius + 2)

        menu.aboutToShow.connect(
            lambda m=menu: QTimer.singleShot(0, lambda: _do_blur(m))
        )
    else:
        _harden_popup_opacity(menu)
    a_r, a_g, a_b = _hex_to_rgb(ACCENT)
    # Frosty fill when blur is verified behind the menu (lets the blur lift
    # through), opaque otherwise — mirrors the WA_TranslucentBackground gate
    # above so the menu's paint and its surface translucency agree.
    menu.setStyleSheet(f"""
        QMenu {{
            background-color: {popup_body_fill()};
            color: {TEXT};
            border: none;
            border-radius: {rad(4)}px;
            padding: 4px;
        }}
        QMenu::item {{
            background-color: transparent;
            /* Symmetric horizontal padding — the old right-padding of
               22 reserved space for a shortcut/arrow column we don't
               use, which made every menu wider than its longest entry
               needed. Symmetric padding tightens the menu to its
               content + matches the visual balance left↔right. */
            padding: 7px 14px;
            border-radius: {rad(4)}px;
        }}
        QMenu::item:selected {{
            background-color: rgba({a_r},{a_g},{a_b},0.28);
            color: {TEXT};
        }}
        QMenu::separator {{
            height: 1px;
            background: {BORDER};
            margin: 4px 8px;
        }}
    """)
    return menu


def popup_fill_qcolor() -> QColor:
    """Opaque QColor form of the active theme's ``POPUP_OPAQUE_FILL``
    token — for the palette autofill backstop in ``_harden_popup_opacity``
    where an opaque palette ``Window`` is required to paint solid
    pixels under the QSS. Alpha is STRIPPED here even if the token
    is rgba (frosted themes diverge ``popup_opaque_fill`` to a
    translucent composite for tooltip painting; the autofill backstop
    still wants the opaque rgb, since menus/combos that go through
    ``_harden_popup_opacity`` need solid fill). Use ``popup_paint_qcolor``
    instead when the caller WANTS the alpha (e.g. translucent tooltip
    paint over a blurred surface)."""
    try:
        s = POPUP_OPAQUE_FILL
        inner = s[s.index("(") + 1 : s.index(")")]
        parts = [p.strip() for p in inner.split(",")]
        return QColor(int(parts[0]), int(parts[1]), int(parts[2]))
    except Exception:
        return QColor(20, 22, 26)


def popup_blur_active() -> bool:
    """True when an elevated popup (tooltip / menu / combo / About) should
    render as translucent glass — i.e. the theme wants blur AND real compositor
    blur is *verified* behind it. False on non-frosted themes, or a frosted
    theme on a box where blur didn't land, so popups harden to a near-opaque
    panel instead of reading thin / see-through. The popup analogue of the
    blur-status check in :func:`body_color_tuple`. Never raises."""
    try:
        from jellytoast import blur
        from jellytoast.theme import get_active_theme

        return bool(get_active_theme().blur) and (
            blur.status() is blur.BlurStatus.ACTIVE
        )
    except Exception:
        return False


def popup_paint_qcolor() -> QColor:
    """Status-aware elevated-popup body colour for PAINTING a popup backdrop
    (the tooltip pill, the About dialog body, the _Selector dropdown). Returns
    the full rgba QColor from ``POPUP_OPAQUE_FILL`` — but, like
    :func:`body_color_tuple`, the alpha tracks whether real blur is verified
    behind the popup: the translucent glass tone when :func:`popup_blur_active`,
    a near-opaque panel otherwise, so popups never read thin / see-through on a
    box without working blur. Opaque (rgb) fills return unchanged."""
    try:
        s = POPUP_OPAQUE_FILL
        inner = s[s.index("(") + 1 : s.index(")")]
        parts = [p.strip() for p in inner.split(",")]
        r, g, b = int(parts[0]), int(parts[1]), int(parts[2])
        a = int(round(float(parts[3]) * 255)) if len(parts) >= 4 else 255
    except Exception:
        return QColor(20, 22, 26)
    if a < 255 and not popup_blur_active():
        # Frosted popup with no verified blur behind it — harden to a
        # near-opaque panel (matches the body fallback in body_color_for).
        from jellytoast.theme import get_active_theme

        a = max(a, getattr(get_active_theme(), "fallback_body_alpha", None) or 240)
    elif a < 255:
        # Real blur verified — cap the alpha so the blur lifts through as
        # "a slight lift, still frosty" instead of a near-solid panel
        # (the light family's token is tuned opaque at 0.80). Mirrors
        # popup_body_fill() for the QSS-painted popups.
        a = min(a, int(round(_POPUP_FROST_ALPHA * 255)))
    return QColor(r, g, b, max(0, min(255, a)))


# Target alpha for a blur-backed popup body — low enough that the
# compositor blur reads through as "a slight lift, still frosty" rather
# than a solid panel. The light family's POPUP_OPAQUE_FILL was tuned to
# 0.80 (vs the dark family's 0.65), so its menus / combos read as stark
# white over the frosted body; capping the painted alpha here when real
# blur is verified brings both families to the same frosted depth.
_POPUP_FROST_ALPHA = 0.62


def popup_body_fill() -> str:
    """QSS background fill for a blur-AWARE popup (the ``opaque_menu`` menus,
    the _Selector dropdown, the About body). When real compositor blur is
    verified behind the popup, return the ``POPUP_OPAQUE_FILL`` hue at a
    capped frosted alpha (``_POPUP_FROST_ALPHA``) so the blur lifts through;
    otherwise return ``POPUP_OPAQUE_FILL`` unchanged so the popup stays
    opaque and legible on a box with no working blur. Never raises.

    Bare ``QMenu`` / ``QComboBox`` popups (GLOBAL_STYLE) still use the raw
    opaque token — they get no ``blur.apply()`` so they MUST stay opaque."""
    if not popup_blur_active():
        return POPUP_OPAQUE_FILL
    try:
        r, g, b, a = _parse_qss_color(POPUP_OPAQUE_FILL)
    except Exception:
        return POPUP_OPAQUE_FILL
    a = min(a, _POPUP_FROST_ALPHA)
    return f"rgba({r}, {g}, {b}, {a:.2f})"


def _parse_qss_color(s: str) -> tuple[int, int, int, float]:
    """Parse a QSS colour literal — ``#rrggbb``, ``rgb(r,g,b)``, or
    ``rgba(r,g,b,a)`` — into ``(r, g, b, a)`` with ``a`` in 0..1. Falls
    back to opaque mid-grey on anything unparseable. Never raises."""
    try:
        s = s.strip()
        if s.startswith("#"):
            from jellytoast.theme import _hex_to_rgb

            r, g, b = _hex_to_rgb(s)
            return r, g, b, 1.0
        inner = s[s.index("(") + 1 : s.index(")")]
        parts = [p.strip() for p in inner.split(",")]
        r, g, b = int(parts[0]), int(parts[1]), int(parts[2])
        a = float(parts[3]) if len(parts) >= 4 else 1.0
        return r, g, b, a
    except Exception:
        return (128, 128, 128, 1.0)


def volume_popup_fill() -> str:
    """Opaque, NEUTRAL fill for the volume slider popup, baked to read as
    the SAME elevated tone as the volume BUTTON's hover highlight.

    The popup is a child surface (can't ride KWin blur), so it has to be a
    flat OPAQUE pill — but it must still match the hovered volume button it
    sits over, which is ``wash_hover`` riding the blurred body. So on the
    FROSTED themes we reproduce that highlight directly: composite the
    theme's ``wash_hover`` over a representative backdrop, then neutralise
    to gray (the button wash is hueless, so r==g==b — no blue cast) and
    force it opaque (the mini-player right-edge popup must hide the volume
    button + ✕ it overlaps — a translucent fill would let them ghost
    through).

    The backdrop is this theme's ``body_color`` over a neutral mid-gray
    stand-in for the (unknowable) blurred wallpaper. That mid-gray term is
    the crux: an earlier attempt composited the wash over the body's raw RGB
    alone and read TOO DARK; the real desktop behind the body lightens it,
    so the stand-in restores that. With it the popup lands at the hovered
    button's apparent tone in each family — ≈224 on light, ≈74 on dark.

    The prior implementation took the raw luminance of ``popup_opaque_fill``
    and DROPPED its alpha. That's fine on dark (the token's RGB is already a
    dark 67) but baked the light token's near-white *wash* (248,248,248 @
    0.80) to a stark 248 — far whiter than the 0.55-alpha button highlight
    it sits over, so the light popup read as a bright white slab. (A later
    ``min(lum, 238)`` cap only shaved the worst off and still read white.)

    Solid (non-frosted) themes carry an already-opaque ``rgb()``
    ``popup_opaque_fill`` (a == 1.0) tuned to the elevated tone; those are
    returned as-is (neutralised), unchanged. Reads the live theme so a
    dark↔light flip retints it."""
    from jellytoast.theme import get_active_theme

    th = get_active_theme()
    pr, pg, pb, pa = _parse_qss_color(th.popup_opaque_fill)
    body = getattr(th, "body_color", None)
    if pa < 1.0 and body and len(body) == 4:
        # Frosted theme: rebuild the button-hover highlight as an opaque
        # tone. wash_hover over (body over neutral-gray) — see docstring.
        wr, wg, wb, wa = _parse_qss_color(th.wash_hover)
        body_a = body[3] / 255.0
        _NEUTRAL = 128.0
        back = [body_a * body[i] + (1.0 - body_a) * _NEUTRAL for i in range(3)]
        r = wa * wr + (1.0 - wa) * back[0]
        g = wa * wg + (1.0 - wa) * back[1]
        b = wa * wb + (1.0 - wa) * back[2]
    else:
        r, g, b = pr, pg, pb
    # Perceived (WCAG) luminance → neutral gray; drops any cool tint while
    # preserving the composited lightness.
    lum = max(0, min(255, round(0.2126 * r + 0.7152 * g + 0.0722 * b)))
    return f"rgb({lum}, {lum}, {lum})"


# Backdrop blur for the volume popup (in-app "acrylic"). Tunable by eye.
VOLUME_BACKDROP_RADIUS = 12  # logical px of blur + grab padding


def volume_popup_veil_qcolor() -> "QColor":
    """Semi-transparent veil painted OVER the software-blurred backdrop in the
    volume popup so the slider handle / accent fill / padlock stay legible
    while the frost still reads through. The neutral ``volume_popup_fill()``
    tone at a per-family reduced alpha (lower than the opaque pill so the blur
    shows). Tunable by eye."""
    from PySide6.QtGui import QColor

    from jellytoast.theme import get_active_theme

    fill = volume_popup_fill()  # "rgb(l, l, l)"
    try:
        inner = fill[fill.index("(") + 1 : fill.index(")")]
        r, g, b = (int(x) for x in inner.split(",")[:3])
    except Exception:
        r = g = b = 128
    light = not getattr(get_active_theme(), "dark", False)
    # Dark dropped 0.55 → 0.42: at 0.55 the neutral veil over the (already
    # dark) captured backdrop reconstituted the flat opaque-pill tone, so the
    # frost was invisible. A thinner dark veil lets the blurred backdrop read
    # through. Light stays 0.62 (it already reads well). Tunable by eye.
    a = int(round((0.62 if light else 0.42) * 255))
    return QColor(r, g, b, a)


def capture_blurred_backdrop(
    host: "QWidget", geom: "QRect", *, radius_logical: int = VOLUME_BACKDROP_RADIUS
) -> "Optional[QPixmap]":
    """Grab the ``host`` pixels under ``geom`` and return a software-blurred
    QPixmap of that region — the in-app "frosted glass" backdrop for the volume
    popup (a child surface that can't ride compositor blur).

    KEY: ``QWidget.grab()`` re-renders the widget tree into an offscreen
    pixmap; it does NOT round-trip the Wayland compositor, so the "grab is
    blur-blind" caveat (about *compositor* blur) does not apply to a frost we
    paint ourselves. The grab is expanded by ``radius_logical`` on every side so
    the blur has padding to bleed into (no sharp clipped edge); the caller draws
    the result offset by ``-radius_logical`` and clips to the body's rounded
    rect. A fast, predictable downscale→upscale (SmoothTransformation) blur —
    cheap enough for a hover popup and free of QGraphicsScene coordinate
    pitfalls. Returns None on any failure → caller falls back to the opaque
    pill. Never raises."""
    try:
        from PySide6.QtCore import Qt as _Qt

        if host is None:
            return None
        r = int(radius_logical)
        grab_rect = geom.adjusted(-r, -r, r, r)
        src = host.grab(grab_rect)  # QPixmap, physical size, dpr-tagged
        if src is None or src.isNull() or src.width() < 2 or src.height() < 2:
            return None
        dpr = src.devicePixelRatio() or screen_dpr(host)
        # Downscale→upscale box blur. Stronger shrink = softer frost; scale the
        # shrink with the (device) radius so it reads consistent across DPRs.
        shrink = max(3, int(round(r * dpr / 2.0)))
        sw = max(1, src.width() // shrink)
        sh = max(1, src.height() // shrink)
        small = src.scaled(
            sw, sh, _Qt.AspectRatioMode.IgnoreAspectRatio, _Qt.TransformationMode.SmoothTransformation
        )
        blurred = small.scaled(
            src.width(),
            src.height(),
            _Qt.AspectRatioMode.IgnoreAspectRatio,
            _Qt.TransformationMode.SmoothTransformation,
        )
        blurred.setDevicePixelRatio(dpr)
        return blurred
    except Exception:
        return None


def _harden_popup_opacity(popup: "QWidget") -> None:
    """Force ``popup`` to render opaque even when its ancestor window
    has ``WA_TranslucentBackground`` set.

    Applies the same multi-layer fix used by ``opaque_menu``:
    translucent-background OFF, system-background ON, opaque paint
    event flag set, autoFillBackground True, palette ``Window`` /
    ``Base`` set to the theme's opaque popup fill. Idempotent — safe
    to call on the same widget multiple times.

    Use directly on custom popups (volume sliders, drag chips, etc.)
    where ``QMenu`` / ``QComboBox`` plumbing doesn't apply. Combobox
    callers should use ``_OpaqueComboBox`` from ``settings_dialog`` so
    the per-popup show-time fixup also runs.
    """
    popup.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
    popup.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, False)
    popup.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
    popup.setAutoFillBackground(True)
    pal = popup.palette()
    fill = popup_fill_qcolor()
    pal.setColor(pal.ColorRole.Window, fill)
    pal.setColor(pal.ColorRole.Base, fill)
    popup.setPalette(pal)


# ── Seeded-radio entry point (album / artist / genre) ────────────────────
#
# The track-radio flow seeds an INSTANT_MIX queue with the track itself
# and lets ``queue_manager.RadioFeeder`` fetch similar tracks once
# playback nears the tail (see ``SongsView._on_context_menu``). Album /
# artist / genre have no single seed *item* to drop into the queue, so
# ``start_seed_radio`` must fetch the initial batch itself (off the GUI
# thread via ``async_io.run_async``) before emitting ``queue_play_now``.
# The RadioFeeder then auto-extends from the stamped ``seed_kind``
# exactly as it does for the track flow.


def start_seed_radio(seed_kind: str, source_id: str, source_label: str) -> None:
    """Fetch the initial radio batch for ``seed_kind`` and install it as
    the live INSTANT_MIX queue.

    ``seed_kind`` is one of ``"album"`` / ``"artist"`` / ``"genre"``:

      * ``album``  → ``get_instant_mix(source_id)``
      * ``artist`` → ``get_similar_songs(source_id)``
      * ``genre``  → ``get_genre_radio(source_label)``

    The provider call is a network round-trip, so it runs on the shared
    pool. On an empty result (or any failure) nothing is emitted — the
    user just sees no change, matching the "show nothing fancy on
    failure" contract. Called from the view-internal right-click menus
    (``LibraryGrid.contextMenuEvent``, ``_GenresListView``) that each
    own their ``QMenu``.
    """
    if seed_kind == "genre":
        if not source_label:
            return
    elif not source_id:
        return

    from jellytoast import async_io
    from jellytoast.providers import get_provider

    def _fetch():
        api = get_provider()
        if seed_kind == "album":
            return api.get_instant_mix(source_id)
        if seed_kind == "artist":
            return api.get_similar_songs(source_id)
        if seed_kind == "genre":
            return api.get_genre_radio(source_label)
        return []

    def _on_result(tracks):
        if not tracks:
            return
        from jellytoast.player_state import PlayerBus, QueueContext, QueueKind

        ctx = QueueContext(
            kind=QueueKind.INSTANT_MIX,
            source_id=source_id,
            source_label=source_label,
            seed_kind=seed_kind,
        )
        PlayerBus.get().queue_play_now.emit(list(tracks), 0, ctx)

    async_io.run_async(_fetch, on_result=_on_result)


# ── "Create smart playlist from this X" entry point ──────────────────────


def open_create_smart_playlist(
    parent: QWidget,
    kind: str,
    name: str,
    item: "Optional[dict]" = None,
) -> None:
    """Right-click *Create smart playlist from this <kind>* flow.

    ``kind`` is one of ``"artist"`` / ``"album"`` / ``"genre"`` /
    ``"track"``. Builds a schema-valid rules dict via the matching
    ``jellytoast.smart_playlists.presets`` ``from_*`` factory, opens the
    smart-playlist editor pre-populated (rules + a suggested name),
    and on save appends the new entry to ``settings.smart_playlists``
    so it shows up on the Smart Playlists tab.

    ``item`` (optional) is the full item dict for the seeded entity —
    the album/track factories use it to extract Genres + ProductionYear
    for the era-vibe recipes. Passing only ``name`` still works (the
    factories degrade gracefully); pass ``item`` whenever the caller
    already has it for richer rule seeding.

    Naming follows the Spotify/Plexamp short-suffix idiom — "More like
    X", "Deep Cuts: X", "X Discoveries" — to read well in the
    Playlists list typography.

    Non-blocking — opens the editor with a save callback rather than
    waiting on the dialog.
    """
    if not name:
        return
    from jellytoast.smart_playlist_editor import open_smart_playlist_editor
    from jellytoast.smart_playlists import presets as _presets

    hint: "Optional[str]" = None
    if kind == "artist":
        rules = _presets.from_artist(name)
        suggested = QCoreApplication.translate("UiHelpers", "Deep Cuts: {0}").format(name)
    elif kind == "album":
        rules = _presets.from_album(item if item is not None else name)
        suggested = QCoreApplication.translate("UiHelpers", "More like {0}").format(name)
        # Surface the missing-metadata case so the user knows WHY the
        # recipe only has a year rule. The album / track recipes both
        # rely on Genres for the "more like" feel — a library without
        # genre tags makes the recipe degrade to era-only.
        if isinstance(item, dict) and not (item.get("Genres") or []):
            hint = QCoreApplication.translate(
                "UiHelpers", "{0} has no genre tags, add some to help suggestions."
            ).format(name)
    elif kind == "genre":
        rules = _presets.from_genre(name)
        suggested = QCoreApplication.translate("UiHelpers", "{0} Discoveries").format(name)
    elif kind == "track":
        rules = _presets.from_track(item if item is not None else name)
        suggested = QCoreApplication.translate("UiHelpers", "More like {0}").format(name)
        if isinstance(item, dict) and not (item.get("Genres") or []):
            hint = QCoreApplication.translate(
                "UiHelpers", "{0} has no genre tags, add some to help suggestions."
            ).format(name)
    else:
        return

    def _persist(entry):
        from jellytoast.settings import get_settings

        entries = list(get_settings().smart_playlists)
        entries.append(entry)
        get_settings().smart_playlists = entries

    def _on_save_and_play(entry, dismiss):
        """Save & Play: persist, then resolve+play. The editor stays
        open in a Loading state until ``dismiss`` is called — pass
        it through to ``play_entry`` as the ``on_complete`` hook so
        the dialog closes the moment playback actually starts (or
        empty / error feedback lands)."""
        from jellytoast.smart_playlists.play import play_entry

        _persist(entry)
        play_entry(entry, parent, on_complete=dismiss)

    open_smart_playlist_editor(
        parent,
        preset_rules=rules,
        suggested_name=suggested,
        hint=hint,
        on_save=_persist,
        on_save_and_play=_on_save_and_play,
    )


# ── Auto-fade scroll bar ─────────────────────────────────────────────────


class AutoFadeScrollBar(QScrollBar):
    """A scroll bar that renders ONLY the pill — no track, no lane, no
    background of any kind. Bypasses Qt's native style (which would
    otherwise paint a track lane even when QSS sets the bar's
    background transparent) by overriding paintEvent and drawing the
    handle directly.

    The pill fades to invisible after a short idle period. Any scroll
    movement (wheel, drag, programmatic value change, or mouse hover
    over the bar itself) wakes it back to full opacity. The fade is
    driven by a QPropertyAnimation on the custom handleAlpha property,
    not a QGraphicsOpacityEffect — the effect approach left a faint
    rendered backdrop visible against translucent body colors."""

    IDLE_MS = 900  # how long the pill stays visible after the last interaction
    FADE_MS = 220  # cross-fade duration
    PILL_ALPHA = 110  # peak alpha of the handle (0-255); ~0.43
    PILL_RADIUS = 3
    PILL_INSET = 2  # px shrink applied to the handle rect for breathing room

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._handle_alpha = 0
        self._hovered = False
        # Translucent + no system background → Qt won't paint anything
        # behind the widget; combined with our paintEvent skipping
        # everything except the handle, the lane is truly invisible.
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setAutoFillBackground(False)
        # Strip any application-level QSS that would otherwise reach
        # this widget. We paint everything manually.
        self.setStyleSheet("QScrollBar { background: transparent; border: none; }")

        self._anim = QPropertyAnimation(self, b"handleAlpha", self)
        self._anim.setDuration(self.FADE_MS)

        self._idle_timer = QTimer(self)
        self._idle_timer.setSingleShot(True)
        self._idle_timer.timeout.connect(self._fade_out)

        self.valueChanged.connect(self._wake)

    # ── Custom property used for the fade animation ────────────────────

    def get_handle_alpha(self) -> int:
        return self._handle_alpha

    def set_handle_alpha(self, alpha: int):
        alpha = max(0, min(255, int(alpha)))
        if alpha != self._handle_alpha:
            self._handle_alpha = alpha
            self.update()  # repaint with new alpha

    handleAlpha = Property(int, get_handle_alpha, set_handle_alpha)

    # ── Paint just the pill ────────────────────────────────────────────

    def paintEvent(self, _event):
        if self._handle_alpha <= 0:
            return  # nothing to draw
        # Look up the handle rect from the style — accounts for the
        # current scroll position + range automatically.
        opt = QStyleOptionSlider()
        self.initStyleOption(opt)
        handle = self.style().subControlRect(
            QStyle.ComplexControl.CC_ScrollBar,
            opt,
            QStyle.SubControl.SC_ScrollBarSlider,
            self,
        )
        # Inset on the long axis so the pill has a tiny breath of
        # space at each end of its slot — reads as a floating element
        # rather than something flush to invisible bounds.
        if self.orientation() == Qt.Orientation.Vertical:
            handle.adjust(0, self.PILL_INSET, 0, -self.PILL_INSET)
        else:
            handle.adjust(self.PILL_INSET, 0, -self.PILL_INSET, 0)

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        # Brighter on hover so the pill answers cursor presence.
        peak = 180 if self._hovered else self.PILL_ALPHA
        # Scale alpha down by the current handleAlpha fraction.
        alpha = int(peak * (self._handle_alpha / 255))
        # Theme ink so the handle reads on a light theme too.
        _hr, _hg, _hb = _hex_to_rgb_safe(TEXT)
        painter.setBrush(QColor(_hr, _hg, _hb, alpha))
        painter.drawRoundedRect(handle, self.PILL_RADIUS, self.PILL_RADIUS)

    def _wake(self, *_):
        self._anim.stop()
        self._anim.setStartValue(self._handle_alpha)
        self._anim.setEndValue(255)
        self._anim.start()
        self._idle_timer.start(self.IDLE_MS)

    def _fade_out(self):
        self._anim.stop()
        self._anim.setStartValue(self._handle_alpha)
        self._anim.setEndValue(0)
        self._anim.start()

    def enterEvent(self, event):
        self._hovered = True
        self._wake()
        self._idle_timer.stop()
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hovered = False
        self._idle_timer.start(self.IDLE_MS)
        self.update()
        super().leaveEvent(event)


def install_autofade_scrollbars(scroll_area: QScrollArea):
    """Replace the QScrollArea's default scroll bars with auto-fading
    versions. The bar widgets paint nothing but their own pill — track,
    lane, and page backgrounds are skipped entirely so only the handle
    renders against the body."""
    v = AutoFadeScrollBar(Qt.Orientation.Vertical, scroll_area)
    h = AutoFadeScrollBar(Qt.Orientation.Horizontal, scroll_area)
    scroll_area.setVerticalScrollBar(v)
    scroll_area.setHorizontalScrollBar(h)
    # Under QStyleSheetStyle a QScrollArea paints an OPAQUE background
    # (pure black in every theme — it reads the unthemed app palette) in
    # the scrollbar gutter beneath our transparent bars: an 8px solid
    # strip over the frost/body on any page whose content overflows.
    # Descendant rules on the host view (e.g. "QWidget#x QScrollArea")
    # do NOT cure it — the QSS must sit on the widget itself. Appended
    # so a caller's own stylesheet survives. The selector only matches
    # QScrollArea proper; QListView callers (QAbstractScrollArea branch)
    # are unaffected and don't exhibit the bug.
    if isinstance(scroll_area, QScrollArea):
        existing = scroll_area.styleSheet()
        scroll_area.setStyleSheet(
            (existing + "\n" if existing else "")
            + "QScrollArea { background: transparent; border: none; }"
        )
    return v, h
