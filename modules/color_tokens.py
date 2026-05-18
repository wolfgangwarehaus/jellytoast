"""Central color-token registry + override mechanism.

The source of truth for every named color used in the UI. Each token's
*value* still lives on its original module (``modules.ui_helpers``,
``modules.design_tokens``, ``modules.icons``); this module owns the
*metadata* (display name, category, default, kind) and the *override
storage* (QSettings under ``debug/colors/<TOKEN_NAME>``).

The override flow:

1. App startup calls ``load_persisted_overrides()`` once; any saved
   override is applied to its module's global in place.
2. The Settings → Colors page reads tokens via ``get_current()`` /
   ``get_default()`` and shows H/S/V/A sliders + a swatch.
3. Slider changes call ``apply_override(name, value)`` which:
   - Persists the override to QSettings.
   - Mutates the module-level global in place (same pattern as
     ``ui_helpers.refresh_theme()``).
   - Fires ``PlayerBus.theme_changed`` so every widget that listens
     re-stamps its QSS / re-paints.
4. ``reset(name)`` removes the override and restores the default.

Pattern memory: callers should access tokens as ``module.TOKEN`` so
the live value is read on each access — ``from module import TOKEN``
captures the value at import time and won't see overrides applied
later. The accent-picker convention documented in
``ui_helpers.refresh_theme`` already follows this rule; the same
rule applies to every other token.
"""

from __future__ import annotations

import importlib
import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ColorToken:
    """Metadata describing one named UI color.

    ``kind`` drives the editor UI shape:

    - ``"hex"`` — a ``#rrggbb`` string. Edited with H/S/V sliders.
    - ``"rgba"`` — a ``"rgba(r,g,b,a)"`` string. Edited with H/S/V + opacity.
    - ``"tuple_rgba"`` — a ``(r, g, b, a)`` int tuple (for
      ``QColor(*tuple)`` in paintEvent). Same UI as rgba.

    ``module`` is the dotted module path where the live value lives.
    ``apply_override`` mutates ``module.<name>`` in place.
    """

    name: str
    default: Any
    kind: str  # "hex" | "rgba" | "tuple_rgba"
    category: str  # "accent" | "text" | "surface" | "highlight" | "input" | "slider" | "destructive"
    description: str
    module: str


# ── Token registry ─────────────────────────────────────────────────────────
#
# Defaults match the values currently set in ui_helpers.py,
# design_tokens.py, and icons.py. Categories drive the section
# grouping on the Settings → Colors page.

