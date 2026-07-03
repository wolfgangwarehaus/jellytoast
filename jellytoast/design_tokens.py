"""
jellytoast design tokens.

A frozen vocabulary for typography, spacing, radii, and button sizing —
the geometry side of the design system. Color lives in `theme.py`; this
module owns everything else widgets need to look consistent.

Two registries do most of the work:
  TYPE   — 7 typography tiers (display → micro)
  BUTTON — 5 button tiers (primary, secondary, ghost, icon, destructive)

Helpers convert tiers into Qt primitives:
  font(tier)        → QFont
  type_qss(tier)    → "font-size: 13px; font-weight: 400;" fragment
  button_qss(tier)  → full QPushButton stylesheet pulling colors from
                      the active theme

Why a registry of dataclasses rather than free constants: lets callers
look up by string ("body", "primary") when iterating, and lets future
themes override sizing without touching every call site.
"""

from dataclasses import dataclass

from PySide6.QtGui import QFont

# ── Global font-scale multiplier ───────────────────────────────────────────
# Settings → Display → Font size writes "small" / "default" / "large" /
# "largest" into `ui/font_scale`. We read it once at module import and
# multiply every typography + button tier's pixel size by the mapped
# factor. Restart required for changes to take effect because the
# scaled sizes are baked into class-level constants and into the QSS
# fragments emitted by `type_qss()` — those strings are splattered
# across the codebase at construction time, not at paint time.

_FONT_SCALE_MAP = {
    "small": 0.9,
    "default": 1.0,
    "large": 1.1,
    "largest": 1.25,
}


def _load_font_scale() -> float:
    """Read ``ui/font_scale`` from QSettings without requiring a
    QApplication. ``settings_migration.open_qsettings()`` works
    standalone (org/app names are supplied explicitly) and is the same
    handle `jellytoast.settings` uses. Falls back to
    1.0 on any error so this never breaks the import."""
    try:
        from jellytoast.settings_migration import open_qsettings

        s = open_qsettings()
        key = s.value("ui/font_scale", "default", type=str)
        return _FONT_SCALE_MAP.get(key, 1.0)
    except Exception:
        return 1.0


FONT_SCALE: float = _load_font_scale()


def _fs(px: int) -> int:
    """Scale a pixel size by the active font-scale factor. Clamped at
    a minimum of 1 px so a sub-1.0 scale on an already-small tier
    (11 px × 0.9 = 9.9 → 10) doesn't accidentally vanish."""
    return max(1, int(round(px * FONT_SCALE)))


# ── Typography ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class TypeTier:
    name: str
    size_px: int
    # Qt's QFont.Weight values — kept as plain ints so this module stays
    # importable without pulling Qt in for callers that just want tokens.
    # 400=normal, 500=medium, 600=demibold, 700=bold.
    weight: int
    # Letter-spacing as a fraction of the em (e.g. 0.12 == 12%). Used
    # almost exclusively by the MICRO tier for ALL-CAPS kicker labels.
    letter_spacing_em: float = 0.0
    uppercase: bool = False


def _make_type_tokens() -> "tuple[TypeTier, ...]":
    """Build the typography tiers from their raw base sizes through ``_fs()``,
    so a font-scale change can rebuild them at runtime (see ``refresh_fonts``).
    Order matches the unpacking below. TINY is 11px non-uppercase tertiary text
    (time codes, mini-player subtitles); MICRO is 11px ALL-CAPS kicker labels."""
    return (
        TypeTier("display", size_px=_fs(22), weight=700),
        TypeTier("title", size_px=_fs(18), weight=600),
        TypeTier("heading", size_px=_fs(16), weight=600),
        TypeTier("subhead", size_px=_fs(14), weight=600),
        TypeTier("body", size_px=_fs(13), weight=400),
        TypeTier("caption", size_px=_fs(12), weight=400),
        TypeTier("tiny", size_px=_fs(11), weight=400),
        TypeTier("micro", size_px=_fs(11), weight=700, letter_spacing_em=0.12, uppercase=True),
    )


