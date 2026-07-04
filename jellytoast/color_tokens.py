"""Central color-token registry + override mechanism.

The source of truth for every named color used in the UI. Each token's
*value* still lives on its original module (``jellytoast.ui_helpers``,
``jellytoast.design_tokens``, ``jellytoast.icons``); this module owns the
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
from contextlib import contextmanager
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
# Each token's ``default`` is the value ``reset()`` / ``get_default()`` restore.
# It tracks the FROSTED-DARK (default theme) live value — kept in sync with
# ui_helpers.py / design_tokens.py via ``test_color_tokens`` so it can't
# silently drift again (the previous drift is what made "Reset" write wrong
# colors). Two caveats baked into the values below: the body fills
# (BODY_COLOR / MINI_BODY_COLOR / DIALOG_BODY_COLOR) are blur-state-dependent
# (alpha 172 with blur, ~236 on the opaque fallback) so their default is the
# blur-on glass value; the accent family (ACCENT / ACCENT_DEEP / BORDER_ACCENT)
# is the SHIPPED accent, which the live value diverges from once the user picks
# a custom accent. Categories drive the Settings → Colors page grouping.

TOKENS: dict[str, ColorToken] = {
    # ── Accent ─────────────────────────────────────────────────────────
    "ACCENT": ColorToken(
        name="ACCENT",
        default="#967de1",
        kind="hex",
        category="accent",
        description="Primary accent. Active controls, fills, focus rings.",
        module="jellytoast.ui_helpers",
    ),
    "ACCENT_DEEP": ColorToken(
        name="ACCENT_DEEP",
        default="#7c66d0",
        kind="hex",
        category="accent",
        description="Pressed / active accent (≈10% darker than ACCENT).",
        module="jellytoast.ui_helpers",
    ),
    "BORDER_ACCENT": ColorToken(
        name="BORDER_ACCENT",
        default="rgba(150,125,225,0.35)",
        kind="rgba",
        category="accent",
        description="Focused input borders, accent-tinted dividers.",
        module="jellytoast.ui_helpers",
    ),
    # ── Text ───────────────────────────────────────────────────────────
    "TEXT": ColorToken(
        name="TEXT",
        default="#ffffff",
        kind="hex",
        category="text",
        description="Primary foreground text.",
        module="jellytoast.ui_helpers",
    ),
    "TEXT_DIM": ColorToken(
        name="TEXT_DIM",
        default="rgba(255,255,255,0.7)",
        kind="rgba",
        category="text",
        description="Secondary text (artist names, subtitles).",
        module="jellytoast.ui_helpers",
    ),
    "TEXT_FAINT": ColorToken(
        name="TEXT_FAINT",
        default="rgba(255,255,255,0.4)",
        kind="rgba",
        category="text",
        description="Kickers, disabled text, hairline labels.",
        module="jellytoast.ui_helpers",
    ),
    "IDLE_TEXT": ColorToken(
        name="IDLE_TEXT",
        default="#a8a8a8",
        kind="hex",
        category="text",
        description="\"Nothing playing\" / empty-state labels.",
        module="jellytoast.ui_helpers",
    ),
    "ERROR_FG": ColorToken(
        name="ERROR_FG",
        default="#f87171",
        kind="hex",
        category="text",
        description="Inline error text (login failure, etc).",
        module="jellytoast.ui_helpers",
    ),
    "WARN_FG": ColorToken(
        name="WARN_FG",
        default="#e0735c",
        kind="hex",
        category="text",
        description="Warning marker (offline indicator).",
        module="jellytoast.ui_helpers",
    ),
    # ── Surfaces ───────────────────────────────────────────────────────
    "BG": ColorToken(
        name="BG",
        default="#101010",
        kind="hex",
        category="surface",
        description="Global window background.",
        module="jellytoast.ui_helpers",
    ),
    "BG_PANEL": ColorToken(
        name="BG_PANEL",
        default="#1a1a1a",
        kind="hex",
        category="surface",
        description="Menu / combo popup fill.",
        module="jellytoast.ui_helpers",
    ),
    "BG_CARD": ColorToken(
        name="BG_CARD",
        default="rgba(255,255,255,0.04)",
        kind="rgba",
        category="surface",
        description="Card surfaces (rare).",
        module="jellytoast.ui_helpers",
    ),
    "BODY_COLOR": ColorToken(
        name="BODY_COLOR",
        default=(18, 18, 18, 172),  # blur-on glass; ~236 on the opaque fallback
        kind="tuple_rgba",
        category="surface",
        description="Main window body fill (painted via QPainter).",
        module="jellytoast.ui_helpers",
    ),
    "MINI_BODY_COLOR": ColorToken(
        name="MINI_BODY_COLOR",
        default=(18, 18, 18, 172),  # blur-dependent alpha; see BODY_COLOR
        kind="tuple_rgba",
        category="surface",
        description="Mini player body fill.",
        module="jellytoast.ui_helpers",
    ),
    "DIALOG_BODY_COLOR": ColorToken(
        name="DIALOG_BODY_COLOR",
        default=(18, 18, 18, 172),  # blur-dependent alpha; see BODY_COLOR
        kind="tuple_rgba",
        category="surface",
        description="Settings / cast dialog body fill.",
        module="jellytoast.ui_helpers",
    ),
    "POPUP_OPAQUE_FILL": ColorToken(
        name="POPUP_OPAQUE_FILL",
        default="rgba(67,67,67,0.65)",
        kind="rgba",
        category="surface",
        description="Opaque popup body (cast/sort menus, combos in Wayland).",
        module="jellytoast.ui_helpers",
    ),
    # ── Highlights / washes ────────────────────────────────────────────
    "WASH_HOVER": ColorToken(
        name="WASH_HOVER",
        default="rgba(255,255,255,0.10)",
        kind="rgba",
        category="highlight",
        description="Icon-button hover, volume popup body.",
        module="jellytoast.ui_helpers",
    ),
    "WASH_PRESSED": ColorToken(
        name="WASH_PRESSED",
        default="rgba(255,255,255,0.15)",
        kind="rgba",
        category="highlight",
        description="Icon-button pressed state.",
        module="jellytoast.ui_helpers",
    ),
    "HOVER_SUBTLE": ColorToken(
        name="HOVER_SUBTLE",
        default="rgba(255,255,255,0.06)",
        kind="rgba",
        category="highlight",
        description="Ghost-button hover, library tile hover.",
        module="jellytoast.ui_helpers",
    ),
    "HOVER_LIST_ROW": ColorToken(
        name="HOVER_LIST_ROW",
        default="rgba(255,255,255,0.10)",
        kind="rgba",
        category="highlight",
        description="List row hover (cast dialog, settings sidebar).",
        module="jellytoast.ui_helpers",
    ),
    "SELECTED_ROW": ColorToken(
        name="SELECTED_ROW",
        default="rgba(255,255,255,0.10)",
        kind="rgba",
        category="highlight",
        description="Selected list row (non-accent variant).",
        module="jellytoast.ui_helpers",
    ),
    "PRESSED_WHITE": ColorToken(
        name="PRESSED_WHITE",
        default="rgba(255,255,255,0.12)",
        kind="rgba",
        category="highlight",
        description="White-press button state (lighter than WASH_PRESSED).",
        module="jellytoast.ui_helpers",
    ),
    "OVERLAY_DARK": ColorToken(
        name="OVERLAY_DARK",
        default="rgba(0,0,0,0.65)",
        kind="rgba",
        category="highlight",
        description="Translucent dark overlay (cover-art heart bg).",
        module="jellytoast.ui_helpers",
    ),
    "OVERLAY_DARK_HOVER": ColorToken(
        name="OVERLAY_DARK_HOVER",
        default="rgba(0,0,0,0.85)",
        kind="rgba",
        category="highlight",
        description="Translucent dark overlay on hover.",
        module="jellytoast.ui_helpers",
    ),
    # ── Inputs ─────────────────────────────────────────────────────────
    "BORDER": ColorToken(
        name="BORDER",
        default="rgba(255,255,255,0.08)",
        kind="rgba",
        category="input",
        description="Input borders, separators.",
        module="jellytoast.ui_helpers",
    ),
    "SURFACE_INPUT": ColorToken(
        name="SURFACE_INPUT",
        default="rgba(255,255,255,0.05)",
        kind="rgba",
        category="input",
        description="QLineEdit / QComboBox / QSpinBox fill.",
        module="jellytoast.ui_helpers",
    ),
    "SURFACE_INPUT_FOCUS": ColorToken(
        name="SURFACE_INPUT_FOCUS",
        default="rgba(255,255,255,0.07)",
        kind="rgba",
        category="input",
        description="Input :focus background tint.",
        module="jellytoast.ui_helpers",
    ),
    "DISABLED_FG": ColorToken(
        name="DISABLED_FG",
        default="rgba(255,255,255,0.30)",
        kind="rgba",
        category="input",
        description="Disabled foreground (icon-button color, placeholders).",
        module="jellytoast.ui_helpers",
    ),
    # ── Destructive ────────────────────────────────────────────────────
    "DANGER": ColorToken(
        name="DANGER",
        default="#ef4444",
        kind="hex",
        category="destructive",
        description="Destructive action accent (e.g. sign out, delete).",
        module="jellytoast.design_tokens",
    ),
    "DANGER_DEEP": ColorToken(
        name="DANGER_DEEP",
        default="#b91c1c",
        kind="hex",
        category="destructive",
        description="Destructive pressed state.",
        module="jellytoast.design_tokens",
    ),
    # ── Sliders ────────────────────────────────────────────────────────
    "SLIDER_GROOVE": ColorToken(
        name="SLIDER_GROOVE",
        default="rgba(255,255,255,0.20)",
        kind="rgba",
        category="slider",
        description="Slider track fill (volume / seek / EQ).",
        module="jellytoast.ui_helpers",
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
    """Return the shipped default value for ``name`` (the frosted-dark theme
    value; see the registry note about blur-/accent-dependent tokens)."""
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
    QSettings.

    Special-case ACCENT: also recompute ACCENT_DEEP (~10% darker)
    and BORDER_ACCENT (rgba at the active theme's border alpha) so the
    rest of the accent family follows. Without this, dragging ACCENT in the Colors
    page leaves checkbox indicator backgrounds (ACCENT_DEEP) and
    focus rings (BORDER_ACCENT) stuck at their previous values. The
    user can still override either independently afterwards — they
    just won't auto-track ACCENT after that point. The accent
    picker's existing "clear accent family" already handles the
    converse for preset picks."""
    token = TOKENS[name]
    # 1. Mutate the live value on the owning module.
    module = importlib.import_module(token.module)
    setattr(module, name, value)
    # 2. Persist if requested.
    if persist:
        from jellytoast.settings_migration import open_qsettings

        open_qsettings().setValue(_qs_key(name), _serialize(value, token.kind))
    # 3. Cascade-derive accent-family followers when ACCENT changes.
    if name == "ACCENT":
        _cascade_accent_family(value, persist=persist)
    # 4. Notify subscribers across the app.
    _emit_theme_changed()