TOKENS: dict[str, ColorToken] = {
    # ── Accent ─────────────────────────────────────────────────────────
    "ACCENT": ColorToken(
        name="ACCENT",
        default="#967de1",
        kind="hex",
        category="accent",
        description="Primary accent. Active controls, fills, focus rings.",
        module="modules.ui_helpers",
    ),
    "ACCENT_DEEP": ColorToken(
        name="ACCENT_DEEP",
        default="#7c66d0",
        kind="hex",
        category="accent",
        description="Pressed / active accent (≈10% darker than ACCENT).",
        module="modules.ui_helpers",
    ),
    "BORDER_ACCENT": ColorToken(
        name="BORDER_ACCENT",
        default="rgba(150,125,225,0.35)",
        kind="rgba",
        category="accent",
        description="Focused input borders, accent-tinted dividers.",
        module="modules.ui_helpers",
    ),
    # ── Text ───────────────────────────────────────────────────────────
    "TEXT": ColorToken(
        name="TEXT",
        default="#ffffff",
        kind="hex",
        category="text",
        description="Primary foreground text.",
        module="modules.ui_helpers",
    ),
    "TEXT_DIM": ColorToken(
        name="TEXT_DIM",
        default="rgba(255,255,255,0.7)",
        kind="rgba",
        category="text",
        description="Secondary text (artist names, subtitles).",
        module="modules.ui_helpers",
    ),
    "TEXT_FAINT": ColorToken(
        name="TEXT_FAINT",
        default="rgba(255,255,255,0.4)",
        kind="rgba",
        category="text",
        description="Kickers, disabled text, hairline labels.",
        module="modules.ui_helpers",
    ),
    "IDLE_TEXT": ColorToken(
        name="IDLE_TEXT",
        default="#a8a8a8",
        kind="hex",
        category="text",
        description="\"Nothing playing\" / empty-state labels.",
        module="modules.ui_helpers",
    ),
    "ERROR_FG": ColorToken(
        name="ERROR_FG",
        default="#f87171",
        kind="hex",
        category="text",
        description="Inline error text (login failure, etc).",
        module="modules.ui_helpers",
    ),
    "WARN_FG": ColorToken(
        name="WARN_FG",
        default="#e0735c",
        kind="hex",
        category="text",
        description="Warning marker (offline indicator).",
        module="modules.ui_helpers",
    ),
    # ── Surfaces ───────────────────────────────────────────────────────
    "BG": ColorToken(
        name="BG",
        default="#101010",
        kind="hex",
        category="surface",
        description="Global window background.",
        module="modules.ui_helpers",
    ),
    "BG_PANEL": ColorToken(
        name="BG_PANEL",
        default="#1a1a1a",
        kind="hex",
        category="surface",
        description="Menu / combo popup fill.",
        module="modules.ui_helpers",
    ),
    "BG_CARD": ColorToken(
        name="BG_CARD",
        default="rgba(255,255,255,0.04)",
        kind="rgba",
        category="surface",
        description="Card surfaces (rare).",
        module="modules.ui_helpers",
    ),
    "BODY_COLOR": ColorToken(
        name="BODY_COLOR",
        default=(18, 18, 18, 232),
        kind="tuple_rgba",
        category="surface",
        description="Main window body fill (painted via QPainter).",
        module="modules.ui_helpers",
    ),
    "MINI_BODY_COLOR": ColorToken(
        name="MINI_BODY_COLOR",
        default=(22, 22, 22, 232),
        kind="tuple_rgba",
        category="surface",
        description="Mini player body fill.",
        module="modules.ui_helpers",
    ),
    "DIALOG_BODY_COLOR": ColorToken(
        name="DIALOG_BODY_COLOR",
        default=(12, 12, 12, 252),
        kind="tuple_rgba",
        category="surface",
        description="Settings / cast dialog body fill.",
        module="modules.ui_helpers",
    ),
    "POPUP_OPAQUE_FILL": ColorToken(
        name="POPUP_OPAQUE_FILL",
        default="rgba(20,22,26,1.0)",
        kind="rgba",
        category="surface",
        description="Opaque popup body (cast/sort menus, combos in Wayland).",
        module="modules.ui_helpers",
    ),
    # ── Highlights / washes ────────────────────────────────────────────
    "WASH_HOVER": ColorToken(
        name="WASH_HOVER",
        default="rgba(58, 60, 68, 0.92)",
        kind="rgba",
        category="highlight",
        description="Icon-button hover, volume popup body.",
        module="modules.ui_helpers",
    ),
    "WASH_PRESSED": ColorToken(
        name="WASH_PRESSED",
        default="rgba(72, 74, 82, 0.92)",
        kind="rgba",
        category="highlight",
        description="Icon-button pressed state.",
        module="modules.ui_helpers",
    ),
    "HOVER_SUBTLE": ColorToken(
        name="HOVER_SUBTLE",
        default="rgba(255,255,255,0.06)",
        kind="rgba",
        category="highlight",
        description="Ghost-button hover, library tile hover.",
        module="modules.ui_helpers",
    ),
    "HOVER_LIST_ROW": ColorToken(
        name="HOVER_LIST_ROW",
        default="rgba(255,255,255,0.04)",
        kind="rgba",
        category="highlight",
        description="List row hover (cast dialog, settings sidebar).",
        module="modules.ui_helpers",
    ),
    "SELECTED_ROW": ColorToken(
        name="SELECTED_ROW",
        default="rgba(255,255,255,0.10)",
        kind="rgba",
        category="highlight",
        description="Selected list row (non-accent variant).",
        module="modules.ui_helpers",
    ),
    "PRESSED_WHITE": ColorToken(
        name="PRESSED_WHITE",
        default="rgba(255,255,255,0.12)",
        kind="rgba",
        category="highlight",
        description="White-press button state (lighter than WASH_PRESSED).",
        module="modules.ui_helpers",
    ),
    "OVERLAY_DARK": ColorToken(
        name="OVERLAY_DARK",
        default="rgba(0,0,0,0.65)",
        kind="rgba",
        category="highlight",
        description="Translucent dark overlay (cover-art heart bg).",
        module="modules.ui_helpers",
    ),
    "OVERLAY_DARK_HOVER": ColorToken(
        name="OVERLAY_DARK_HOVER",
        default="rgba(0,0,0,0.85)",
        kind="rgba",
        category="highlight",
        description="Translucent dark overlay on hover.",
        module="modules.ui_helpers",
    ),
    # ── Inputs ─────────────────────────────────────────────────────────
    "BORDER": ColorToken(
        name="BORDER",
        default="rgba(255,255,255,0.08)",
        kind="rgba",
        category="input",
        description="Input borders, separators.",
        module="modules.ui_helpers",
    ),
    "SURFACE_INPUT": ColorToken(
        name="SURFACE_INPUT",
        default="rgba(255,255,255,0.05)",
        kind="rgba",
        category="input",
        description="QLineEdit / QComboBox / QSpinBox fill.",
        module="modules.ui_helpers",
    ),
    "SURFACE_INPUT_FOCUS": ColorToken(
        name="SURFACE_INPUT_FOCUS",
        default="rgba(255,255,255,0.07)",
        kind="rgba",
        category="input",
        description="Input :focus background tint.",
        module="modules.ui_helpers",
    ),
    "DISABLED_FG": ColorToken(
        name="DISABLED_FG",
        default="rgba(255,255,255,0.30)",
        kind="rgba",
        category="input",
        description="Disabled foreground (icon-button color, placeholders).",
        module="modules.ui_helpers",
    ),
    # ── Destructive ────────────────────────────────────────────────────
    "DANGER": ColorToken(
        name="DANGER",
        default="#ef4444",
        kind="hex",
        category="destructive",
        description="Destructive action accent (e.g. sign out, delete).",
        module="modules.design_tokens",
    ),
    "DANGER_DEEP": ColorToken(
        name="DANGER_DEEP",
        default="#b91c1c",
        kind="hex",
        category="destructive",
        description="Destructive pressed state.",
        module="modules.design_tokens",
    ),
    # ── Sliders ────────────────────────────────────────────────────────
    "SLIDER_GROOVE": ColorToken(
        name="SLIDER_GROOVE",
        default="rgba(255,255,255,0.20)",
        kind="rgba",
        category="slider",
        description="Slider track fill (volume / seek / EQ).",
        module="modules.ui_helpers",
    ),
    # Icon colors (ICON_DIM / ICON_BRIGHT in icons.py) and the slider
    # handle pill are intentionally NOT registered as first-class
    # tokens — they mirror IDLE_TEXT and TEXT respectively. Editing
    # those tokens flows through to icons (after a refresh_theme) and
    # to slider handles (slider handle is currently hardcoded #ffffff
    # everywhere; Phase 4 will refactor to read from TEXT).
}


