"""Tests for jellytoast.theme — the semantic-token theme registry.

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

from jellytoast import theme as th
from jellytoast.theme import THEMES, Theme

# ── Fixture: isolate the QSettings keys get_active_theme() reads ──────


def _settings_handle():
    """The exact QSettings handle the get_settings() singleton reads
    through. get_active_theme() resolves get_settings()._s; a write
    from any *other* QSettings handle isn't visible to this one until
    it sync()s (QSettings caches file contents per instance). Writing
    and reading through this single handle keeps the test hermetic
    regardless of suite ordering."""
    from jellytoast.settings import get_settings

    return get_settings()._s


@pytest.fixture
def clean_theme_settings():
    """Wipe every theme axis get_active_theme() reads before AND after the
    test so it sees a clean slate (defaults re-derive from QSettings)."""

    def _wipe():
        s = _settings_handle()
        for k in (
            "ui/theme_mode",
            "ui/accent_color",
            "ui/frosted",
            "ui/theme_family",
            "ui/imported_scheme_json",
            "ui/preset_glass_alpha",
            "ui/jellytoast_glass_alpha",
        ):
            s.remove(k)
        s.sync()

    _wipe()
    yield
    _wipe()


def _set_theme_settings(theme_mode=None, accent_color=None, frosted=None, theme_family=None):
    """Write the theme axes get_active_theme() reads through the same QSettings
    handle get_settings() uses, then sync so the read sees them. ``theme_mode``
    is now the luminance intent (auto/dark/light); ``frosted`` is the orthogonal
    Frosted/Opaque axis (defaults True when unset)."""
    s = _settings_handle()
    if theme_mode is not None:
        s.setValue("ui/theme_mode", theme_mode)
    if accent_color is not None:
        s.setValue("ui/accent_color", accent_color)
    if frosted is not None:
        s.setValue("ui/frosted", frosted)
    if theme_family is not None:
        s.setValue("ui/theme_family", theme_family)
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

    def test_four_themes_registered(self):
        assert set(THEMES) == {
            "frosted_dark",
            "dark",
            "frosted_light",
            "light",
        }

    def test_registry_key_matches_theme_name(self):
        for key, theme in THEMES.items():
            assert theme.name == key

    def test_every_theme_carries_full_token_set(self):
        """Every required field on the Theme dataclass is populated
        (non-None) for all themes — the constructor requires every field, so
        a half-authored theme fails loudly, but assert it anyway.

        `fallback_body_alpha` is deliberately optional (None on non-frosted
        themes — see TestBlurField), so it's excluded here."""
        optional = {"fallback_body_alpha"}
        fields = [
            f.name for f in dataclasses.fields(Theme) if f.name not in optional
        ]
        for theme in THEMES.values():
            for field in fields:
                assert getattr(theme, field) is not None, (
                    f"{theme.name}: field '{field}' is None"
                )

    def test_default_theme_is_frosted_dark(self):
        assert th.DEFAULT_THEME is THEMES["frosted_dark"]
        assert th.DEFAULT_THEME.name == "frosted_dark"


# ── Per-family shared tokens (_DARK_TOKENS / _LIGHT_TOKENS) ───────────

_DARK_NAMES = ("frosted_dark", "dark")
_LIGHT_NAMES = ("frosted_light", "light")


