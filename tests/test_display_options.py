"""Display options: the "Square corners" toggle + the UI font-family picker.

Square corners routes every finite radius token through ``design_tokens.rad()``
(0 when on), leaving the pill/circle sentinel and genuinely round controls
untouched. The font picker prefixes the chosen family onto the global QSS
font stack. Both bake at import (like ``font_scale``) → restart-required.
"""

from __future__ import annotations

from jellytoast import design_tokens as dt
from jellytoast import settings as _settings_mod
from jellytoast import ui_helpers as uih


def test_rad_squares_finite_but_keeps_the_pill_sentinel():
    orig = dt._SQUARE_CORNERS
    try:
        dt.set_square_corners(False)
        assert dt.rad(8) == 8 and dt.rad(4) == 4 and dt.rad(9999) == 9999

        dt.set_square_corners(True)
        # Finite radii collapse to a square.
        assert dt.rad(8) == 0 and dt.rad(4) == 0 and dt.rad(999) == 0
        # The pill/circle sentinel (>= 1000) passes through so round icon
        # buttons, the slider handle, avatars etc. never go sharp-cornered.
        assert dt.rad(9999) == 9999 and dt.rad(1000) == 1000
    finally:
        dt.set_square_corners(orig)


def test_font_family_stack_prefixes_user_choice(monkeypatch):
    class _S:
        font_family = "Comic Sans MS"

    monkeypatch.setattr(_settings_mod, "get_settings", lambda: _S())
    stack = uih._ui_font_family_stack()
    assert stack.startswith("'Comic Sans MS', ")
    assert "'Inter'" in stack  # keeps the built-in stack as fallback

    class _Empty:
        font_family = ""

    monkeypatch.setattr(_settings_mod, "get_settings", lambda: _Empty())
    # No selection → the built-in stack verbatim.
    assert uih._ui_font_family_stack() == "'Inter', 'Segoe UI', 'Noto Sans', sans-serif"


def test_global_style_builds_in_both_corner_modes():
    orig = dt._SQUARE_CORNERS
    try:
        dt.set_square_corners(True)
        squared = uih._build_global_style()
        dt.set_square_corners(False)
        rounded = uih._build_global_style()
        # Both render; square mode zeroes the token-driven radii, rounded keeps
        # them — so the two stylesheets must differ.
        assert "font-family:" in squared and "font-family:" in rounded
        assert squared != rounded
        assert "border-radius: 0px" in squared
    finally:
        dt.set_square_corners(orig)


def test_settings_square_corners_and_font_family_roundtrip():
    s = _settings_mod.get_settings()
    orig_sc, orig_ff = s.square_corners, s.font_family
    try:
        s.square_corners = True
        assert s.square_corners is True
        s.square_corners = False
        assert s.square_corners is False

        s.font_family = "  DejaVu Sans  "
        assert s.font_family == "DejaVu Sans"  # whitespace stripped on write
        s.font_family = ""
        assert s.font_family == ""
    finally:
        s.square_corners = orig_sc
        s.font_family = orig_ff
