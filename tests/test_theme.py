"""Tests for modules.theme — the semantic-token theme registry.

Covers the `Theme` frozen dataclass, the `THEMES` registry, the shared
`_DARK_TOKENS` splat, `get_active_theme()` (theme selection + accent
override + malformed-hex fallback), and the `ink_alpha()` /
`_darken` / `_hex_to_rgb` helpers.

QSettings is process-wide in test mode (conftest enables test mode),
so the `clean_theme_settings` fixture clears the two keys this module
reads — `ui/theme_mode` and `ui/accent_color` — before and after each
test that touches `get_active_theme()`.
"""

from __future__ import annotations

import dataclasses

import pytest

from modules import theme as th
from modules.theme import THEMES, Theme


# ── Fixture: isolate the QSettings keys get_active_theme() reads ──────


def _settings_handle():
    """The exact QSettings handle the get_settings() singleton reads
    through. get_active_theme() resolves get_settings()._s; a write
    from any *other* QSettings handle isn't visible to this one until
    it sync()s (QSettings caches file contents per instance). Writing
    and reading through this single handle keeps the test hermetic
    regardless of suite ordering."""
    from modules.settings import get_settings

    return get_settings()._s


@pytest.fixture
def clean_theme_settings():
    """Wipe ui/theme_mode + ui/accent_color before AND after the test
    so get_active_theme() sees a clean slate, then restore nothing
    (the defaults are re-derived from QSettings defaults)."""

    def _wipe():
        s = _settings_handle()
        s.remove("ui/theme_mode")
        s.remove("ui/accent_color")
        s.sync()

    _wipe()
    yield
    _wipe()


def _set_theme_settings(theme_mode=None, accent_color=None):
    """Write the two keys get_active_theme() reads through the same
    QSettings handle get_settings() uses, then sync so the read in
    get_active_theme() sees them."""
    s = _settings_handle()
    if theme_mode is not None:
        s.setValue("ui/theme_mode", theme_mode)
    if accent_color is not None:
        s.setValue("ui/accent_color", accent_color)
    s.sync()


# ── The Theme dataclass + THEMES registry ────────────────────────────


class TestThemeDataclass:
    def test_theme_is_frozen(self):
        assert dataclasses.is_dataclass(Theme)
        params = Theme.__dataclass_params__
        assert params.frozen is True

    def test_frozen_theme_rejects_mutation(self):
        with pytest.raises(dataclasses.FrozenInstanceError):
            THEMES["dark"].accent = "#000000"  # type: ignore[misc]

    def test_six_themes_registered(self):
        assert set(THEMES) == {
            "frosted_dark",
            "dark",
            "transparent",
            "frosted_light",
            "light",
            "transparent_light",
        }

    def test_registry_key_matches_theme_name(self):
        for key, theme in THEMES.items():
            assert theme.name == key

    def test_every_theme_carries_full_token_set(self):
        """Every field on the Theme dataclass is populated (non-None)
        for all three themes — the constructor requires every field, so
        a half-authored theme fails loudly, but assert it anyway."""
        fields = [f.name for f in dataclasses.fields(Theme)]
        for theme in THEMES.values():
            for field in fields:
                assert getattr(theme, field) is not None, (
                    f"{theme.name}: field '{field}' is None"
                )

    def test_default_theme_is_frosted_dark(self):
        assert th.DEFAULT_THEME is THEMES["frosted_dark"]
        assert th.DEFAULT_THEME.name == "frosted_dark"


# ── Per-family shared tokens (_DARK_TOKENS / _LIGHT_TOKENS) ───────────

_DARK_NAMES = ("frosted_dark", "dark", "transparent")
_LIGHT_NAMES = ("frosted_light", "light", "transparent_light")