class TestSharedFamilyTokens:
    # popup_opaque_fill is a deliberate per-theme override: frosted
    # themes diverge to a translucent wash (backstopped by compositor
    # blur installed at popup show time), while solid + transparent
    # themes stay opaque. The rest of the family tokens stay locked.
    SHARED_FIELDS = [k for k in th._DARK_TOKENS.keys() if k != "popup_opaque_fill"]

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
        token splat — the dark family varies bg_panel across its members."""
        for tokens in (th._DARK_TOKENS, th._LIGHT_TOKENS):
            assert "bg_panel" not in tokens
            assert "border" not in tokens
        dark_panels = {THEMES[n].bg_panel for n in _DARK_NAMES}
        assert len(dark_panels) == len(_DARK_NAMES)  # all distinct


# ── The blur field ───────────────────────────────────────────────────


class TestBlurField:
    def test_frosted_dark_blur_true(self):
        assert THEMES["frosted_dark"].blur is True

    def test_dark_blur_false(self):
        assert THEMES["dark"].blur is False

    def test_only_frosted_variants_request_blur(self):
        blurred = {t.name for t in THEMES.values() if t.blur}
        assert blurred == {"frosted_dark", "frosted_light"}

    def test_every_blur_theme_defines_a_fallback_alpha(self):
        """The class invariant behind the see-through fix: any theme that
        asks for blur MUST carry a near-opaque fallback alpha, or it would
        render transparent on a box without working blur."""
        for t in THEMES.values():
            if t.blur:
                assert t.fallback_body_alpha is not None, t.name
                assert 0 < t.fallback_body_alpha <= 255

    def test_non_blur_themes_have_no_fallback_alpha(self):
        for t in THEMES.values():
            if not t.blur:
                assert t.fallback_body_alpha is None, t.name

    def test_fallback_is_more_opaque_than_glass(self):
        """Fallback must be MORE opaque than the glass body — the whole
        point is to stop the see-through render when blur is absent."""
        for name in ("frosted_dark", "frosted_light"):
            t = THEMES[name]
            assert t.fallback_body_alpha > t.body_color[3]


# ── body_color_for() — status-driven body alpha ───────────────────────


class TestBodyColorFor:
    def test_active_keeps_glass_alpha(self):
        from jellytoast.blur import BlurStatus

        t = THEMES["frosted_dark"]
        assert th.body_color_for(t, BlurStatus.ACTIVE) == t.body_color

    @pytest.mark.parametrize(
        "status_name", ["UNSUPPORTED", "REQUESTED_UNVERIFIABLE"]
    )
    def test_no_blur_status_swaps_to_fallback_alpha(self, status_name):
        from jellytoast.blur import BlurStatus

        t = THEMES["frosted_dark"]
        status = getattr(BlurStatus, status_name)
        rgba = th.body_color_for(t, status)
        assert rgba[:3] == t.body_color[:3]  # same hue
        assert rgba[3] == t.fallback_body_alpha  # near-opaque alpha

    def test_non_frosted_theme_ignores_status(self):
        from jellytoast.blur import BlurStatus

        for name in ("dark", "light"):
            t = THEMES[name]
            for status in BlurStatus:
                assert th.body_color_for(t, status) == t.body_color

    def test_surfaces_select_the_right_base(self):
        from jellytoast.blur import BlurStatus

        t = THEMES["frosted_dark"]
        for surface, attr in (
            ("main", "body_color"),
            ("mini", "mini_body_color"),
            ("dialog", "dialog_body_color"),
        ):
            base = getattr(t, attr)
            assert th.body_color_for(t, BlurStatus.ACTIVE, surface) == base

    def test_unknown_surface_falls_back_to_main(self):
        from jellytoast.blur import BlurStatus

        t = THEMES["frosted_dark"]
        assert th.body_color_for(
            t, BlurStatus.ACTIVE, "bogus"
        ) == t.body_color


# ── ui_helpers.body_color_tuple() — the shared surface resolver ───────


class TestBodyColorTuple:
    """body_color_tuple is read by the main window, mini player, and every
    frosted dialog, so it's the choke point that keeps them degrading
    together. It resolves the live theme + the cached blur status."""

    def _setup(self, monkeypatch, status, *, frosted=True):
        from jellytoast import blur

        # Dark luminance; Frosted vs Opaque via the orthogonal axis.
        _set_theme_settings(theme_mode="dark", accent_color="", frosted=frosted)
        monkeypatch.setattr(blur, "_FORCE", "")  # ignore JT_BLUR_FORCE
        monkeypatch.setattr(blur, "_status_cache", status)

    def test_frosted_glass_when_active(self, clean_theme_settings, monkeypatch):
        from jellytoast import ui_helpers
        from jellytoast.blur import BlurStatus

        self._setup(monkeypatch, BlurStatus.ACTIVE, frosted=True)
        assert ui_helpers.body_color_tuple("main") == (18, 18, 18, 172)

    def test_frosted_fallback_when_not_active(self, clean_theme_settings, monkeypatch):
        from jellytoast import ui_helpers
        from jellytoast.blur import BlurStatus

        self._setup(monkeypatch, BlurStatus.UNSUPPORTED, frosted=True)
        # Every body surface lands on the near-opaque fallback, not 172.
        assert ui_helpers.body_color_tuple("main") == (18, 18, 18, 236)
        assert ui_helpers.body_color_tuple("mini")[3] == 236
        assert ui_helpers.body_color_tuple("dialog")[3] == 236

    def test_non_frosted_ignores_status(self, clean_theme_settings, monkeypatch):
        from jellytoast import ui_helpers
        from jellytoast.blur import BlurStatus

        self._setup(monkeypatch, BlurStatus.UNSUPPORTED, frosted=False)
        assert ui_helpers.body_color_tuple("main") == THEMES["dark"].body_color


# ── get_active_theme() ────────────────────────────────────────────────


class TestGetActiveTheme:
    def test_returns_theme_matching_theme_mode(self, clean_theme_settings):
        # (luminance mode, frosted) composes the built-in theme name.
        for mode, frosted, name in (
            ("dark", True, "frosted_dark"),
            ("dark", False, "dark"),
            ("light", True, "frosted_light"),
            ("light", False, "light"),
        ):
            _set_theme_settings(theme_mode=mode, frosted=frosted, accent_color="")
            assert th.get_active_theme().name == name

    def test_unknown_mode_falls_back_to_default(self, clean_theme_settings):
        _set_theme_settings(theme_mode="chartreuse_neon", accent_color="")
        assert th.get_active_theme().name == th.DEFAULT_THEME.name

    def test_default_accent_returns_base_unchanged(self, clean_theme_settings):
        """When accent_color equals the theme default, the base theme
        object is returned verbatim (no replace())."""
        _set_theme_settings(
            theme_mode="dark", frosted=False, accent_color=th._DEFAULT_ACCENT
        )
        active = th.get_active_theme()
        assert active is THEMES["dark"]

    def test_empty_accent_returns_base_unchanged(self, clean_theme_settings):
        _set_theme_settings(theme_mode="dark", frosted=False, accent_color="")
        active = th.get_active_theme()
        assert active is THEMES["dark"]

    def test_accent_override_applied(self, clean_theme_settings):
        _set_theme_settings(theme_mode="dark", frosted=False, accent_color="#112233")
        active = th.get_active_theme()
        assert active.accent == "#112233"
        # accent_deep is the darkened variant.
        assert active.accent_deep == th._darken("#112233")
        # border_accent uses the rgba form with the theme's alpha.
        assert active.border_accent == "rgba(17,34,51,0.45)"  # dark = 0.45

    def test_accent_override_preserves_other_tokens(self, clean_theme_settings):
        _set_theme_settings(theme_mode="dark", frosted=True, accent_color="#abcdef")
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
            _set_theme_settings(theme_mode="dark", frosted=True, accent_color=bad)
            active = th.get_active_theme()
            # Falls back to the base theme.
            assert active.name == "frosted_dark"
            assert active.accent == THEMES["frosted_dark"].accent

    def test_get_active_theme_never_raises(self, clean_theme_settings):
        for bad in ("", "  ", "#", "#xyzxyz", "rgb(1,2,3)"):
            _set_theme_settings(theme_mode="dark", accent_color=bad)
            th.get_active_theme()  # must not raise


class TestPresetBodyTint:
    """A preset family recolours the painted body with its background (base00):
    get_active_theme overlays the persisted BODY_COLOR override's RGB but keeps
    the resolved base theme's alpha, so Frosted/Opaque still owns opacity."""

    def test_preset_tints_body_keeps_base_alpha(self, clean_theme_settings, qapp):
        import jellytoast.color_tokens as ct

        try:
            _set_theme_settings(
                theme_mode="dark", theme_family="catppuccin", frosted=True, accent_color=""
            )
            ct.apply_override("BODY_COLOR", (30, 30, 46, 172))
            active = th.get_active_theme()
            # RGB from the override; alpha is the DEEPER preset glass (truer to
            # base00 than jellytoast's own 172), not the stored override alpha.
            assert active.body_color == (30, 30, 46, th._glass_alpha(True, True))
            assert active.body_color[3] > 172  # deeper than jellytoast's glass
            # Flip to Opaque → base "dark" (alpha 255), tint RGB unchanged.
            _set_theme_settings(frosted=False)
            active2 = th.get_active_theme()
            assert active2.body_color[:3] == (30, 30, 46)
            assert active2.body_color[3] == THEMES["dark"].body_color[3]
        finally:
            ct.reset_all()

    def test_jellytoast_never_reads_body_override(self, clean_theme_settings, qapp):
        import jellytoast.color_tokens as ct

        try:
            # A stray BODY_COLOR override must NOT tint the built-in jellytoast body.
            ct.apply_override("BODY_COLOR", (1, 2, 3, 99))
            _set_theme_settings(theme_mode="dark", frosted=True, theme_family="")
            active = th.get_active_theme()
            assert active.body_color == THEMES["frosted_dark"].body_color
        finally:
            ct.reset_all()

    def test_jellytoast_glass_default_returns_base(self, clean_theme_settings):
        # No jellytoast override → get_active_theme returns the base theme verbatim
        # (its own 172 glass), the airier default the user likes.
        _set_theme_settings(
            theme_mode="dark", frosted=True, theme_family="", accent_color=""
        )
        _settings_handle().remove("ui/jellytoast_glass_alpha")
        _settings_handle().sync()
        active = th.get_active_theme()
        assert active is THEMES["frosted_dark"]

    def test_jellytoast_glass_override_applies(self, clean_theme_settings):
        # A jellytoast override retints only the frosted body ALPHA (RGB stays the
        # theme's own), independent of presets.
        _set_theme_settings(
            theme_mode="dark", frosted=True, theme_family="", accent_color=""
        )
        _settings_handle().setValue("ui/jellytoast_glass_alpha", 160)
        _settings_handle().sync()
        active = th.get_active_theme()
        assert active.body_color == (18, 18, 18, 160)
        assert active.mini_body_color[3] == 160
        # Opaque jellytoast ignores the override (solid body).
        _set_theme_settings(frosted=False)
        assert th.get_active_theme().body_color == THEMES["dark"].body_color
        _settings_handle().remove("ui/jellytoast_glass_alpha")
        _settings_handle().sync()


