"""base16 scheme importer — parse both formats + feed the existing engine."""

from __future__ import annotations

import pytest

from jellytoast.external_theme import Base16ParseError, parse_base16_yaml
from jellytoast.theme_presets import EMITTED_TOKENS, _base16_to_palette

# New nested form, lowercase slots (base0a..base0f) — the tinted-theming shape.
_MOCHA_NESTED = """
system: "base16"
name: "Catppuccin Mocha"
author: "Catppuccin Org"
variant: "dark"
palette:
  base00: "1e1e2e"
  base01: "181825"
  base02: "313244"
  base03: "45475a"
  base04: "585b70"
  base05: "cdd6f4"
  base06: "f5e0dc"
  base07: "b4befe"
  base08: "f38ba8"
  base09: "fab387"
  base0a: "f9e2af"
  base0b: "a6e3a1"
  base0c: "94e2d5"
  base0d: "89b4fa"
  base0e: "cba6f7"
  base0f: "f2cdcd"
"""

_SLOTS = {f"base0{c}" for c in "0123456789ABCDEF"}


def test_parses_nested_and_normalises_case_and_hash():
    p = parse_base16_yaml(_MOCHA_NESTED)
    assert p.name == "Catppuccin Mocha"
    assert p.variant == "dark"
    assert set(p.base16) == _SLOTS
    assert p.base16["base00"] == "#1e1e2e"
    # lowercase base0e in the file → canonical base0E key, with a leading #
    assert p.base16["base0E"] == "#cba6f7"
    # and it feeds the existing engine cleanly
    tokens = _base16_to_palette(p)["tokens"]
    assert set(tokens) == set(EMITTED_TOKENS)


def test_parses_legacy_flat_with_hash_and_infers_light_variant():
    hexes = [
        "fafafa", "f0f0f0", "e0e0e0", "c0c0c0", "808080", "303030", "202020",
        "101010", "d00000", "d06000", "d0a000", "008000", "008080", "0000d0",
        "8000d0", "804000",
    ]
    flat = "scheme: Light One\nauthor: x\n" + "\n".join(
        f"base0{c}: '#{h}'" for c, h in zip("0123456789abcdef", hexes, strict=True)
    )
    p = parse_base16_yaml(flat)
    assert p.name == "Light One"
    assert p.base16["base00"] == "#fafafa"
    # no explicit variant → inferred from the bright base00 luminance
    assert p.variant == "light"


def test_missing_slots_raises():
    with pytest.raises(Base16ParseError):
        parse_base16_yaml("name: Broken\nbase00: '111111'\n")


def test_accent_slot_override_and_default():
    assert parse_base16_yaml(_MOCHA_NESTED).accent_slot == "base0D"
    assert parse_base16_yaml(_MOCHA_NESTED, accent_slot="base0E").accent_slot == "base0E"