class TestSharedFamilyTokens:
    SHARED_FIELDS = list(th._DARK_TOKENS.keys())

    def test_token_dicts_non_empty(self):
        assert len(th._DARK_TOKENS) > 0
        assert len(th._LIGHT_TOKENS) > 0

    def test_dark_and_light_token_dicts_have_same_keys(self):
        """The two families must cover the identical token set — a key
        in one but not the other means a half-authored family."""
        assert set(th._DARK_TOKENS) == set(th._LIGHT_TOKENS)

    def test_dark_family_shares_dark_token_values(self):
        """The three dark themes splat _DARK_TOKENS, so every key in it
        holds the same value across frosted_dark / dark / transparent."""
        themes = [THEMES[n] for n in _DARK_NAMES]
        for field in self.SHARED_FIELDS:
            values = {getattr(t, field) for t in themes}
            assert len(values) == 1, (
                f"shared token '{field}' diverges across dark themes: {values}"
            )

    def test_light_family_shares_light_token_values(self):
        """Same contract for the light family and _LIGHT_TOKENS."""
        themes = [THEMES[n] for n in _LIGHT_NAMES]
        for field in self.SHARED_FIELDS:
            values = {getattr(t, field) for t in themes}
            assert len(values) == 1, (
                f"shared token '{field}' diverges across light themes: {values}"
            )

    def test_light_family_differs_from_dark(self):
        """The families must actually diverge — light text is not white.
        Guards against a light theme accidentally splatting dark tokens."""
        assert THEMES["light"].text != THEMES["dark"].text

    @pytest.mark.parametrize("field", ["wash_hover", "surface_input", "idle_text"])
    def test_named_dark_tokens_identical(self, field):
        vals = {getattr(THEMES[n], field) for n in _DARK_NAMES}
        assert vals == {th._DARK_TOKENS[field]}

    def test_surface_and_border_depth_not_in_tokens(self):
        """bg_panel + border are per-theme, NOT part of either family's
        token splat — the dark family varies bg_panel across all three."""
        for tokens in (th._DARK_TOKENS, th._LIGHT_TOKENS):
            assert "bg_panel" not in tokens
            assert "border" not in tokens
        dark_panels = {THEMES[n].bg_panel for n in _DARK_NAMES}
        assert len(dark_panels) == 3  # all distinct


# ── The blur field ───────────────────────────────────────────────────


class TestBlurField:
    def test_frosted_dark_blur_true(self):
        assert THEMES["frosted_dark"].blur is True

    def test_dark_blur_false(self):
        assert THEMES["dark"].blur is False

    def test_transparent_blur_false(self):
        assert THEMES["transparent"].blur is False

    def test_only_frosted_variants_request_blur(self):
        blurred = {t.name for t in THEMES.values() if t.blur}
        assert blurred == {"frosted_dark", "frosted_light"}


# ── get_active_theme() ────────────────────────────────────────────────


class TestGetActiveTheme:
    def test_returns_theme_matching_theme_mode(self, clean_theme_settings):
        for name in ("frosted_dark", "dark", "transparent"):
            _set_theme_settings(theme_mode=name, accent_color="")
            assert th.get_active_theme().name == name

    def test_unknown_mode_falls_back_to_default(self, clean_theme_settings):
        _set_theme_settings(theme_mode="chartreuse_neon", accent_color="")
        assert th.get_active_theme().name == th.DEFAULT_THEME.name

    def test_default_accent_returns_base_unchanged(self, clean_theme_settings):
        """When accent_color equals the theme default, the base theme
        object is returned verbatim (no replace())."""
        _set_theme_settings(theme_mode="dark", accent_color=th._DEFAULT_ACCENT)
        active = th.get_active_theme()
        assert active is THEMES["dark"]

    def test_empty_accent_returns_base_unchanged(self, clean_theme_settings):
        _set_theme_settings(theme_mode="dark", accent_color="")
        active = th.get_active_theme()
        assert active is THEMES["dark"]

    def test_accent_override_applied(self, clean_theme_settings):
        _set_theme_settings(theme_mode="dark", accent_color="#112233")
        active = th.get_active_theme()
        assert active.accent == "#112233"
        # accent_deep is the darkened variant.
        assert active.accent_deep == th._darken("#112233")
        # border_accent uses the rgba form with the theme's alpha.
        assert active.border_accent == "rgba(17,34,51,0.45)"  # dark = 0.45

    def test_accent_override_preserves_other_tokens(self, clean_theme_settings):
        _set_theme_settings(theme_mode="frosted_dark", accent_color="#abcdef")
        active = th.get_active_theme()
        base = THEMES["frosted_dark"]
        # Non-accent tokens unchanged.
        assert active.bg == base.bg
        assert active.wash_hover == base.wash_hover
        assert active.blur == base.blur
        # frosted_dark uses 0.35 alpha for border_accent.
        assert active.border_accent == "rgba(171,205,239,0.35)"

    def test_malformed_accent_falls_back_to_base(self, clean_theme_settings):
        """A non-hex accent string must not raise — get_active_theme
        catches ValueError/IndexError and returns the base theme."""
        for bad in ("not-a-hex", "#zzz", "#12", "garbage"):
            _set_theme_settings(theme_mode="transparent", accent_color=bad)
            active = th.get_active_theme()
            # Falls back to the base transparent theme.
            assert active.name == "transparent"
            assert active.accent == THEMES["transparent"].accent

    def test_get_active_theme_never_raises(self, clean_theme_settings):
        for bad in ("", "  ", "#", "#xyzxyz", "rgb(1,2,3)"):
            _set_theme_settings(theme_mode="dark", accent_color=bad)
            th.get_active_theme()  # must not raise


