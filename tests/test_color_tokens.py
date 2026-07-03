"""Tests for jellytoast.color_tokens — the central registry +
override mechanism for UI colors.

Pattern note: tests that mutate module-level globals
(jellytoast.ui_helpers.ACCENT, etc.) use the addCleanup-style restore
so a test failure doesn't leak overrides into subsequent tests.
The QSettings store is isolated via the ``isolated_settings``
conftest fixture.
"""

from __future__ import annotations

import importlib

import pytest

from jellytoast import color_tokens as ct

# ── Fixture: snapshot + restore every token's default after each test ──


@pytest.fixture
def restore_tokens():
    """Snapshot every token's current module value + clean any
    debug/colors/* QSettings entries before AND after the test.
    QSettings test-mode shares one file process-wide, so isolation
    has to be explicit."""
    from PySide6.QtCore import QSettings

    def _wipe_qs():
        s = QSettings()
        for name in ct.TOKENS:
            s.remove(f"debug/colors/{name}")

    _wipe_qs()
    snapshot: dict[str, object] = {}
    for name, token in ct.TOKENS.items():
        module = importlib.import_module(token.module)
        snapshot[name] = getattr(module, name)
    yield
    for name, value in snapshot.items():
        module = importlib.import_module(ct.TOKENS[name].module)
        setattr(module, name, value)
    _wipe_qs()


# ── Registry shape ──────────────────────────────────────────────────────


class TestRegistryShape:
    def test_tokens_dict_non_empty(self):
        assert len(ct.TOKENS) > 0

    def test_every_token_has_required_fields(self):
        for name, token in ct.TOKENS.items():
            assert token.name == name, (
                f"Token key '{name}' doesn't match its name field '{token.name}'"
            )
            assert token.default is not None, f"{name}: default missing"
            assert token.kind in {"hex", "rgba", "tuple_rgba"}, (
                f"{name}: invalid kind '{token.kind}'"
            )
            assert token.category in {
                "accent",
                "text",
                "surface",
                "highlight",
                "input",
                "slider",
                "destructive",
            }, f"{name}: invalid category '{token.category}'"
            assert token.description, f"{name}: description missing"
            assert token.module.startswith("jellytoast."), (
                f"{name}: module path '{token.module}' should start with 'jellytoast.'"
            )

    def test_every_token_resolves_to_its_module(self):
        """Each token's module should expose a global with the token's
        name — that's what apply_override mutates."""
        for name, token in ct.TOKENS.items():
            module = importlib.import_module(token.module)
            assert hasattr(module, name), (
                f"{name}: module {token.module} has no attribute '{name}'"
            )

    def test_default_kind_matches_value_shape(self):
        for name, token in ct.TOKENS.items():
            if token.kind == "hex":
                assert isinstance(token.default, str) and token.default.startswith(
                    "#"
                ), f"{name}: hex token default should be #-prefixed string"
            elif token.kind == "rgba":
                assert isinstance(
                    token.default, str
                ) and "rgba" in token.default, (
                    f"{name}: rgba token default should be a 'rgba(...)' string"
                )
            elif token.kind == "tuple_rgba":
                assert isinstance(
                    token.default, tuple
                ) and len(token.default) == 4, (
                    f"{name}: tuple_rgba token default should be a 4-tuple"
                )


# ── get_default / get_current ────────────────────────────────────────────


class TestAccessors:
    def test_get_default_returns_registry_value(self):
        assert ct.get_default("ACCENT") == "#967de1"

    def test_get_current_reads_live_module_value(self, restore_tokens):
        from jellytoast import ui_helpers

        ui_helpers.ACCENT = "#abcdef"
        assert ct.get_current("ACCENT") == "#abcdef"

    def test_get_default_unaffected_by_module_mutation(self, restore_tokens):
        from jellytoast import ui_helpers

        ui_helpers.ACCENT = "#abcdef"
        assert ct.get_default("ACCENT") == "#967de1"


# ── apply_override ──────────────────────────────────────────────────────


