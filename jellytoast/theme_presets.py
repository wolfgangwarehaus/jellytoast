"""Curated named color presets (Catppuccin, Nord, Gruvbox, …).

Each preset is authored as a **base16 palette** (the 16 fixed-semantic slots
``base00``–``base0F``) plus a ``variant`` tag and an explicit ``accent_slot``.
A single shared mapper (:func:`_base16_to_palette`) resolves that into the
palette-dict shape the ``color_tokens`` engine already consumes via
``import_palette`` — so a preset is just data + one mapping rule, no new engine.

The mapping is deliberately narrow: only the tokens base16 *meaningfully colors*
are emitted (backgrounds, text, accent, error/warn, body fills). Every
structural wash (``WASH_*``, ``HOVER_*``, ``SELECTED_ROW``, ``BORDER``,
``OVERLAY_DARK*``, …) is **omitted on purpose** so it follows the light/dark
*base theme* the preset's ``variant`` selects — recoloring those from base16
looks muddy and breaks the light-family inversion. ``ACCENT_DEEP`` /
``BORDER_ACCENT`` are likewise omitted: ``import_palette`` runs the accent
cascade (``_cascade_accent_family``) off ``ACCENT`` alone, byte-identical to the
accent picker.

The picker in Settings → Display applies a preset by:
  1. setting ``theme_mode`` to ``frosted_dark`` / ``frosted_light`` from the
     variant (so the accent cascade reads the right border-alpha and the omitted
     washes inherit the correct base), and ``accent_color`` to the accent hex,
  2. ``import_palette(_base16_to_palette(preset))`` (persists + cascades + emits),
  3. ``refresh_theme()`` inside a ``theme_swap_guard`` — re-derives the washes
     from the variant base and re-overlays the preset's colorful tokens.
"""

from __future__ import annotations

from dataclasses import dataclass

from jellytoast.theme import _darken, _hex_to_rgb

# Alpha/const values that come from the app's own visual language, NOT base16:
# the body-fill glass alpha and the two muted-text alphas mirror the built-in
# frosted themes so a preset reads like a native theme, not a recolor.
_GLASS_A = 172
_DIM_A = 0.7
_FAINT_A = 0.4


@dataclass(frozen=True)
class ThemePreset:
    """A curated scheme. ``base16`` maps ``base00``..``base0F`` to ``#rrggbb``;
    ``accent_slot`` names which slot drives the single ``ACCENT`` token (base16
    has no canonical brand accent, so each preset picks one explicitly)."""

    name: str
    variant: str  # "dark" | "light"
    accent_slot: str  # e.g. "base0E"
    base16: dict[str, str]


def _rgba(hex_str: str, alpha: float) -> str:
    """``#rrggbb`` → ``"rgba(r,g,b,a)"`` in the exact format the engine stores
    (no spaces) so an import round-trips byte-for-byte."""
    r, g, b = _hex_to_rgb(hex_str)
    return f"rgba({r},{g},{b},{alpha})"


