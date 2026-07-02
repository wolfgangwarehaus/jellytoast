"""Import external base16 color schemes as jellytoast themes (0.1.7 P1a).

Parses a base16 ``.yaml`` scheme — the tinted-theming interchange format (~250
community schemes, and every named palette publishes as one) — into the same
``ThemePreset`` shape the curated presets use, so an imported scheme applies
through the *exact same engine path*
(``theme_presets._base16_to_palette`` → ``color_tokens.import_palette``): bolder
tone, one-emit swap, keep/revert, all for free.

The parser is hand-rolled (no YAML dependency): base16 files are trivially flat
``key: value`` lines. Both the current nested form (``palette:`` block) and the
legacy flat form (top-level ``base00: …``) are accepted, quoted or not, hex with
or without a leading ``#``.
"""

from __future__ import annotations

import re

from jellytoast.theme_presets import ThemePreset

_SLOTS = [f"base0{c}" for c in "0123456789ABCDEF"]
_SLOTS_LOWER = {s.lower(): s for s in _SLOTS}  # "base0a" → canonical "base0A"
_KV = re.compile(r"^(\s*)([A-Za-z0-9_-]+)\s*:\s*(.*)$")
_HEX = re.compile(r"^#?([0-9a-fA-F]{6})$")


class Base16ParseError(ValueError):
    """A scheme text isn't a usable base16 palette (missing slots / bad hex)."""


def _luminance_variant(bg_hex: str) -> str:
    """dark/light inferred from the background's perceived luminance — used only
    when the scheme omits an explicit ``variant``."""
    r, g, b = (int(bg_hex[i : i + 2], 16) for i in (1, 3, 5))
    return "light" if (0.299 * r + 0.587 * g + 0.114 * b) > 128 else "dark"


def parse_base16_yaml(text: str, *, accent_slot: str = "base0D") -> ThemePreset:
    """Parse a base16 scheme into a ``ThemePreset``.

    ``accent_slot`` defaults to ``base0D`` (the base16 convention for the UI
    accent); the import dialog lets the user pick a different slot. ``variant``
    is read from the scheme, else inferred from ``base00`` luminance. Raises
    :class:`Base16ParseError` if any of the 16 slots is missing or malformed.
    """
    name = ""
    variant = ""
    palette: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.rstrip()
        stripped = line.lstrip()
        if not stripped or stripped.startswith("#"):
            continue
        m = _KV.match(line)
        if not m:
            continue
        key, val = m.group(2), m.group(3).strip().strip('"').strip("'")
        key_l = key.lower()
        if key_l in _SLOTS_LOWER:
            hm = _HEX.match(val)
            if hm:
                palette[_SLOTS_LOWER[key_l]] = "#" + hm.group(1).lower()
        elif key_l in ("name", "scheme") and val:
            name = val
        elif key_l == "variant" and val:
            variant = val.lower()

    missing = [s for s in _SLOTS if s not in palette]
    if missing:
        raise Base16ParseError(f"scheme is missing slots: {', '.join(missing)}")
    if accent_slot not in _SLOTS:
        accent_slot = "base0D"
    if variant not in ("dark", "light"):
        variant = _luminance_variant(palette["base00"])
    return ThemePreset(
        name=name or "Imported scheme",
        variant=variant,
        accent_slot=accent_slot,
        base16=palette,
    )
