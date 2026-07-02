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


def _perceived_luminance(hex_str: str) -> float:
    """0-255 perceived luminance (ITU-R 601 weights)."""
    r, g, b = (int(hex_str[i : i + 2], 16) for i in (1, 3, 5))
    return 0.299 * r + 0.587 * g + 0.114 * b


def _luminance_variant(bg_hex: str) -> str:
    """dark/light inferred from the background's perceived luminance — used
    when the scheme omits ``variant`` or mislabels it (see the cross-check in
    :func:`parse_base16_yaml`)."""
    return "light" if _perceived_luminance(bg_hex) > 128 else "dark"


def _rel_luminance(hex_str: str) -> float:
    """WCAG relative luminance in [0, 1]."""
    def chan(c: int) -> float:
        v = c / 255.0
        return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4

    r, g, b = (int(hex_str[i : i + 2], 16) for i in (1, 3, 5))
    return 0.2126 * chan(r) + 0.7152 * chan(g) + 0.0722 * chan(b)


def contrast_ratio(a_hex: str, b_hex: str) -> float:
    """WCAG contrast ratio between two ``#rrggbb`` colors (1.0 – 21.0)."""
    la, lb = _rel_luminance(a_hex), _rel_luminance(b_hex)
    lighter, darker = max(la, lb), min(la, lb)
    return (lighter + 0.05) / (darker + 0.05)


# Below this fg-vs-bg ratio a scheme is unreadable garbage, not a taste choice
# (WCAG AA body text is 4.5; the softest legitimate schemes sit well above 3).
_MIN_TEXT_CONTRAST = 3.0


def _mix(a_hex: str, b_hex: str, t: float) -> str:
    """Linear mix of two ``#rrggbb`` colors (t = weight of ``b``)."""
    ar, ag, ab_ = (int(a_hex[i : i + 2], 16) for i in (1, 3, 5))
    br, bg, bb = (int(b_hex[i : i + 2], 16) for i in (1, 3, 5))
    return "#{:02x}{:02x}{:02x}".format(
        round(ar + (br - ar) * t), round(ag + (bg - ag) * t), round(ab_ + (bb - ab_) * t)
    )


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
    lum_variant = _luminance_variant(palette["base00"])
    if variant not in ("dark", "light"):
        variant = lum_variant
    elif variant != lum_variant and abs(_perceived_luminance(palette["base00"]) - 128) > 32:
        # The declared variant contradicts a DECISIVELY dark/light background —
        # a mislabeled scheme. The variant keys the structural washes
        # (hover/selection polarity), so trusting the label paints light washes
        # on a light bg (or the inverse). Trust the background instead; near
        # the midpoint the label wins (a taste call, not a mistake).
        variant = lum_variant
    if contrast_ratio(palette["base05"], palette["base00"]) < _MIN_TEXT_CONTRAST:
        raise Base16ParseError(
            "the scheme's text (base05) and background (base00) are too similar "
            "to be readable"
        )
    return ThemePreset(
        name=name or "Imported scheme",
        variant=variant,
        accent_slot=accent_slot,
        base16=palette,
    )


def ensure_readable(preset: ThemePreset) -> ThemePreset:
    """Return ``preset`` with its text slots nudged to a readable floor — the
    non-interactive ingest paths (pywal watcher) can't pop an error dialog, so
    an unreadable machine-generated palette gets its fg clamped instead."""
    b = dict(preset.base16)
    if contrast_ratio(b["base05"], b["base00"]) < _MIN_TEXT_CONTRAST:
        b["base05"] = "#f2f2f2" if preset.variant == "dark" else "#1d1d1d"
        b["base04"] = _mix(b["base00"], b["base05"], 0.55)
        b["base06"] = b["base05"]
        b["base07"] = b["base05"]
        return ThemePreset(
            name=preset.name,
            variant=preset.variant,
            accent_slot=preset.accent_slot,
            base16=b,
        )
    return preset


def parse_pywal_json(text: str) -> ThemePreset:
    """Parse a pywal / wallust ``colors.json`` into a ``ThemePreset``.

    pywal palettes are wallpaper-derived (16 ANSI colors + special bg/fg), so
    the base16 mapping is conventional: elevation ramp mixed from bg→fg, ANSI
    reds/yellows/greens onto the semantic slots, ``color4`` (blue-ish, usually
    the most prominent wallpaper hue) as the accent. Raises
    :class:`Base16ParseError` on malformed / incomplete input."""
    import json
    import os

    def norm(v) -> str:
        m = _HEX.match(str(v).strip())
        if not m:
            raise Base16ParseError(f"bad hex value in colors.json: {v!r}")
        return "#" + m.group(1).lower()

    try:
        d = json.loads(text)
        special = d["special"]
        colors = d["colors"]
        bg = norm(special["background"])
        fg = norm(special["foreground"])
        ansi = {k: norm(colors[f"color{k}"]) for k in range(16)}
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        if isinstance(exc, Base16ParseError):
            raise
        raise Base16ParseError(f"not a pywal colors.json: {exc!r}") from exc

    variant = _luminance_variant(bg)
    wallpaper = os.path.basename(str(d.get("wallpaper", "")) or "")
    palette = {
        "base00": bg,
        "base01": _mix(bg, fg, 0.06),
        "base02": _mix(bg, fg, 0.14),
        "base03": ansi[8],
        "base04": _mix(bg, fg, 0.55),
        "base05": fg,
        "base06": _mix(fg, "#ffffff" if variant == "dark" else "#000000", 0.3),
        "base07": ansi[15],
        "base08": ansi[1],
        "base09": ansi[3],
        "base0A": ansi[3],
        "base0B": ansi[2],
        "base0C": ansi[6],
        "base0D": ansi[4],
        "base0E": ansi[5],
        "base0F": ansi[7],
    }
    return ensure_readable(
        ThemePreset(
            name=f"pywal — {wallpaper}" if wallpaper else "pywal",
            variant=variant,
            accent_slot="base0D",
            base16=palette,
        )
    )
