"""Tests for modules.color_tokens — the central registry +
override mechanism for UI colors.

Pattern note: tests that mutate module-level globals
(modules.ui_helpers.ACCENT, etc.) use the addCleanup-style restore
so a test failure doesn't leak overrides into subsequent tests.
The QSettings store is isolated via the ``isolated_settings``
conftest fixture.
"""

from __future__ import annotations

import importlib

import pytest

from modules import color_tokens as ct


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
            assert token.module.startswith("modules."), (
                f"{name}: module path '{token.module}' should start with 'modules.'"
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
        from modules import ui_helpers

        ui_helpers.ACCENT = "#abcdef"
        assert ct.get_current("ACCENT") == "#abcdef"

    def test_get_default_unaffected_by_module_mutation(self, restore_tokens):
        from modules import ui_helpers

        ui_helpers.ACCENT = "#abcdef"
        assert ct.get_default("ACCENT") == "#967de1"


# ── apply_override ──────────────────────────────────────────────────────


class TestApplyOverride:
    def test_apply_mutates_module_global(self, restore_tokens, isolated_settings):
        from modules import ui_helpers

        ct.apply_override("ACCENT", "#ff0000")
        assert ui_helpers.ACCENT == "#ff0000"

    def test_apply_persists_to_qsettings(self, restore_tokens, isolated_settings):
        from PySide6.QtCore import QSettings

        ct.apply_override("ACCENT", "#ff0000")
        s = QSettings()
        assert s.contains("debug/colors/ACCENT")
        # Stored as JSON.
        import json
        assert json.loads(s.value("debug/colors/ACCENT", type=str)) == "#ff0000"

    def test_apply_emits_theme_changed(self, restore_tokens, isolated_settings):
        from modules.player_state import PlayerBus

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
        from modules import ui_helpers
        assert ui_helpers.ACCENT == "#ff0000"
        # But QSettings entry NOT written.
        assert not QSettings().contains("debug/colors/ACCENT")

    def test_apply_tuple_value_round_trips(self, restore_tokens, isolated_settings):
        from modules import ui_helpers

        ct.apply_override("BODY_COLOR", (10, 20, 30, 200))
        assert ui_helpers.BODY_COLOR == (10, 20, 30, 200)


# ── reset / reset_all ──────────────────────────────────────────────────


class TestReset:
    def test_reset_restores_module_default(self, restore_tokens, isolated_settings):
        from modules import ui_helpers

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
        from PySide6.QtCore import QSettings
        import json

        # Seed QSettings as if a previous run had set an override.
        s = QSettings()
        s.setValue("debug/colors/ACCENT", json.dumps("#deadbe"))
        ct.load_persisted_overrides()
        from modules import ui_helpers

        assert ui_helpers.ACCENT == "#deadbe"

    def test_loads_tuple_override(self, restore_tokens, isolated_settings):
        from PySide6.QtCore import QSettings
        import json

        s = QSettings()
        s.setValue(
            "debug/colors/BODY_COLOR", json.dumps([99, 88, 77, 240])
        )
        ct.load_persisted_overrides()
        from modules import ui_helpers

        assert ui_helpers.BODY_COLOR == (99, 88, 77, 240)

    def test_malformed_override_skipped_silently(
        self, restore_tokens, isolated_settings
    ):
        from PySide6.QtCore import QSettings

        s = QSettings()
        s.setValue("debug/colors/ACCENT", "{not json")
        # Should not raise. ACCENT should be untouched.
        from modules import ui_helpers

        before = ui_helpers.ACCENT
        ct.load_persisted_overrides()
        assert ui_helpers.ACCENT == before

    def test_no_override_for_token_leaves_default(
        self, restore_tokens, isolated_settings
    ):
        # No QSettings keys set.
        ct.load_persisted_overrides()
        from modules import ui_helpers

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