# Pretty-print names for section headers in the Settings UI.
CATEGORY_LABELS: dict[str, str] = {
    "accent": "Accent",
    "text": "Text",
    "surface": "Surfaces",
    "highlight": "Highlights & washes",
    "input": "Inputs & borders",
    "slider": "Sliders",
    "destructive": "Destructive",
}


# ── Public API ─────────────────────────────────────────────────────────────


def get_default(name: str) -> Any:
    """Return the shipped default value for ``name``."""
    return TOKENS[name].default


def get_current(name: str) -> Any:
    """Read the live value from the token's module (reflects any
    applied override)."""
    token = TOKENS[name]
    module = importlib.import_module(token.module)
    return getattr(module, name)


def apply_override(name: str, value: Any, *, persist: bool = True) -> None:
    """Apply an override: mutate the module global in place, fire
    ``PlayerBus.theme_changed``, and (by default) persist to
    QSettings."""
    token = TOKENS[name]
    # 1. Mutate the live value on the owning module.
    module = importlib.import_module(token.module)
    setattr(module, name, value)
    # 2. Persist if requested.
    if persist:
        from PySide6.QtCore import QSettings

        QSettings().setValue(_qs_key(name), _serialize(value, token.kind))
    # 3. Cascade re-apply across all listening widgets.
    _emit_theme_changed()


def reset(name: str) -> None:
    """Remove the override; restore the shipped default."""
    from PySide6.QtCore import QSettings

    token = TOKENS[name]
    QSettings().remove(_qs_key(name))
    module = importlib.import_module(token.module)
    setattr(module, name, token.default)
    _emit_theme_changed()


def reset_all() -> None:
    """Wipe every override; restore every default."""
    from PySide6.QtCore import QSettings

    s = QSettings()
    for name, token in TOKENS.items():
        s.remove(_qs_key(name))
        module = importlib.import_module(token.module)
        setattr(module, name, token.default)
    _emit_theme_changed()


def load_persisted_overrides() -> None:
    """Apply every override saved in QSettings. Called once at app
    startup (before any widget is constructed, so its first stylesheet
    sees the overridden values)."""
    from PySide6.QtCore import QSettings

    s = QSettings()
    for name, token in TOKENS.items():
        key = _qs_key(name)
        if not s.contains(key):
            continue
        raw = s.value(key, type=str)
        if not raw:
            continue
        try:
            value = _deserialize(raw, token.kind)
        except Exception:
            continue
        module = importlib.import_module(token.module)
        setattr(module, name, value)