def _base16_to_palette(p: ThemePreset) -> dict:
    """Resolve a preset's base16 data into an ``import_palette`` dict.

    Emits ONLY the meaningfully-colored tokens (see module docstring); the
    accent is shipped as ``ACCENT`` alone so the engine cascade derives
    ``ACCENT_DEEP`` / ``BORDER_ACCENT``. ``*_BODY_COLOR`` are ``tuple_rgba``
    (rgb from ``base00``, alpha from the app glass value) serialised as lists —
    ``import_palette`` converts them back to tuples."""
    b = p.base16
    bg_rgb = list(_hex_to_rgb(b["base00"]))
    return {
        "version": 1,
        "name": p.name,
        "tokens": {
            # accent — ONLY ACCENT; engine cascade fills the deep/border followers
            "ACCENT": b[p.accent_slot],
            # text
            "TEXT": b["base05"],
            "TEXT_DIM": _rgba(b["base05"], _DIM_A),
            "TEXT_FAINT": _rgba(b["base05"], _FAINT_A),
            "IDLE_TEXT": b["base04"],
            "ERROR_FG": b["base08"],
            "WARN_FG": b["base09"],
            # surfaces (QSS) — commit the preset's OWN elevation ramp so the
            # whole app adopts the scheme, not just bg/text/accent: base01 is
            # base16's "lighter background" (cards/inputs), base02 its selection,
            # base03 its comment-grey (borders). Opaque so they read as the
            # preset's tone rather than a faint white wash over it.
            "BG": b["base00"],
            "BG_PANEL": b["base02"],
            "POPUP_OPAQUE_FILL": _rgba(b["base02"], 0.98),
            "BG_CARD": _rgba(b["base01"], 1.0),
            "SELECTED_ROW": _rgba(b["base02"], 1.0),
            "SURFACE_INPUT": _rgba(b["base01"], 1.0),
            "SURFACE_INPUT_FOCUS": _rgba(b["base02"], 1.0),
            "BORDER": _rgba(b["base03"], 0.9),
            # body paint fills (QPainter) — tuple_rgba; alpha is app glass, not base16
            "BODY_COLOR": [*bg_rgb, _GLASS_A],
            "MINI_BODY_COLOR": [*bg_rgb, _GLASS_A],
            "DIALOG_BODY_COLOR": [*bg_rgb, _GLASS_A],
            # destructive
            "DANGER": b["base08"],
            "DANGER_DEEP": _darken(b["base08"], 0.75),
        },
    }


# The tokens every preset emits — a parity guard target (all presets emit the
# SAME set so switching never leaves orphan overrides). Kept in sync with
# _base16_to_palette by test_theme_presets.
EMITTED_TOKENS: tuple[str, ...] = (
    "ACCENT", "TEXT", "TEXT_DIM", "TEXT_FAINT", "IDLE_TEXT", "ERROR_FG", "WARN_FG",
    "BG", "BG_PANEL", "POPUP_OPAQUE_FILL", "BG_CARD", "SELECTED_ROW",
    "SURFACE_INPUT", "SURFACE_INPUT_FOCUS", "BORDER", "BODY_COLOR",
    "MINI_BODY_COLOR", "DIALOG_BODY_COLOR", "DANGER", "DANGER_DEEP",
)


# ── The curated set ─────────────────────────────────────────────────────────
# base16 slot roles: 00 bg · 01 lighter bg · 02 selection · 03 comments ·
# 04 dark fg · 05 fg · 06 light fg · 07 lightest · 08 red · 09 orange ·
# 0A yellow · 0B green · 0C cyan · 0D blue · 0E magenta/purple · 0F brown.