# Built from one tuple so ``TYPE[...]`` holds the SAME tier objects as the
# module-level ``TYPE_*`` names (a live rebuild must keep them in lockstep).
_type_tokens = _make_type_tokens()
(
    TYPE_DISPLAY,
    TYPE_TITLE,
    TYPE_HEADING,
    TYPE_SUBHEAD,
    TYPE_BODY,
    TYPE_CAPTION,
    TYPE_TINY,
    TYPE_MICRO,
) = _type_tokens

TYPE: dict[str, TypeTier] = {t.name: t for t in _type_tokens}


def font(tier: TypeTier) -> QFont:
    """Build a QFont from a TypeTier. Use when a widget consumes QFont
    directly (e.g. QLabel.setFont) rather than QSS.

    Sizing uses ``setPixelSize`` (not ``setPointSize``) on purpose:
    Qt 6 scales pixel sizes by the screen's device-pixel ratio
    automatically, so the visual rhythm of the type ramp stays
    consistent across 1×/1.5×/2×/3× displays. The tradeoff is that
    OS-level "Large Text" / accessibility text-scale preferences
    (KDE font DPI override, Windows "Make text bigger") are ignored —
    the music-player aesthetic stays pinned regardless of system
    preference. If we ever want to honour those prefs, swap to
    ``setPointSize`` here and the rest of the codebase follows.
    """
    f = QFont()
    f.setPixelSize(tier.size_px)
    f.setWeight(QFont.Weight(tier.weight))
    if tier.letter_spacing_em:
        # Qt's PercentageSpacing is "100% == normal", so 112 == +12% em.
        f.setLetterSpacing(
            QFont.SpacingType.PercentageSpacing,
            100 + tier.letter_spacing_em * 100,
        )
    if tier.uppercase:
        f.setCapitalization(QFont.Capitalization.AllUppercase)
    return f


def type_qss(tier: TypeTier) -> str:
    """Return a QSS fragment (no selector, no braces) — splice into an
    existing rule, e.g. `f"color: {TEXT}; {type_qss(TYPE_BODY)}"`.

    Only emits Qt-stylesheet-supported properties (font-size,
    font-weight). Letter-spacing and text-transform aren't in Qt's
    QSS subset — Qt would silently fail to parse and log a "Could
    not parse stylesheet" warning. Apply those via the QFont path:
    call ``apply_type(label, tier)`` alongside ``setStyleSheet`` so
    the font gets the full tier including spacing + uppercase
    capitalization."""
    return f"font-size: {tier.size_px}px; font-weight: {tier.weight};"


def apply_type(widget, tier: TypeTier) -> None:
    """Apply the tier's full font (size, weight, letter spacing,
    uppercase capitalization) via QWidget.setFont. Pair with
    ``type_qss`` when a label needs spacing / uppercase: stylesheets
    handle color and sizing, this handles the QFont attributes Qt's
    QSS parser rejects."""
    widget.setFont(font(tier))


# ── Spacing (4-based scale) ─────────────────────────────────────────────────
# Used for layout margins, spacing between widgets, padding inside frames.
# Stick to these values rather than ad-hoc px so vertical rhythm holds
# across views.

SPACE_XS = 4
SPACE_SM = 8
SPACE_MD = 12
SPACE_LG = 16
SPACE_XL = 24
SPACE_XXL = 32


# ── Radii ───────────────────────────────────────────────────────────────────


def _load_square_corners() -> bool:
    """Read ``ui/square_corners`` from QSettings without requiring a
    QApplication — same standalone handle as ``_load_font_scale``. Baked
    into the radius tokens below at import, so (like font_scale) the
    "Square corners" setting takes effect on the next launch."""
    try:
        from jellytoast.settings_migration import open_qsettings

        return bool(
            open_qsettings().value(
                "ui/square_corners", False, type=bool
            )
        )
    except Exception:
        return False


_SQUARE_CORNERS: bool = _load_square_corners()


def rad(px: int) -> int:
    """Resolve a corner radius honoring the "Square corners" setting: 0 when
    square is on, else ``px`` unchanged. The pill/circle sentinel (>= 1000,
    e.g. ``RADIUS_PILL``) passes through untouched, so genuinely circular or
    pill controls — round icon buttons, the slider handle, avatars — never
    collapse to a sharp square. The ``RADIUS_*`` tokens are pre-resolved
    through this; call ``rad()`` directly only for literal radii at sites
    that don't go through a token."""
    if _SQUARE_CORNERS and px < 1000:
        return 0
    return px