def tokens_by_category() -> dict[str, list[ColorToken]]:
    """Return tokens grouped by category in CATEGORY_LABELS order."""
    out: dict[str, list[ColorToken]] = {cat: [] for cat in CATEGORY_LABELS}
    for token in TOKENS.values():
        out.setdefault(token.category, []).append(token)
    # Drop empty categories.
    return {cat: ts for cat, ts in out.items() if ts}


# ── Palette export / import ────────────────────────────────────────────────


PALETTE_VERSION = 1


def export_palette(name: str = "") -> dict:
    """Build a palette dict containing the CURRENT value of every
    token (whether the value matches the default or has been
    overridden). Suitable for ``json.dumps`` to disk."""
    return {
        "version": PALETTE_VERSION,
        "name": name,
        "tokens": {n: get_current(n) for n in TOKENS},
    }


def import_palette(palette: dict) -> int:
    """Apply every recognised token from ``palette`` as an override.
    Returns the number of tokens applied. Unknown token names are
    silently skipped (forward-compat with palettes from a future
    schema). Tokens missing from the palette are left at their
    current value (not reset)."""
    if not isinstance(palette, dict):
        raise ValueError("palette must be a dict")
    tokens = palette.get("tokens")
    if not isinstance(tokens, dict):
        raise ValueError("palette['tokens'] must be a dict")
    applied = 0
    for name, value in tokens.items():
        if name not in TOKENS:
            continue
        token = TOKENS[name]
        # JSON serialises tuples as lists — convert back.
        if token.kind == "tuple_rgba" and isinstance(value, list):
            value = tuple(value)
        try:
            apply_override(name, value)
            applied += 1
        except Exception:
            continue
    return applied


# ── Named palette library ──────────────────────────────────────────────────
#
# User-saved presets stored under QSettings ``debug/color_palettes/<name>``
# as JSON-encoded palette dicts. Independent from the per-token override
# store (``debug/colors/<TOKEN>``); saving a palette doesn't touch
# overrides, loading a palette writes overrides via ``import_palette``.


def _palette_qs_key(name: str) -> str:
    return f"debug/color_palettes/{name}"


def save_palette(name: str) -> None:
    """Snapshot the current token values into a named palette.
    Overwrites silently if ``name`` exists. Caller validates the name
    (non-empty, no slashes that would split the QSettings path)."""
    from PySide6.QtCore import QSettings
    import json as _json

    palette = export_palette(name=name)
    QSettings().setValue(_palette_qs_key(name), _json.dumps(palette))


def load_palette(name: str) -> int:
    """Apply a saved palette by name. Returns the count of applied
    tokens; raises KeyError if the palette doesn't exist."""
    from PySide6.QtCore import QSettings
    import json as _json

    s = QSettings()
    key = _palette_qs_key(name)
    if not s.contains(key):
        raise KeyError(name)
    palette = _json.loads(s.value(key, type=str))
    return import_palette(palette)


def list_palettes() -> list[str]:
    """Return the names of all saved palettes, sorted."""
    from PySide6.QtCore import QSettings

    s = QSettings()
    prefix = "debug/color_palettes/"
    return sorted(
        k[len(prefix):] for k in s.allKeys() if k.startswith(prefix)
    )


def delete_palette(name: str) -> None:
    """Remove a saved palette. No-op if it doesn't exist."""
    from PySide6.QtCore import QSettings

    QSettings().remove(_palette_qs_key(name))


# ── Internals ──────────────────────────────────────────────────────────────


def _qs_key(name: str) -> str:
    return f"debug/colors/{name}"


def _serialize(value: Any, kind: str) -> str:
    """JSON-serialize for QSettings storage. Tuples → JSON arrays."""
    if kind == "tuple_rgba" and isinstance(value, tuple):
        return json.dumps(list(value))
    return json.dumps(value)


def _deserialize(raw: str, kind: str) -> Any:
    """Inverse of _serialize. Lists → tuples for tuple_rgba."""
    parsed = json.loads(raw)
    if kind == "tuple_rgba" and isinstance(parsed, list):
        return tuple(parsed)
    return parsed


def _emit_theme_changed() -> None:
    """Fire theme_changed so every subscriber re-stamps its styles.
    Wrapped because PlayerBus may not be initialised in early-boot
    contexts (token overrides loaded before QApplication)."""
    try:
        from modules.player_state import PlayerBus

        PlayerBus.get().theme_changed.emit()
    except Exception:
        # No bus yet (app booting): persisted overrides apply via
        # load_persisted_overrides() mutating the globals directly,
        # so the first widget construction reads the override.
        pass