BUILTIN_PRESETS: list[ThemePreset] = [
    ThemePreset(
        name="Catppuccin Mocha", variant="dark", accent_slot="base0E",
        base16={
            "base00": "#1e1e2e", "base01": "#181825", "base02": "#313244", "base03": "#45475a",
            "base04": "#585b70", "base05": "#cdd6f4", "base06": "#f5e0dc", "base07": "#b4befe",
            "base08": "#f38ba8", "base09": "#fab387", "base0A": "#f9e2af", "base0B": "#a6e3a1",
            "base0C": "#94e2d5", "base0D": "#89b4fa", "base0E": "#cba6f7", "base0F": "#f2cdcd",
        },
    ),
    ThemePreset(
        name="Catppuccin Macchiato", variant="dark", accent_slot="base0E",
        base16={
            "base00": "#24273a", "base01": "#1e2030", "base02": "#363a4f", "base03": "#494d64",
            "base04": "#5b6078", "base05": "#cad3f5", "base06": "#f4dbd6", "base07": "#b7bdf8",
            "base08": "#ed8796", "base09": "#f5a97f", "base0A": "#eed49f", "base0B": "#a6da95",
            "base0C": "#8bd5ca", "base0D": "#8aadf4", "base0E": "#c6a0f6", "base0F": "#f0c6c6",
        },
    ),
    ThemePreset(
        name="Catppuccin Frappé", variant="dark", accent_slot="base0E",
        base16={
            "base00": "#303446", "base01": "#292c3c", "base02": "#414559", "base03": "#51576d",
            "base04": "#626880", "base05": "#c6d0f5", "base06": "#f2d5cf", "base07": "#babbf1",
            "base08": "#e78284", "base09": "#ef9f76", "base0A": "#e5c890", "base0B": "#a6d189",
            "base0C": "#81c8be", "base0D": "#8caaee", "base0E": "#ca9ee6", "base0F": "#eebebe",
        },
    ),
    ThemePreset(
        name="Catppuccin Latte", variant="light", accent_slot="base0E",
        base16={
            "base00": "#eff1f5", "base01": "#e6e9ef", "base02": "#ccd0da", "base03": "#bcc0cc",
            "base04": "#acb0be", "base05": "#4c4f69", "base06": "#dc8a78", "base07": "#7287fd",
            "base08": "#d20f39", "base09": "#fe640b", "base0A": "#df8e1d", "base0B": "#40a02b",
            "base0C": "#179299", "base0D": "#1e66f5", "base0E": "#8839ef", "base0F": "#dd7878",
        },
    ),
    ThemePreset(
        name="Nord", variant="dark", accent_slot="base0D",
        base16={
            "base00": "#2e3440", "base01": "#3b4252", "base02": "#434c5e", "base03": "#4c566a",
            "base04": "#d8dee9", "base05": "#e5e9f0", "base06": "#eceff4", "base07": "#8fbcbb",
            "base08": "#bf616a", "base09": "#d08770", "base0A": "#ebcb8b", "base0B": "#a3be8c",
            "base0C": "#88c0d0", "base0D": "#81a1c1", "base0E": "#b48ead", "base0F": "#5e81ac",
        },
    ),
    ThemePreset(
        name="Gruvbox Dark", variant="dark", accent_slot="base09",
        base16={
            "base00": "#282828", "base01": "#3c3836", "base02": "#504945", "base03": "#665c54",
            "base04": "#bdae93", "base05": "#d5c4a1", "base06": "#ebdbb2", "base07": "#fbf1c7",
            "base08": "#fb4934", "base09": "#fe8019", "base0A": "#fabd2f", "base0B": "#b8bb26",
            "base0C": "#8ec07c", "base0D": "#83a598", "base0E": "#d3869b", "base0F": "#d65d0e",
        },
    ),
    ThemePreset(
        name="Gruvbox Light", variant="light", accent_slot="base09",
        base16={
            "base00": "#fbf1c7", "base01": "#ebdbb2", "base02": "#d5c4a1", "base03": "#bdae93",
            "base04": "#665c54", "base05": "#504945", "base06": "#3c3836", "base07": "#282828",
            "base08": "#9d0006", "base09": "#af3a03", "base0A": "#b57614", "base0B": "#79740e",
            "base0C": "#427b58", "base0D": "#076678", "base0E": "#8f3f71", "base0F": "#d65d0e",
        },
    ),
    ThemePreset(
        name="Tokyo Night", variant="dark", accent_slot="base0D",
        base16={
            "base00": "#1a1b26", "base01": "#16161e", "base02": "#2f3549", "base03": "#444b6a",
            "base04": "#787c99", "base05": "#a9b1d6", "base06": "#c0caf5", "base07": "#d5d6db",
            "base08": "#f7768e", "base09": "#ff9e64", "base0A": "#e0af68", "base0B": "#9ece6a",
            "base0C": "#7dcfff", "base0D": "#7aa2f7", "base0E": "#bb9af7", "base0F": "#de5971",
        },
    ),
    ThemePreset(
        name="Rosé Pine", variant="dark", accent_slot="base0D",
        base16={
            "base00": "#191724", "base01": "#1f1d2e", "base02": "#26233a", "base03": "#6e6a86",
            "base04": "#908caa", "base05": "#e0def4", "base06": "#e0def4", "base07": "#524f67",
            "base08": "#eb6f92", "base09": "#f6c177", "base0A": "#ebbcba", "base0B": "#31748f",
            "base0C": "#9ccfd8", "base0D": "#c4a7e7", "base0E": "#c4a7e7", "base0F": "#524f67",
        },
    ),
]


