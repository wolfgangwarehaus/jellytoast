"""Invariants for the design tokens module.

These guard against silent regressions when the registry is edited:
- Tier counts stay 7 typography / 5 button.
- Typography sizes are strictly descending from display → micro.
- Helpers produce non-empty output for every tier.
"""

import pytest

from modules.design_tokens import (
    BUTTON,
    TYPE,
    TYPE_BODY,
    TYPE_DISPLAY,
    TYPE_MICRO,
    button_qss,
    font,
    type_qss,
)


class TestTypography:
    def test_eight_tiers(self):
        # display, title, heading, subhead, body, caption, tiny, micro.
        assert len(TYPE) == 8

    def test_tier_names_unique(self):
        assert len(set(TYPE)) == len(TYPE)

    def test_sizes_strictly_descending_in_definition_order(self):
        # display=22 → title=18 → heading=16 → subhead=14 → body=13
        # → caption=12 → micro=11. If someone adds a tier mid-scale,
        # this catches an out-of-order size.
        sizes = [
            TYPE["display"].size_px,
            TYPE["title"].size_px,
            TYPE["heading"].size_px,
            TYPE["subhead"].size_px,
            TYPE["body"].size_px,
            TYPE["caption"].size_px,
            TYPE["micro"].size_px,
        ]
        assert sizes == sorted(sizes, reverse=True)
        assert len(set(sizes)) == len(sizes)

    def test_micro_is_uppercase_kicker(self):
        assert TYPE_MICRO.uppercase is True
        assert TYPE_MICRO.letter_spacing_em > 0

    def test_body_is_plain(self):
        assert TYPE_BODY.uppercase is False
        assert TYPE_BODY.letter_spacing_em == 0.0

    @pytest.mark.parametrize("tier_name", list(TYPE))
    def test_type_qss_contains_size_and_weight(self, tier_name):
        tier = TYPE[tier_name]
        css = type_qss(tier)
        assert f"font-size: {tier.size_px}px" in css
        assert f"font-weight: {tier.weight}" in css

    def test_type_qss_omits_qt_unsupported_properties(self):
        # Qt's stylesheet parser doesn't accept letter-spacing or
        # text-transform — emitting them produces "Could not parse
        # stylesheet" warnings at runtime. The MICRO tier still needs
        # those visually, but they're applied via QFont (apply_type)
        # rather than QSS.
        for tier_name in TYPE:
            css = type_qss(TYPE[tier_name])
            assert "letter-spacing" not in css, tier_name
            assert "text-transform" not in css, tier_name


class TestFontHelper:
    """`font(tier)` constructs a real QFont. Needs Qt available but no
    QApplication — QFont can be built without one."""

    @pytest.mark.parametrize("tier_name", list(TYPE))
    def test_font_round_trip(self, tier_name):
        tier = TYPE[tier_name]
        f = font(tier)
        assert f.pixelSize() == tier.size_px
        assert int(f.weight()) == tier.weight

    def test_micro_font_uppercases(self):
        from PySide6.QtGui import QFont

        f = font(TYPE_MICRO)
        assert f.capitalization() == QFont.Capitalization.AllUppercase

    def test_display_font_does_not_uppercase(self):
        from PySide6.QtGui import QFont

        f = font(TYPE_DISPLAY)
        assert f.capitalization() == QFont.Capitalization.MixedCase


class TestButtons:
    def test_five_tiers(self):
        assert len(BUTTON) == 5

    def test_expected_tier_names(self):
        assert set(BUTTON) == {
            "primary",
            "secondary",
            "ghost",
            "icon",
            "destructive",
        }

    @pytest.mark.parametrize("tier_name", list(BUTTON))
    def test_button_qss_renders(self, tier_name):
        css = button_qss(BUTTON[tier_name])
        # Every tier must produce a styleable QPushButton block. Hover
        # state coverage matters because plain buttons feel dead without
        # one.
        assert "QPushButton" in css
        assert ":hover" in css
        assert "border-radius" in css

    def test_primary_uses_accent_color(self):
        # The active theme palette uses #00a4dc / #0085bd. If a future
        # theme adds a different accent, update this assertion (or pull
        # the value via get_active_theme).
        from modules.theme import get_active_theme

        accent = get_active_theme().accent
        assert accent in button_qss(BUTTON["primary"])

    def test_destructive_uses_danger_red(self):
        from modules.design_tokens import DANGER

        assert DANGER in button_qss(BUTTON["destructive"])

    def test_unknown_tier_raises(self):
        from modules.design_tokens import TYPE_BODY, ButtonTier

        bogus = ButtonTier("nope", height_px=20, pad_x=4, pad_y=4, radius=4, type_tier=TYPE_BODY)
        with pytest.raises(ValueError, match="unknown button tier"):
            button_qss(bogus)


class TestSpacingAndRadii:
    """Cheap sanity check — these are constants, but if someone edits
    them out of order the visual rhythm collapses."""

    def test_spacing_strictly_increasing(self):
        from modules.design_tokens import (
            SPACE_LG,
            SPACE_MD,
            SPACE_SM,
            SPACE_XL,
            SPACE_XS,
            SPACE_XXL,
        )

        scale = [SPACE_XS, SPACE_SM, SPACE_MD, SPACE_LG, SPACE_XL, SPACE_XXL]
        assert scale == sorted(scale)
        assert len(set(scale)) == len(scale)

    def test_radii_strictly_increasing(self):
        from modules.design_tokens import (
            RADIUS_LG,
            RADIUS_MD,
            RADIUS_SM,
            RADIUS_XL,
        )

        scale = [RADIUS_SM, RADIUS_MD, RADIUS_LG, RADIUS_XL]
        assert scale == sorted(scale)
        assert len(set(scale)) == len(scale)
