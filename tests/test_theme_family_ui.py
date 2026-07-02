"""Settings → Display: the unified theme family / mode / frosted controls.

Builds the Display page for jellytoast + every preset family (exercising the
conditional Mode control, accent row, and base16 preview grid), then drives the
family / frosted handlers and asserts the live apply reaches the painted body —
including the preset body-tint and the Frosted↔Opaque opacity flip.
"""

from __future__ import annotations

import pytest

from jellytoast import appearance_confirm, ui_helpers
from jellytoast import color_tokens as ct
from jellytoast.settings import get_settings
from jellytoast.theme_presets import FAMILY_ORDER, family_has_both

_DISPLAY_ROW = 4  # nav index of the Display page (see test_live_font)


@pytest.fixture
def _no_revert_prompt(monkeypatch):
    """Swallow the 10s keep/revert dialog so an apply doesn't leave a live timer."""
    monkeypatch.setattr(
        appearance_confirm, "show_appearance_revert", lambda *a, **k: None
    )


@pytest.mark.parametrize("family", ["jellytoast", *FAMILY_ORDER])
def test_display_builds_for_every_family(qapp, isolated_settings, family):
    from jellytoast.settings_dialog import SettingsDialog

    s = get_settings()
    s.theme_family = "" if family == "jellytoast" else family
    s.theme_mode = "dark"
    if family != "jellytoast":
        # A preset family reads its persisted palette for the preview + tint.
        from jellytoast.theme_presets import THEME_FAMILIES, _base16_to_palette

        ct.import_palette(_base16_to_palette(THEME_FAMILIES[family].member_for("dark")))
    d = SettingsDialog()
    try:
        d.nav.setCurrentRow(_DISPLAY_ROW)  # builds _build_theme_section
        expected = "jellytoast" if family == "jellytoast" else family
        assert d._family_combo.currentData() == expected
        # Mode control presence tracks has_both.
        if family_has_both(family):
            assert d._mode_combo is not None
        else:
            assert d._mode_combo is None
        # Frosted switch is always present.
        assert d._frosted_check is not None
        # Accent row + follow-accent only for jellytoast.
        if family == "jellytoast":
            assert hasattr(d, "_follow_accent_check")
    finally:
        ct.reset_all()
        d.deleteLater()


def test_switch_family_applies_and_tints_body(
    qapp, isolated_settings, _no_revert_prompt
):
    from jellytoast.settings_dialog import SettingsDialog

    s = get_settings()
    s.theme_family = ""
    s.theme_mode = "dark"
    d = SettingsDialog()
    try:
        d.nav.setCurrentRow(_DISPLAY_ROW)
        idx = d._family_combo.findData("catppuccin")
        assert idx >= 0
        d._family_combo.setCurrentIndex(idx)  # → _on_family_changed → apply
        assert s.theme_family == "catppuccin"
        assert s.last_preset_name in ("Catppuccin Mocha", "Catppuccin Latte")
        # The painted body adopts the scheme's background (not the generic dark).
        assert ui_helpers.body_color_tuple("main")[:3] != (18, 18, 18)
    finally:
        ct.reset_all()
        d.deleteLater()


def test_frosted_toggle_flips_opacity(qapp, isolated_settings, _no_revert_prompt):
    from jellytoast.settings_dialog import SettingsDialog

    s = get_settings()
    s.theme_family = ""
    s.theme_mode = "dark"
    s.frosted = True
    d = SettingsDialog()
    try:
        d.nav.setCurrentRow(_DISPLAY_ROW)
        d._frosted_check.setChecked(False)  # → _on_frosted_toggled
        assert s.frosted is False
        # Opaque → the solid "dark" body, fully opaque.
        assert ui_helpers.body_color_tuple("main")[3] == 255
        d._frosted_check.setChecked(True)
        assert s.frosted is True
    finally:
        ct.reset_all()
        d.deleteLater()