# Resolved once at import: (name, palette_dict) for the picker to feed the engine,
# and a by-name lookup for the picker to read variant / accent_slot.
PRESET_PALETTES: list[tuple[str, dict]] = [
    (p.name, _base16_to_palette(p)) for p in BUILTIN_PRESETS
]
PRESET_BY_NAME: dict[str, ThemePreset] = {p.name: p for p in BUILTIN_PRESETS}


# ── Theme families (0.1.7 P2 — unified family + mode model) ──────────────────
# A *family* groups the dark/light members a user flips between with the
# Dark / Light / Follow-system mode control. A family with BOTH members exposes
# follow-system (swap on OS light/dark); a single-member family is dark-only (no
# mode control). ``"jellytoast"`` (the built-in Theme set) and ``"imported"`` (a
# one-off base16 the user loaded) are recognised sentinels, NOT rows in the
# table below — the resolver + UI special-case them.


@dataclass(frozen=True)
class ThemeFamily:
    key: str
    label: str
    dark: ThemePreset | None
    light: ThemePreset | None

    @property
    def has_both(self) -> bool:
        return self.dark is not None and self.light is not None

    def member_for(self, lum: str) -> ThemePreset | None:
        """The member for a resolved luminance (``"dark"``/``"light"``), falling
        back to whichever member exists — so a dark-only family ignores ``lum``."""
        pick = self.light if lum == "light" else self.dark
        return pick or self.dark or self.light


def _fam(key: str, label: str, dark_name: str, light_name: str | None = None) -> ThemeFamily:
    return ThemeFamily(
        key,
        label,
        dark=PRESET_BY_NAME.get(dark_name) if dark_name else None,
        light=PRESET_BY_NAME.get(light_name) if light_name else None,
    )


THEME_FAMILIES: dict[str, ThemeFamily] = {
    "catppuccin": _fam("catppuccin", "Catppuccin", "Catppuccin Mocha", "Catppuccin Latte"),
    "catppuccin-frappe": _fam("catppuccin-frappe", "Catppuccin Frappé", "Catppuccin Frappé"),
    "catppuccin-macchiato": _fam(
        "catppuccin-macchiato", "Catppuccin Macchiato", "Catppuccin Macchiato"
    ),
    "gruvbox": _fam("gruvbox", "Gruvbox", "Gruvbox Dark", "Gruvbox Light"),
    "nord": _fam("nord", "Nord", "Nord"),
    "tokyo-night": _fam("tokyo-night", "Tokyo Night", "Tokyo Night"),
    "rose-pine": _fam("rose-pine", "Rosé Pine", "Rosé Pine"),
}

# Dropdown order for the preset families (the UI prepends the "jellytoast"
# sentinel first). Keeps the Catppuccin flavours adjacent.
FAMILY_ORDER: list[str] = [
    "catppuccin",
    "catppuccin-frappe",
    "catppuccin-macchiato",
    "gruvbox",
    "nord",
    "tokyo-night",
    "rose-pine",
]

# member preset name → family key: migrates the old ``last_preset_name`` and
# points the dropdown at the family a stored member belongs to.
PRESET_NAME_TO_FAMILY: dict[str, str] = {
    m.name: fam.key
    for fam in THEME_FAMILIES.values()
    for m in (fam.dark, fam.light)
    if m is not None
}


def family_label(key: str) -> str:
    """Human label for a family key (``""``/``"jellytoast"`` → ``"jellytoast"``)."""
    if key in ("", "jellytoast"):
        return "jellytoast"
    fam = THEME_FAMILIES.get(key)
    return fam.label if fam else key


def family_has_both(key: str) -> bool:
    """Whether the Dark / Light / Follow-system mode control applies. jellytoast
    always does; a preset family does only when it ships both members; a
    single-member or imported family does not."""
    if key in ("", "jellytoast"):
        return True
    fam = THEME_FAMILIES.get(key)
    return bool(fam and fam.has_both)