class TestThemeAxesMigration:
    """The old single ui/theme_mode (frosted_dark / dark / … / transparent*) is
    split into the orthogonal (theme_mode ∈ auto/dark/light, frosted,
    theme_family) axes by settings_migration._migrate_theme_axes. The dropped
    Transparent aliases fold into their frosted family."""

    def _run(self, **kv):
        from jellytoast.settings_migration import (
            _THEME_AXES_MARKER,
            _migrate_theme_axes,
        )

        s = _settings_handle()
        s.remove(_THEME_AXES_MARKER)  # force a re-run in the shared test QSettings
        for k in ("ui/theme_mode", "ui/frosted", "ui/theme_family", "ui/last_preset_name"):
            s.remove(k)
        for k, v in kv.items():
            s.setValue(k, v)
        s.sync()
        _migrate_theme_axes(s)
        return (
            s.value("ui/theme_mode", type=str),
            s.value("ui/frosted", type=bool),
            s.value("ui/theme_family", type=str),
        )

    def test_transparent_folds_to_frosted_dark(self, clean_theme_settings):
        assert self._run(**{"ui/theme_mode": "transparent"}) == ("dark", True, "")

    def test_transparent_light_folds_to_frosted_light(self, clean_theme_settings):
        assert self._run(**{"ui/theme_mode": "transparent_light"}) == ("light", True, "")

    def test_frosted_dark_splits(self, clean_theme_settings):
        assert self._run(**{"ui/theme_mode": "frosted_dark"}) == ("dark", True, "")

    def test_solid_dark_splits(self, clean_theme_settings):
        assert self._run(**{"ui/theme_mode": "dark"}) == ("dark", False, "")

    def test_preset_maps_to_family(self, clean_theme_settings):
        assert self._run(
            **{"ui/theme_mode": "frosted_light", "ui/last_preset_name": "Catppuccin Latte"}
        ) == ("light", True, "catppuccin")

    def test_active_theme_after_transparent_migration_is_frosted(self, clean_theme_settings):
        self._run(**{"ui/theme_mode": "transparent"})
        assert th.get_active_theme().name == "frosted_dark"


