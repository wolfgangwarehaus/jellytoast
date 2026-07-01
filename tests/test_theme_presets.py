"""Curated theme presets — the base16→token mapper + a real engine round-trip.

Carries a *parity guard* (every preset emits the identical token set, so
switching presets can't leave orphan overrides) and confirms each preset applies
byte-for-byte through the actual ``color_tokens`` engine, with the accent cascade
firing off ``ACCENT`` alone.
"""

from __future__ import annotations

import jellytoast.color_tokens as ct
from jellytoast.color_tokens import TOKENS
from jellytoast.theme_presets import (
    BUILTIN_PRESETS,
    EMITTED_TOKENS,
    PRESET_PALETTES,
    _base16_to_palette,
)

_ALL_SLOTS = {f"base0{c}" for c in "0123456789ABCDEF"}


def test_every_preset_emits_the_fixed_token_set_with_valid_kinds():
    for p in BUILTIN_PRESETS:
        assert p.variant in ("dark", "light"), p.name
        assert set(p.base16) == _ALL_SLOTS, p.name
        assert p.accent_slot in p.base16, p.name
        toks = _base16_to_palette(p)["tokens"]
        # parity guard: identical emitted set across every preset
        assert set(toks) == set(EMITTED_TOKENS), p.name
        for name, val in toks.items():
            assert name in TOKENS, (p.name, name)
            kind = TOKENS[name].kind
            if kind == "hex":
                assert isinstance(val, str) and val.startswith("#"), (p.name, name, val)
            elif kind == "rgba":
                assert isinstance(val, str) and val.startswith("rgba("), (p.name, name, val)
            elif kind == "tuple_rgba":
                assert isinstance(val, list) and len(val) == 4, (p.name, name, val)
            else:  # a kind we don't emit slipped in
                raise AssertionError(f"{p.name}: unexpected kind {kind} for {name}")


def test_import_round_trips_through_the_engine(qapp):
    try:
        for name, palette in PRESET_PALETTES:
            applied = ct.import_palette(palette)
            assert applied == len(palette["tokens"]), name
            for tok, want in palette["tokens"].items():
                got = ct.get_current(tok)
                if TOKENS[tok].kind == "tuple_rgba":
                    assert list(got) == list(want), (name, tok, got, want)
                else:
                    assert got == want, (name, tok, got, want)
            # the accent cascade fired: deep/border followers derived, non-empty
            assert str(ct.get_current("ACCENT_DEEP")).startswith("#"), name
            assert str(ct.get_current("BORDER_ACCENT")).startswith("rgba("), name
    finally:
        ct.reset_all()


def test_preset_names_are_unique():
    names = [p.name for p in BUILTIN_PRESETS]
    assert len(names) == len(set(names))