# ── Live apply — the single source of truth for a theme selection ────────────


def _os_lum(mode: str) -> str:
    """Resolve a luminance intent (``"auto"``/``"dark"``/``"light"``) to
    ``"dark"``/``"light"`` — ``"auto"`` follows the OS scheme."""
    if mode == "auto":
        from jellytoast.theme import os_color_scheme

        return os_color_scheme()
    return "light" if mode == "light" else "dark"


def _live_restamp() -> None:
    """Refresh the token constants + icons and broadcast ``theme_changed`` under
    the swap guard — the shared live-apply tail every theme change runs."""
    from jellytoast import icons as _icons
    from jellytoast import ui_helpers as _uih
    from jellytoast.player_state import PlayerBus

    with _uih.theme_swap_guard():
        _uih.refresh_theme()
        _icons.refresh_theme()
        PlayerBus.get().theme_changed.emit()


def apply_theme_family(family: str, mode: str, frosted: bool) -> None:
    """Apply a theme selection (family + luminance ``mode`` + ``frosted``) live.

    The single entry point shared by launch re-resolve, the OS-scheme watcher,
    and the Settings dialog. ``jellytoast``/``""`` drops any preset overrides so
    the built-in base theme + accent tokens win; a preset family imports the
    resolved member's palette (cascades its accent + tints the body), and the
    built-in base theme (``frosted_<variant>`` / ``<variant>``) still supplies
    the omitted washes. Persists all three axes. Does NOT show the keep/revert
    prompt — that's the dialog's responsibility."""
    from jellytoast import color_tokens as ct
    from jellytoast.settings import get_settings

    s = get_settings()
    s.theme_mode = mode
    s.frosted = bool(frosted)
    s.theme_family = family or ""
    if family in ("", "jellytoast"):
        # Built-in jellytoast theme: wipe any preset overrides so the base theme
        # + user accent win. Leaves accent_color as the user set it.
        ct.reset_all()
        s.last_preset_name = ""
    elif family == "imported":
        # Imported scheme has no registered members — its palette is already
        # persisted (see apply_imported_preset); just re-stamp on the new axes.
        pass
    else:
        member = THEME_FAMILIES[family].member_for(_os_lum(mode))
        if member is not None:
            s.accent_color = member.base16[member.accent_slot]
            ct.import_palette(_base16_to_palette(member))
            s.last_preset_name = member.name
    _live_restamp()


def apply_imported_preset(preset: ThemePreset, frosted: bool) -> None:
    """Apply a user-imported base16 scheme (no family). Persists the raw scheme
    so its preview grid + body tint survive a restart, records
    ``theme_family="imported"`` with the scheme's own variant as the luminance
    (single variant → no follow-system), imports the palette, and live-restamps."""
    import json

    from jellytoast import color_tokens as ct
    from jellytoast.settings import get_settings

    s = get_settings()
    s.theme_mode = "light" if preset.variant == "light" else "dark"
    s.frosted = bool(frosted)
    s.theme_family = "imported"
    s.imported_scheme_json = json.dumps(
        {
            "name": preset.name,
            "variant": preset.variant,
            "accent_slot": preset.accent_slot,
            "base16": preset.base16,
        }
    )
    s.accent_color = preset.base16[preset.accent_slot]
    ct.import_palette(_base16_to_palette(preset))
    s.last_preset_name = preset.name
    _live_restamp()


def imported_preset_from_settings() -> ThemePreset | None:
    """Reconstruct the imported ThemePreset from the persisted JSON (for the
    dialog's preview grid). None when nothing valid is stored."""
    import json

    from jellytoast.settings import get_settings

    raw = get_settings().imported_scheme_json
    if not raw:
        return None
    try:
        d = json.loads(raw)
        return ThemePreset(
            name=d["name"],
            variant=d["variant"],
            accent_slot=d["accent_slot"],
            base16=dict(d["base16"]),
        )
    except Exception:
        return None