def _cascade_accent_family(accent_value: Any, *, persist: bool) -> None:
    """Derive ACCENT_DEEP + BORDER_ACCENT from a new ACCENT and apply
    them as overrides too. Silent no-op if the hex parse fails."""
    if not (isinstance(accent_value, str) and accent_value.startswith("#")):
        return
    try:
        from jellytoast.theme import (
            _BORDER_ALPHAS,
            _border_accent_for,
            _darken,
            get_active_theme,
        )
    except Exception:
        return
    # Deep: 85% brightness via theme._darken — byte-identical to the
    # accent-picker path (theme.py) so the Colors-page slider and the
    # accent picker produce the SAME ACCENT_DEEP. A local int(round(...))
    # diverged from _darken's int(...) truncation by 1 on some channels.
    try:
        deep_hex = _darken(accent_value)
    except Exception:
        return
    # Border alpha is per-theme — mirror theme.py's `get_active_theme()`
    # accent override so this cascade and the accent picker agree.
    try:
        alpha = _BORDER_ALPHAS.get(get_active_theme().name, 0.35)
    except Exception:
        alpha = 0.35
    border_rgba = _border_accent_for(accent_value, alpha)
    # Apply via the module / QSettings layer directly to avoid recursing
    # back through apply_override (which would re-fire theme_changed
    # twice more). Mutates the globals + persists like the main path.
    for derived_name, derived_value in (
        ("ACCENT_DEEP", deep_hex),
        ("BORDER_ACCENT", border_rgba),
    ):
        derived_token = TOKENS[derived_name]
        derived_module = importlib.import_module(derived_token.module)
        setattr(derived_module, derived_name, derived_value)
        if persist:
            from jellytoast.settings_migration import open_qsettings

            open_qsettings().setValue(
                _qs_key(derived_name),
                _serialize(derived_value, derived_token.kind),
            )