def set_square_corners(on: bool) -> None:
    """Flip the in-memory square-corners flag. The radius tokens are baked at
    import, so this alone does NOT re-square already-built surfaces — the
    Settings setter persists the QSetting and the change takes full effect on
    the next launch (the Settings UI shows the restart notice). Exposed mainly
    for tests."""
    global _SQUARE_CORNERS
    _SQUARE_CORNERS = bool(on)


# Every finite radius flows through rad() so "Square corners" zeros them all
# from one place. RADIUS_PILL is the circular/pill sentinel — left as-is, and
# rad() passes anything >= 1000 through, so round controls stay round.
RADIUS_SM = rad(4)
RADIUS_MD = rad(6)
RADIUS_LG = rad(8)
RADIUS_XL = rad(12)
RADIUS_PILL = 9999

# Host-OS window-corner radius. jellytoast's frameless surfaces (mini
# player, settings dialog) are KWin `noborder` windows — KWin draws no
# decoration, so the app paints its own corners. Matching this to the
# native window-corner radius keeps them uniform with the rest of the
# desktop. KDE Breeze ≈ 8px (measured 2026-05-21). Per-OS values slot
# in here when the Windows / macOS backends arrive. Flows through rad() so
# square corners squares the window chrome AND the blur shape (both derive
# from this one token) together.
RADIUS_WINDOW = rad(8)


# ── Buttons ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ButtonTier:
    name: str
    height_px: int  # nominal min-height; combined with vertical padding
    pad_x: int
    pad_y: int
    radius: int
    type_tier: TypeTier


# Button geometry scales with the font multiplier so text-anchored
# buttons grow/shrink with their labels. Radii stay token-fixed —
# corner curvature is a constant of the design system.
def _make_button_tokens(body: TypeTier, caption: TypeTier) -> "tuple[ButtonTier, ...]":
    """Build the button tiers from raw base geometry through ``_fs()`` (so they
    rebuild on a font-scale change) against the CURRENT type + radius tokens."""
    return (
        ButtonTier("primary", height_px=_fs(36), pad_x=_fs(14), pad_y=_fs(8),
                   radius=RADIUS_LG, type_tier=body),
        ButtonTier("secondary", height_px=_fs(36), pad_x=_fs(14), pad_y=_fs(8),
                   radius=RADIUS_LG, type_tier=body),
        ButtonTier("ghost", height_px=_fs(32), pad_x=_fs(12), pad_y=_fs(6),
                   radius=RADIUS_MD, type_tier=body),
        ButtonTier("icon", height_px=_fs(32), pad_x=_fs(8), pad_y=_fs(8),
                   radius=RADIUS_PILL, type_tier=caption),
        ButtonTier("destructive", height_px=_fs(36), pad_x=_fs(14), pad_y=_fs(8),
                   radius=RADIUS_LG, type_tier=body),
    )


_btn_tokens = _make_button_tokens(TYPE_BODY, TYPE_CAPTION)
(BTN_PRIMARY, BTN_SECONDARY, BTN_GHOST, BTN_ICON, BTN_DESTRUCTIVE) = _btn_tokens

BUTTON: dict[str, ButtonTier] = {b.name: b for b in _btn_tokens}