# ── ink_alpha() ───────────────────────────────────────────────────────


class TestInkAlpha:
    def test_dark_theme_resolves_to_white_rgba(self, qapp):
        """On the dark themes ink_alpha(a) is value-identical to the
        old hardcoded rgba(255,255,255,a) literal — this identity is
        what makes the literal-tokenization safe."""
        from modules import ui_helpers

        # ui_helpers.TEXT is the dark-theme white by default.
        assert ui_helpers.TEXT == "#ffffff"
        for a in (0.04, 0.1, 0.5, 1.0):
            assert th.ink_alpha(a) == f"rgba(255,255,255,{a})"

    def test_ink_alpha_uses_live_text_token(self, qapp, monkeypatch):
        """ink_alpha reads the live ui_helpers.TEXT token."""
        from modules import ui_helpers

        monkeypatch.setattr(ui_helpers, "TEXT", "#102030")
        assert th.ink_alpha(0.5) == "rgba(16,32,48,0.5)"

    def test_ink_alpha_never_raises_on_bad_text(self, qapp, monkeypatch):
        """A malformed TEXT token must not take down QSS construction —
        ink_alpha falls back to white."""
        from modules import ui_helpers

        monkeypatch.setattr(ui_helpers, "TEXT", "not-a-color")
        assert th.ink_alpha(0.3) == "rgba(255,255,255,0.3)"

    def test_ink_alpha_returns_string(self, qapp):
        assert isinstance(th.ink_alpha(0.2), str)


# ── _hex_to_rgb / _darken ─────────────────────────────────────────────


class TestHexHelpers:
    def test_hex_to_rgb_basic(self):
        assert th._hex_to_rgb("#ffffff") == (255, 255, 255)
        assert th._hex_to_rgb("#000000") == (0, 0, 0)
        assert th._hex_to_rgb("#102030") == (16, 32, 48)

    def test_hex_to_rgb_without_hash(self):
        assert th._hex_to_rgb("abcdef") == (171, 205, 239)

    def test_hex_to_rgb_raises_on_bad_input(self):
        """get_active_theme relies on _hex_to_rgb raising ValueError /
        IndexError on garbage so it can fall back."""
        with pytest.raises((ValueError, IndexError)):
            th._hex_to_rgb("#zzzzzz")
        with pytest.raises((ValueError, IndexError)):
            th._hex_to_rgb("#12")

    def test_darken_default_factor(self):
        # #ffffff * 0.85 -> floor(216.75) = 216 = 0xd8
        assert th._darken("#ffffff") == "#d8d8d8"

    def test_darken_custom_factor(self):
        assert th._darken("#ffffff", 0.5) == "#7f7f7f"

    def test_darken_black_stays_black(self):
        assert th._darken("#000000") == "#000000"

    def test_darken_round_trips_through_hex(self):
        out = th._darken("#967de1")
        assert out.startswith("#") and len(out) == 7
        # Re-parseable.
        th._hex_to_rgb(out)

    def test_border_accent_for(self):
        assert th._border_accent_for("#102030", 0.5) == "rgba(16,32,48,0.5)"


# ── ui_helpers._hex_to_rgb_safe ───────────────────────────────────────


class TestHexToRgbSafe:
    def test_valid_hex(self, qapp):
        from modules import ui_helpers

        assert ui_helpers._hex_to_rgb_safe("#102030") == (16, 32, 48)

    def test_bad_input_falls_back_to_grey(self, qapp):
        from modules import ui_helpers

        for bad in ("not-a-color", "#zz", "", "rgb(1,2,3)", "#12"):
            assert ui_helpers._hex_to_rgb_safe(bad) == (128, 128, 128)

    def test_never_raises(self, qapp):
        from modules import ui_helpers

        for value in ("#ffffff", "garbage", "", "#abc"):
            ui_helpers._hex_to_rgb_safe(value)  # must not raise