def _reapply_platform_adjustments() -> None:
    """Re-run ``ui_helpers.refresh_theme`` so tokens with platform post-
    processing (e.g. the macOS ``POPUP_OPAQUE_FILL`` opacity bump) recompute
    their *applied* value after a reset — the raw ``token.default`` set above is
    the pre-adjustment value, so without this a reset briefly under-opaques
    popups on macOS until the next theme event. refresh_theme reads the theme +
    re-overlays any REMAINING overrides, so it can't clobber other tokens.
    Best-effort: a no-op before the app/theme is up."""
    try:
        from jellytoast import ui_helpers

        ui_helpers.refresh_theme()
    except Exception:
        pass


def reset(name: str) -> None:
    """Remove the override; restore the shipped default."""
    from jellytoast.settings_migration import open_qsettings

    token = TOKENS[name]
    open_qsettings().remove(_qs_key(name))
    module = importlib.import_module(token.module)
    setattr(module, name, token.default)
    _reapply_platform_adjustments()
    _emit_theme_changed()


def reset_all() -> None:
    """Wipe every override; restore every default."""
    from jellytoast.settings_migration import open_qsettings

    s = open_qsettings()
    for name, token in TOKENS.items():
        s.remove(_qs_key(name))
        module = importlib.import_module(token.module)
        setattr(module, name, token.default)
    _reapply_platform_adjustments()
    _emit_theme_changed()