def refresh_fonts() -> None:
    """Recompute ``FONT_SCALE`` from settings and REBUILD every size-derived
    token (``TYPE_*`` + the ``TYPE`` dict, ``BTN_*`` + ``BUTTON``) so a
    font-scale change applies WITHOUT a restart. The tiers are frozen
    dataclasses, so this reassigns the module globals; pair it with
    ``ui_helpers._propagate_font_constants()`` to reach the modules that
    imported these names by value, then emit ``theme_changed`` so each surface
    re-runs ``type_qss`` / ``_build_fonts`` and picks up the new sizes."""
    global FONT_SCALE
    global TYPE_DISPLAY, TYPE_TITLE, TYPE_HEADING, TYPE_SUBHEAD
    global TYPE_BODY, TYPE_CAPTION, TYPE_TINY, TYPE_MICRO, TYPE
    global BTN_PRIMARY, BTN_SECONDARY, BTN_GHOST, BTN_ICON, BTN_DESTRUCTIVE, BUTTON
    FONT_SCALE = _load_font_scale()
    tt = _make_type_tokens()
    (
        TYPE_DISPLAY,
        TYPE_TITLE,
        TYPE_HEADING,
        TYPE_SUBHEAD,
        TYPE_BODY,
        TYPE_CAPTION,
        TYPE_TINY,
        TYPE_MICRO,
    ) = tt
    TYPE = {t.name: t for t in tt}
    bt = _make_button_tokens(TYPE_BODY, TYPE_CAPTION)
    (BTN_PRIMARY, BTN_SECONDARY, BTN_GHOST, BTN_ICON, BTN_DESTRUCTIVE) = bt
    BUTTON = {b.name: b for b in bt}


# Destructive red — kept here rather than `theme.py` because it should
# stay constant across themes (a sign-out button needs to mean the same
# thing on the dark and frosted-dark palettes).
DANGER = "#ef4444"
DANGER_DEEP = "#b91c1c"


def button_qss(tier: ButtonTier) -> str:
    """
    Render the full QSS block for a button tier, pulling colors from the
    active theme. Apply via `btn.setStyleSheet(button_qss(BTN_PRIMARY))`.

    Imports the theme lazily so this module can be imported during test
    collection without forcing QSettings init.
    """
    from jellytoast.theme import get_active_theme, ink_alpha

    t = get_active_theme()

    type_block = type_qss(tier.type_tier)
    geom = (
        f"border-radius: {tier.radius}px; "
        f"padding: {tier.pad_y}px {tier.pad_x}px; "
        f"min-height: {tier.height_px - 2 * tier.pad_y}px;"
    )

    if tier.name == "primary":
        return f"""
        QPushButton {{
            background: {t.accent_deep}; color: white;
            border: 1px solid {t.accent};
            {geom} {type_block}
        }}
        QPushButton:hover {{ background: {t.accent}; }}
        QPushButton:pressed {{ background: {t.accent_deep}; }}
        QPushButton:disabled {{
            background: {ink_alpha(0.04)}; color: {t.text_faint};
            border-color: {t.border};
        }}
        """

    if tier.name == "secondary":
        return f"""
        QPushButton {{
            background: {ink_alpha(0.05)}; color: {t.text};
            border: 1px solid {t.border};
            {geom} {type_block}
        }}
        QPushButton:hover {{
            background: {ink_alpha(0.08)}; border-color: {t.border_accent};
        }}
        QPushButton:pressed {{ background: {ink_alpha(0.12)}; }}
        QPushButton:disabled {{ color: {t.text_faint}; }}
        """

    if tier.name == "ghost":
        return f"""
        QPushButton {{
            background: transparent; color: {t.text}; border: none;
            {geom} {type_block}
        }}
        QPushButton:hover {{ background: {ink_alpha(0.06)}; }}
        QPushButton:pressed {{ background: {ink_alpha(0.10)}; }}
        QPushButton:disabled {{ color: {t.text_faint}; }}
        """

    if tier.name == "icon":
        # hover/pressed/disabled pull straight from the theme's wash
        # tokens — one cohesive highlight fill across every icon button
        # in the app, shared with ui_helpers' WASH_* re-exports.
        return f"""
        QPushButton {{
            background: transparent; color: {t.text}; border: none;
            {geom} {type_block}
        }}
        QPushButton:hover {{ background: {t.wash_hover}; }}
        QPushButton:pressed {{ background: {t.wash_pressed}; }}
        QPushButton:disabled {{ color: {t.disabled_fg}; }}
        """

    if tier.name == "destructive":
        return f"""
        QPushButton {{
            background: transparent; color: {DANGER};
            border: 1px solid rgba(239,68,68,0.4);
            {geom} {type_block}
        }}
        QPushButton:hover {{ background: {DANGER}; color: white; border-color: {DANGER}; }}
        QPushButton:pressed {{ background: {DANGER_DEEP}; border-color: {DANGER_DEEP}; }}
        """

    raise ValueError(f"unknown button tier: {tier.name!r}")