class TestApplyOverride:
    def test_apply_mutates_module_global(self, restore_tokens, isolated_settings):
        from jellytoast import ui_helpers

        ct.apply_override("ACCENT", "#ff0000")
        assert ui_helpers.ACCENT == "#ff0000"

    def test_accent_cascade_deep_matches_darken(self, restore_tokens, isolated_settings):
        # The Colors-page slider cascade (_cascade_accent_family) must derive
        # ACCENT_DEEP identically to the accent-picker path (theme._darken).
        # A local int(round(...)) diverged from _darken's int(...) truncation
        # by 1 on some channels, so the two UIs produced different deeps.
        from jellytoast import theme as _t
        from jellytoast import ui_helpers

        for _name, hexv in [*_t.ACCENT_PRESETS, ("X", "#ff0000")]:
            ct.apply_override("ACCENT", hexv)
            assert ui_helpers.ACCENT_DEEP == _t._darken(hexv), hexv

    def test_apply_persists_to_qsettings(self, restore_tokens, isolated_settings):
        from PySide6.QtCore import QSettings

        ct.apply_override("ACCENT", "#ff0000")
        s = QSettings()
        assert s.contains("debug/colors/ACCENT")
        # Stored as JSON.
        import json
        assert json.loads(s.value("debug/colors/ACCENT", type=str)) == "#ff0000"

    def test_apply_emits_theme_changed(self, restore_tokens, isolated_settings):
        from jellytoast.player_state import PlayerBus

        bus = PlayerBus.get()
        calls: list = []
        bus.theme_changed.connect(lambda: calls.append(1))
        ct.apply_override("WASH_HOVER", "rgba(10,20,30,0.5)")
        assert len(calls) == 1
        bus.theme_changed.disconnect()

    def test_apply_persist_false_skips_qsettings_write(
        self, restore_tokens, isolated_settings
    ):
        from PySide6.QtCore import QSettings

        ct.apply_override("ACCENT", "#ff0000", persist=False)
        # Module global still updated.
        from jellytoast import ui_helpers
        assert ui_helpers.ACCENT == "#ff0000"
        # But QSettings entry NOT written.
        assert not QSettings().contains("debug/colors/ACCENT")

    def test_apply_tuple_value_round_trips(self, restore_tokens, isolated_settings):
        from jellytoast import ui_helpers

        ct.apply_override("BODY_COLOR", (10, 20, 30, 200))
        assert ui_helpers.BODY_COLOR == (10, 20, 30, 200)


# ── reset / reset_all ──────────────────────────────────────────────────


class TestReset:
    def test_reset_restores_module_default(self, restore_tokens, isolated_settings):
        from jellytoast import ui_helpers

        ct.apply_override("ACCENT", "#ff0000")
        assert ui_helpers.ACCENT == "#ff0000"
        ct.reset("ACCENT")
        assert ui_helpers.ACCENT == ct.get_default("ACCENT")

    def test_reset_removes_qsettings_entry(self, restore_tokens, isolated_settings):
        from PySide6.QtCore import QSettings

        ct.apply_override("ACCENT", "#ff0000")
        assert QSettings().contains("debug/colors/ACCENT")
        ct.reset("ACCENT")
        assert not QSettings().contains("debug/colors/ACCENT")

    def test_reset_all_clears_every_override(
        self, restore_tokens, isolated_settings
    ):
        from PySide6.QtCore import QSettings

        ct.apply_override("ACCENT", "#ff0000")
        ct.apply_override("TEXT", "#aabbcc")
        ct.apply_override("WASH_HOVER", "rgba(1,2,3,0.5)")
        ct.reset_all()
        s = QSettings()
        for name in ("ACCENT", "TEXT", "WASH_HOVER"):
            assert not s.contains(f"debug/colors/{name}"), (
                f"{name} override should have been cleared"
            )


# ── load_persisted_overrides ────────────────────────────────────────────


class TestLoadPersistedOverrides:
    def test_loads_string_override(self, restore_tokens, isolated_settings):
        import json

        from PySide6.QtCore import QSettings

        # Seed QSettings as if a previous run had set an override.
        s = QSettings()
        s.setValue("debug/colors/ACCENT", json.dumps("#deadbe"))
        ct.load_persisted_overrides()
        from jellytoast import ui_helpers

        assert ui_helpers.ACCENT == "#deadbe"

    def test_loads_tuple_override(self, restore_tokens, isolated_settings):
        import json

        from PySide6.QtCore import QSettings

        s = QSettings()
        s.setValue(
            "debug/colors/BODY_COLOR", json.dumps([99, 88, 77, 240])
        )
        ct.load_persisted_overrides()
        from jellytoast import ui_helpers

        assert ui_helpers.BODY_COLOR == (99, 88, 77, 240)

    def test_malformed_override_skipped_silently(
        self, restore_tokens, isolated_settings
    ):
        from PySide6.QtCore import QSettings

        s = QSettings()
        s.setValue("debug/colors/ACCENT", "{not json")
        # Should not raise. ACCENT should be untouched.
        from jellytoast import ui_helpers

        before = ui_helpers.ACCENT
        ct.load_persisted_overrides()
        assert ui_helpers.ACCENT == before

    def test_no_override_for_token_leaves_default(
        self, restore_tokens, isolated_settings
    ):
        # No QSettings keys set.
        ct.load_persisted_overrides()
        from jellytoast import ui_helpers

        assert ui_helpers.ACCENT == ct.get_default("ACCENT")