def load_persisted_overrides() -> None:
    """Apply every override saved in QSettings. Called once at app
    startup (before any widget is constructed, so its first stylesheet
    sees the overridden values)."""
    from jellytoast.settings_migration import open_qsettings

    s = open_qsettings()
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
    # Suspend per-token emits: apply everything under ONE coalesced re-stamp
    # instead of ~15 (one per apply_override). A whole preset swap then costs a
    # single theme_changed, not a visibly-slow storm.
    with _suspend_emit():
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
        # The ACCENT cascade (apply_override -> _cascade_accent_family) derives
        # ACCENT_DEEP/BORDER_ACCENT from ACCENT. If the palette supplies those
        # explicitly but keys them BEFORE ACCENT in the dict, the cascade
        # clobbers them. Re-apply any explicit followers last so user-authored
        # values win regardless of key order (the convenience cascade still
        # fills them in for ACCENT-only palettes).
        for follower in ("ACCENT_DEEP", "BORDER_ACCENT"):
            if "ACCENT" in tokens and follower in tokens and follower in TOKENS:
                fval = tokens[follower]
                ftok = TOKENS[follower]
                if ftok.kind == "tuple_rgba" and isinstance(fval, list):
                    fval = tuple(fval)
                try:
                    apply_override(follower, fval)
                except Exception:
                    pass
    # One re-stamp for the whole palette.
    if applied:
        _emit_theme_changed()
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
    import json as _json

    from jellytoast.settings_migration import open_qsettings

    palette = export_palette(name=name)
    open_qsettings().setValue(_palette_qs_key(name), _json.dumps(palette))


def load_palette(name: str) -> int:
    """Apply a saved palette by name. Returns the count of applied
    tokens; raises KeyError if the palette doesn't exist."""
    import json as _json

    from jellytoast.settings_migration import open_qsettings

    s = open_qsettings()
    key = _palette_qs_key(name)
    if not s.contains(key):
        raise KeyError(name)
    palette = _json.loads(s.value(key, type=str))
    return import_palette(palette)


def list_palettes() -> list[str]:
    """Return the names of all saved palettes, sorted."""
    from jellytoast.settings_migration import open_qsettings

    s = open_qsettings()
    prefix = "debug/color_palettes/"
    return sorted(
        k[len(prefix):] for k in s.allKeys() if k.startswith(prefix)
    )


def delete_palette(name: str) -> None:
    """Remove a saved palette. No-op if it doesn't exist."""
    from jellytoast.settings_migration import open_qsettings

    open_qsettings().remove(_palette_qs_key(name))


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


_emit_suspended = 0


@contextmanager
def _suspend_emit():
    """Coalesce every ``theme_changed`` emit inside the block into ZERO (the
    caller fires one afterwards). ``import_palette`` uses this so applying a
    whole preset costs a SINGLE app-wide re-stamp instead of ~15 (one per
    token) — the difference between a snappy and a visibly-slow theme swap."""
    global _emit_suspended
    _emit_suspended += 1
    try:
        yield
    finally:
        _emit_suspended -= 1


def _emit_theme_changed() -> None:
    """Fire theme_changed so every subscriber re-stamps its styles.
    Wrapped because PlayerBus may not be initialised in early-boot
    contexts (token overrides loaded before QApplication)."""
    if _emit_suspended:
        return
    try:
        from jellytoast.player_state import PlayerBus

        PlayerBus.get().theme_changed.emit()
    except Exception:
        # No bus yet (app booting): persisted overrides apply via
        # load_persisted_overrides() mutating the globals directly,
        # so the first widget construction reads the override.
        pass
