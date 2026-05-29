"""Phase 5 tests for the color editor — palette export/import,
named palette library, and the page's HSV↔token conversion helpers.

QSettings isolation pattern matches test_color_tokens.py: the
``restore_palettes`` fixture wipes ``debug/color_palettes/*`` AND
``debug/colors/*`` before and after each test so the process-wide
QSettings store doesn't leak between tests.
"""

from __future__ import annotations

import importlib
import json

import pytest

from modules import color_tokens as ct

# ── Fixtures ────────────────────────────────────────────────────────────


@pytest.fixture
def restore_palettes(isolated_settings):
    """Wipe per-token overrides AND named palettes before + after the
    test. Also snapshot every token's live module value and restore
    it so a test that mutates ``ui_helpers.ACCENT`` doesn't leak."""
    from PySide6.QtCore import QSettings

    def _wipe():
        s = QSettings()
        # Per-token override store.
        for name in ct.TOKENS:
            s.remove(f"debug/colors/{name}")
        # Named-palette store — collect first since iteration over
        # allKeys + removal mid-iteration is fragile.
        prefix = "debug/color_palettes/"
        for k in list(s.allKeys()):
            if k.startswith(prefix):
                s.remove(k)

    _wipe()
    snapshot: dict[str, object] = {}
    for name, token in ct.TOKENS.items():
        module = importlib.import_module(token.module)
        snapshot[name] = getattr(module, name)
    yield
    for name, value in snapshot.items():
        module = importlib.import_module(ct.TOKENS[name].module)
        setattr(module, name, value)
    _wipe()


# ── Palette export / import ─────────────────────────────────────────────


class TestExportPalette:
    def test_export_has_version_field(self, restore_palettes):
        p = ct.export_palette()
        assert p.get("version") == ct.PALETTE_VERSION

    def test_export_default_name_is_empty(self, restore_palettes):
        p = ct.export_palette()
        assert p.get("name") == ""

    def test_export_explicit_name_passed_through(self, restore_palettes):
        p = ct.export_palette(name="My Theme")
        assert p["name"] == "My Theme"

    def test_export_includes_every_token(self, restore_palettes):
        p = ct.export_palette()
        assert set(p["tokens"].keys()) == set(ct.TOKENS.keys())

    def test_export_reflects_current_overrides(self, restore_palettes):
        ct.apply_override("ACCENT", "#abcdef")
        p = ct.export_palette()
        assert p["tokens"]["ACCENT"] == "#abcdef"

    def test_export_reflects_tuple_values(self, restore_palettes):
        ct.apply_override("BODY_COLOR", (5, 10, 15, 220))
        p = ct.export_palette()
        assert p["tokens"]["BODY_COLOR"] == (5, 10, 15, 220)


class TestImportPalette:
    def test_import_applies_known_tokens(self, restore_palettes):
        from modules import ui_helpers

        palette = {
            "version": 1,
            "name": "test",
            "tokens": {"ACCENT": "#ff0000", "TEXT": "#aabbcc"},
        }
        applied = ct.import_palette(palette)
        assert applied == 2
        assert ui_helpers.ACCENT == "#ff0000"
        assert ui_helpers.TEXT == "#aabbcc"

    def test_import_skips_unknown_tokens(self, restore_palettes):
        palette = {
            "version": 1,
            "tokens": {
                "ACCENT": "#ff0000",
                "BOGUS_TOKEN_NAME": "#000000",
            },
        }
        applied = ct.import_palette(palette)
        # Only ACCENT was applied; BOGUS_TOKEN_NAME skipped silently.
        assert applied == 1

    def test_import_converts_lists_back_to_tuples_for_tuple_kind(
        self, restore_palettes
    ):
        from modules import ui_helpers

        # JSON round-trip turns tuples into lists; importer should
        # convert back.
        palette = {
            "version": 1,
            "tokens": {"BODY_COLOR": [100, 110, 120, 200]},
        }
        ct.import_palette(palette)
        assert ui_helpers.BODY_COLOR == (100, 110, 120, 200)

    def test_import_raises_on_non_dict(self, restore_palettes):
        with pytest.raises(ValueError):
            ct.import_palette([1, 2, 3])  # type: ignore[arg-type]

    def test_import_raises_when_tokens_missing(self, restore_palettes):
        with pytest.raises(ValueError):
            ct.import_palette({"version": 1, "name": "x"})

    def test_import_raises_when_tokens_not_dict(self, restore_palettes):
        with pytest.raises(ValueError):
            ct.import_palette({"version": 1, "tokens": []})  # type: ignore[arg-type]

    def test_export_then_import_round_trips(self, restore_palettes):
        from modules import ui_helpers

        ct.apply_override("ACCENT", "#abcdef")
        ct.apply_override("BODY_COLOR", (5, 10, 15, 220))
        exported = ct.export_palette()
        # Simulate disk persistence via JSON.
        serialized = json.dumps(exported)
        loaded = json.loads(serialized)
        # Reset to defaults so we know the import did the work.
        ct.reset_all()
        ct.import_palette(loaded)
        assert ui_helpers.ACCENT == "#abcdef"
        assert ui_helpers.BODY_COLOR == (5, 10, 15, 220)


# ── Named palette library ───────────────────────────────────────────────