# ── tokens_by_category ──────────────────────────────────────────────────


class TestTokensByCategory:
    def test_returns_non_empty_buckets(self):
        groups = ct.tokens_by_category()
        assert len(groups) > 0

    def test_every_token_appears_exactly_once(self):
        groups = ct.tokens_by_category()
        seen: set[str] = set()
        for tokens in groups.values():
            for token in tokens:
                assert token.name not in seen, (
                    f"{token.name} appears in multiple categories"
                )
                seen.add(token.name)
        assert seen == set(ct.TOKENS)

    def test_categories_present_in_label_map(self):
        groups = ct.tokens_by_category()
        for cat in groups:
            assert cat in ct.CATEGORY_LABELS, (
                f"Category '{cat}' has tokens but no label in CATEGORY_LABELS"
            )


# ── Defaults track the live frosted-dark theme (review P0-4) ────────────────


def _norm_color(value: object, kind: str):
    """Normalise a token value to a comparable form so float-precision /
    whitespace differences (``rgba(255, 255, 255, 0.15000000000000002)`` vs
    ``rgba(255,255,255,0.15)``) don't cause spurious mismatches."""
    from jellytoast.ui_helpers import _parse_qss_color

    if kind == "tuple_rgba":
        t = tuple(value)  # type: ignore[arg-type]
        return (t[0], t[1], t[2], int(t[3]))
    if kind == "hex":
        return _parse_qss_color(str(value))[:3]
    r, g, b, a = _parse_qss_color(str(value))
    return (r, g, b, round(a, 3))


class TestDefaultsTrackLiveTheme:
    """Regression: the hardcoded token defaults had drifted from the live
    frosted-dark values, so reset()/Reset wrote wrong colors. Pin the
    invariant — under the default theme with no overrides, each token's
    default equals its live module value (reset is a true no-op).

    Excluded: the accent family is user-configurable (the live value diverges
    once the user picks a custom accent); body fills are blur-state-dependent
    (alpha 172 with blur / ~236 on the opaque fallback), so they get an RGB +
    valid-alpha check instead of exact equality.
    """

    ACCENT = {"ACCENT", "ACCENT_DEEP", "BORDER_ACCENT"}
    BODY = {"BODY_COLOR", "MINI_BODY_COLOR", "DIALOG_BODY_COLOR"}

    def test_non_accent_non_body_defaults_equal_live(
        self, qapp, restore_tokens, isolated_settings
    ):
        import jellytoast.ui_helpers as uih

        uih.refresh_theme()  # re-derive globals from clean settings (frosted-dark)
        drift = []
        for name, token in ct.TOKENS.items():
            if name in self.ACCENT or name in self.BODY:
                continue
            expected = ct.get_default(name)
            if name == "POPUP_OPAQUE_FILL":
                # the live value is platform-derived: near-opaque on macOS
                # (no app-controllable popup blur there); no-op elsewhere
                expected = uih._popup_fill_opaque_on_macos(expected)
            if _norm_color(expected, token.kind) != _norm_color(
                ct.get_current(name), token.kind
            ):
                drift.append((name, expected, ct.get_current(name)))
        assert not drift, f"token defaults drifted from the live theme: {drift}"

    def test_body_defaults_match_rgb_with_valid_blur_alpha(
        self, qapp, restore_tokens, isolated_settings
    ):
        import jellytoast.ui_helpers as uih

        uih.refresh_theme()
        for name in self.BODY:
            d = ct.get_default(name)
            live = ct.get_current(name)
            assert tuple(d[:3]) == tuple(live[:3]), f"{name} body RGB drift: {d} vs {live}"
            assert d[3] in (172, 236), f"{name} default alpha {d[3]} is not a valid blur state"