def test_import_scheme_applies_and_previews(
    qapp, isolated_settings, _no_revert_prompt
):
    from jellytoast.external_theme import parse_base16_yaml
    from jellytoast.settings_dialog import SettingsDialog
    from jellytoast.theme_presets import apply_imported_preset

    # A minimal valid base16 scheme (all 16 slots) — the grey ramp is steep
    # enough that base05-on-base00 clears the import contrast guard.
    yaml = "\n".join(
        [
            f'base0{c}: "{min(i * 34, 255):02x}{min(i * 34, 255):02x}{min(i * 34, 255):02x}"'
            for i, c in enumerate("0123456789ABCDEF")
        ]
    )
    preset = parse_base16_yaml(yaml)
    apply_imported_preset(preset, get_settings().frosted)
    s = get_settings()
    assert s.theme_family == "imported"
    assert s.imported_scheme_json  # persisted for restart survival

    d = SettingsDialog()
    try:
        d.nav.setCurrentRow(_DISPLAY_ROW)  # builds the imported preview grid
        assert d._family_combo.currentData() == "imported"
        assert d._mode_combo is None  # single-variant import: no follow-system
    finally:
        ct.reset_all()
        s.imported_scheme_json = ""
        d.deleteLater()


def test_glass_opacity_slider_drives_preset_alpha(
    qapp, isolated_settings, _no_revert_prompt
):
    from jellytoast.settings_dialog import SettingsDialog
    from jellytoast.theme import get_active_theme
    from jellytoast.theme_presets import THEME_FAMILIES, _base16_to_palette

    s = get_settings()
    s.theme_family = "catppuccin"
    s.theme_mode = "dark"
    s.frosted = True
    s.preset_glass_alpha = 0
    ct.import_palette(_base16_to_palette(THEME_FAMILIES["catppuccin"].member_for("dark")))
    d = SettingsDialog()
    try:
        d.nav.setCurrentRow(_DISPLAY_ROW)  # frosted preset → slider present
        assert d._glass_slider is not None
        d._glass_slider.setValue(230)  # → _on_glass_alpha_changed
        assert s.preset_glass_alpha == 230
        # get_active_theme composes the frosted body at the slider's alpha.
        assert get_active_theme().body_color[3] == 230
    finally:
        ct.reset_all()
        s.preset_glass_alpha = 0
        d.deleteLater()


def test_glass_slider_shows_for_frosted_hides_for_opaque(qapp, isolated_settings):
    from jellytoast.settings_dialog import SettingsDialog

    s = get_settings()
    # jellytoast FROSTED now gets the slider too (its glass is user-tunable).
    s.theme_family = ""
    s.theme_mode = "dark"
    s.frosted = True
    d = SettingsDialog()
    try:
        d.nav.setCurrentRow(_DISPLAY_ROW)
        assert getattr(d, "_glass_slider", None) is not None
    finally:
        d.deleteLater()

    # Opaque (any family) → no slider; a solid body has no opacity to tune.
    s.theme_family = "catppuccin"
    s.frosted = False
    d2 = SettingsDialog()
    try:
        d2.nav.setCurrentRow(_DISPLAY_ROW)
        assert not hasattr(d2, "_glass_slider")
    finally:
        ct.reset_all()
        d2.deleteLater()


def test_eyedropper_reflects_live_system_accent(qapp, isolated_settings):
    from jellytoast.settings_dialog import SettingsDialog
    from jellytoast.system_accent import apply_accent_now

    s = get_settings()
    s.theme_family = ""
    s.theme_mode = "dark"
    s.frosted = True
    d = SettingsDialog()
    try:
        d.nav.setCurrentRow(_DISPLAY_ROW)  # builds the accent row + eyedropper
        # A custom (non-preset) accent applied from OUTSIDE the dialog (the
        # Follow-system-accent path) should land in the eyedropper swatch as the
        # live indicator of the current colour.
        custom = "#3daee9"  # KDE Breeze blue — not a jellytoast preset
        apply_accent_now(custom)  # sets accent_color + emits theme_changed
        assert d._accent_dropper._fill is not None
        assert d._accent_dropper._fill.name().lower() == custom
        assert d._accent_dropper.isChecked()
        # A subsequent PRESET-matching accent clears the dropper (the preset ring
        # indicates it instead) — no stale custom fill left selected.
        from jellytoast.theme import ACCENT_PRESETS

        preset_hex = ACCENT_PRESETS[0][1]  # Purple #967de1
        apply_accent_now(preset_hex)
        assert not d._accent_dropper.isChecked()
    finally:
        ct.reset_all()
        d.deleteLater()