class TestSavedPalettes:
    def test_save_then_list_includes_name(self, restore_palettes):
        ct.save_palette("Cozy Reds")
        assert "Cozy Reds" in ct.list_palettes()

    def test_list_returns_sorted(self, restore_palettes):
        ct.save_palette("Bravo")
        ct.save_palette("Alpha")
        ct.save_palette("Charlie")
        assert ct.list_palettes() == ["Alpha", "Bravo", "Charlie"]

    def test_save_overwrites_existing_name(self, restore_palettes):
        ct.apply_override("ACCENT", "#111111")
        ct.save_palette("Test")
        # Change ACCENT and save under same name.
        ct.apply_override("ACCENT", "#222222")
        ct.save_palette("Test")
        # Reset and load — should restore the SECOND save.
        ct.reset_all()
        ct.load_palette("Test")
        from modules import ui_helpers

        assert ui_helpers.ACCENT == "#222222"

    def test_load_applies_saved_values(self, restore_palettes):
        from modules import ui_helpers

        ct.apply_override("WASH_HOVER", "rgba(99,88,77,0.5)")
        ct.save_palette("Custom")
        ct.reset_all()
        ct.load_palette("Custom")
        assert ui_helpers.WASH_HOVER == "rgba(99,88,77,0.5)"

    def test_load_unknown_palette_raises_key_error(self, restore_palettes):
        with pytest.raises(KeyError):
            ct.load_palette("does-not-exist")

    def test_delete_removes_from_list(self, restore_palettes):
        ct.save_palette("ToDelete")
        assert "ToDelete" in ct.list_palettes()
        ct.delete_palette("ToDelete")
        assert "ToDelete" not in ct.list_palettes()

    def test_delete_unknown_palette_is_no_op(self, restore_palettes):
        # Should not raise.
        ct.delete_palette("never-existed")
        assert ct.list_palettes() == []

    def test_save_load_round_trip_preserves_tuple_tokens(
        self, restore_palettes
    ):
        from modules import ui_helpers

        ct.apply_override("BODY_COLOR", (1, 2, 3, 250))
        ct.save_palette("Tup")
        ct.reset_all()
        ct.load_palette("Tup")
        assert ui_helpers.BODY_COLOR == (1, 2, 3, 250)


# ── HSV ↔ token conversion (settings_colors_page helpers) ──────────────


class TestColorConversion:
    """The page's parse + serialise helpers are pure functions — easy
    to test without standing up a QApplication. token_to_qcolor /
    qcolor_to_token live in settings_colors_page.py."""

    @pytest.fixture(autouse=True)
    def _qapp(self, qapp):
        # QColor doesn't need a QApplication on Qt 6 / PySide6 but
        # importing settings_colors_page pulls in QtWidgets which
        # benefits from one being present.
        pass

    def test_hex_round_trips(self):
        from modules.settings_colors_page import (
            qcolor_to_token,
            token_to_qcolor,
        )

        c = token_to_qcolor("#a8c3f0", "hex")
        assert c.red() == 0xA8
        assert c.green() == 0xC3
        assert c.blue() == 0xF0
        out = qcolor_to_token(c, "hex")
        assert out == "#a8c3f0"

    def test_rgba_with_alpha_round_trips(self):
        from modules.settings_colors_page import (
            qcolor_to_token,
            token_to_qcolor,
        )

        c = token_to_qcolor("rgba(10, 20, 30, 0.5)", "rgba")
        assert c.red() == 10
        assert c.green() == 20
        assert c.blue() == 30
        # 0.5 * 255 = 127.5 → rounds to 128.
        assert c.alpha() == 128
        out = qcolor_to_token(c, "rgba")
        # Serialised with 2-decimal alpha.
        assert "rgba(10,20,30," in out
        assert "0.50" in out

    def test_rgba_full_alpha_serialises_as_1_0(self):
        from modules.settings_colors_page import (
            qcolor_to_token,
            token_to_qcolor,
        )

        c = token_to_qcolor("rgba(50, 60, 68, 0.92)", "rgba")
        c.setAlpha(255)
        out = qcolor_to_token(c, "rgba")
        assert "1.0" in out

    def test_rgba_zero_alpha_serialises_as_0(self):
        from PySide6.QtGui import QColor

        from modules.settings_colors_page import qcolor_to_token

        c = QColor(100, 100, 100, 0)
        out = qcolor_to_token(c, "rgba")
        assert ",0)" in out

    def test_rgb_form_without_alpha_parses_as_opaque(self):
        from modules.settings_colors_page import token_to_qcolor

        c = token_to_qcolor("rgb(255, 0, 0)", "rgba")
        assert c.red() == 255
        assert c.alpha() == 255

    def test_tuple_round_trips(self):
        from modules.settings_colors_page import (
            qcolor_to_token,
            token_to_qcolor,
        )

        c = token_to_qcolor((10, 20, 30, 220), "tuple_rgba")
        assert (c.red(), c.green(), c.blue(), c.alpha()) == (10, 20, 30, 220)
        out = qcolor_to_token(c, "tuple_rgba")
        assert out == (10, 20, 30, 220)

    def test_malformed_input_falls_back_to_white(self):
        from modules.settings_colors_page import token_to_qcolor

        c = token_to_qcolor("not a real color", "rgba")
        assert c.red() == 255
        assert c.green() == 255
        assert c.blue() == 255
        assert c.alpha() == 255

    def test_hex_always_serialises_with_full_alpha(self):
        """Hex tokens have no alpha component; serialising should
        never include one."""
        from PySide6.QtGui import QColor

        from modules.settings_colors_page import qcolor_to_token

        c = QColor(255, 128, 64, 128)  # half alpha
        out = qcolor_to_token(c, "hex")
        assert out == "#ff8040"  # alpha dropped
        assert len(out) == 7