# ── ink_alpha() ───────────────────────────────────────────────────────


class TestInkAlpha:
    def test_dark_theme_resolves_to_white_rgba(self, qapp):
        """On the dark themes ink_alpha(a) is value-identical to the
        old hardcoded rgba(255,255,255,a) literal — this identity is
        what makes the literal-tokenization safe."""
        from jellytoast import ui_helpers

        # ui_helpers.TEXT is the dark-theme white by default.
        assert ui_helpers.TEXT == "#ffffff"
        for a in (0.04, 0.1, 0.5, 1.0):
            assert th.ink_alpha(a) == f"rgba(255,255,255,{a})"

    def test_ink_alpha_uses_live_text_token(self, qapp, monkeypatch):
        """ink_alpha reads the live ui_helpers.TEXT token."""
        from jellytoast import ui_helpers

        monkeypatch.setattr(ui_helpers, "TEXT", "#102030")
        assert th.ink_alpha(0.5) == "rgba(16,32,48,0.5)"

    def test_ink_alpha_never_raises_on_bad_text(self, qapp, monkeypatch):
        """A malformed TEXT token must not take down QSS construction —
        ink_alpha falls back to white."""
        from jellytoast import ui_helpers

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
        from jellytoast import ui_helpers

        assert ui_helpers._hex_to_rgb_safe("#102030") == (16, 32, 48)

    def test_bad_input_falls_back_to_grey(self, qapp):
        from jellytoast import ui_helpers

        for bad in ("not-a-color", "#zz", "", "rgb(1,2,3)", "#12"):
            assert ui_helpers._hex_to_rgb_safe(bad) == (128, 128, 128)

    def test_never_raises(self, qapp):
        from jellytoast import ui_helpers

        for value in ("#ffffff", "garbage", "", "#abc"):
            ui_helpers._hex_to_rgb_safe(value)  # must not raise