def test_jellytoast_glass_slider_and_default_button(
    qapp, isolated_settings, _no_revert_prompt
):
    from jellytoast.settings_dialog import SettingsDialog
    from jellytoast.theme import get_active_theme

    s = get_settings()
    s.theme_family = ""
    s.theme_mode = "dark"
    s.frosted = True
    s.jellytoast_glass_alpha = 0
    d = SettingsDialog()
    try:
        d.nav.setCurrentRow(_DISPLAY_ROW)
        # Drag → writes the jellytoast key (not the preset one) + retints alpha.
        d._glass_slider.setValue(160)
        assert s.jellytoast_glass_alpha == 160
        assert s.preset_glass_alpha == 0
        assert get_active_theme().body_color[3] == 160
        # Default button → back to jellytoast's own airier default (172).
        d._reset_glass_alpha()
        assert s.jellytoast_glass_alpha == 0
        assert get_active_theme().body_color[3] == 172
    finally:
        s.jellytoast_glass_alpha = 0
        d.deleteLater()


def test_themes_folder_rows_in_family_dropdown(
    qapp, isolated_settings, _no_revert_prompt, tmp_path, monkeypatch
):
    """A scheme dropped in the watched themes folder shows as a family row
    (key file:<path>), and the active folder scheme's row is the selection —
    not a duplicate 'imported' row."""
    from jellytoast.settings_dialog import SettingsDialog

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    folder = tmp_path / "jellytoast" / "themes"
    folder.mkdir(parents=True)
    ramp = ["#101418", "#181c22", "#20262e", "#3a4250", "#8a93a6", "#d8dee9",
            "#e5e9f0", "#eceff4", "#bf616a", "#d08770", "#ebcb8b", "#a3be8c",
            "#88c0d0", "#81a1c1", "#b48ead", "#5e81ac"]
    (folder / "drop.yaml").write_text(
        'scheme: "Dropped"\n'
        + "\n".join(f'base0{c}: "{v}"' for c, v in zip("0123456789ABCDEF", ramp, strict=True)),
        encoding="utf-8",
    )
    path = str(folder / "drop.yaml")

    s = get_settings()
    s.theme_family = "imported"
    s.imported_scheme_path = path
    s.imported_scheme_json = ""  # folder row supplies the palette, not the blob
    d = SettingsDialog()
    try:
        d.nav.setCurrentRow(_DISPLAY_ROW)
        keys = [d._family_combo.itemData(i) for i in range(d._family_combo.count())]
        assert f"file:{path}" in keys
        assert "imported" not in keys  # no duplicate row for a folder scheme
        assert d._family_combo.currentData() == f"file:{path}"
    finally:
        s.imported_scheme_path = ""
        ct.reset_all()
        d.deleteLater()


def test_glass_settle_flushes_on_dialog_close(qapp, isolated_settings, monkeypatch):
    """A glass drag that settles within 120ms of the dialog closing must still
    run the full re-stamp — the settle timer dies with the dialog, so `finished`
    flushes it. Regression: cached-body surfaces stayed stale until the next
    theme event."""
    from jellytoast.settings_dialog import SettingsDialog

    s = get_settings()
    s.theme_family = ""
    s.theme_mode = "dark"
    s.frosted = True
    d = SettingsDialog()
    d.nav.setCurrentRow(_DISPLAY_ROW)
    d._glass_slider.setValue(150)  # starts the 120ms settle timer
    assert d._glass_settle.isActive()

    committed = {"n": 0}
    monkeypatch.setattr(
        d, "_commit_glass_alpha", lambda: committed.__setitem__("n", committed["n"] + 1)
    )
    d._flush_pending_glass_settle()  # what `finished` calls before deleteLater
    assert committed["n"] == 1
    assert not d._glass_settle.isActive()
    d.deleteLater()
