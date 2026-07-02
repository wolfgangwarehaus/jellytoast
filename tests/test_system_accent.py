"""XDG-portal accent reader — the (ddd)-variant → hex parse (no live D-Bus)."""

from __future__ import annotations

from jellytoast.system_accent import _accent_from_variant


def test_parses_ddd_variant_and_bare_triple(qapp):
    # jeepney's usual variant shape ("(ddd)", (r,g,b))
    assert _accent_from_variant(("(ddd)", (1.0, 0.0, 0.0))) == "#ff0000"
    # and a bare triple
    assert _accent_from_variant((0.0, 1.0, 0.0)) == "#00ff00"
    # mid values round-trip through the 0..1 → 0..255 scale
    assert _accent_from_variant((0.5, 0.5, 0.5)).lower() in ("#808080", "#7f7f7f")


def test_unset_or_malformed_returns_none(qapp):
    assert _accent_from_variant(("(ddd)", (-1.0, -1.0, -1.0))) is None  # portal "unset"
    assert _accent_from_variant((1.5, 0.0, 0.0)) is None  # out of range
    assert _accent_from_variant(None) is None
    assert _accent_from_variant(("(ddd)", (0.5, 0.5))) is None  # wrong arity
    assert _accent_from_variant("nope") is None
