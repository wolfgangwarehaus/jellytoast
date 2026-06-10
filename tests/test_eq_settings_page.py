"""Widget-layer guard tests for ``jellytoast.settings_eq_page.EqSettingsPage``.

This is the coverage the EQ section never had: ``test_eq_settings.py``
exercises the Settings *properties* + the ``apply_eq`` backend chain, but
nothing built the EQ *UI* (combo population, save/delete, cast-greying,
the slider double-click eventFilter, or the bit-perfect cross-section
reach). When the EQ cluster was extracted out of ``settings_dialog.py``
into its own widget (2026-06-02), these tests pin the behaviour so the
move is *proven* preserved, not just asserted.

Qt harness follows ``test_hotkeys.py``: a process-wide ``qapp`` fixture
(conftest), widgets are never ``show()``n so ``isHidden()`` is used over
``isVisible()``, and every built widget is ``deleteLater``'d.
"""

from __future__ import annotations

import pytest
from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QMouseEvent

from jellytoast.eq_presets import BAND_COUNT


@pytest.fixture
def settings(isolated_settings):
    """Isolated Settings with the EQ keys cleared before/after."""
    keys = [
        "playback/eq_enabled",
        "playback/eq_preamp",
        "playback/eq_bands",
        "playback/eq_preset",
        "playback/eq_user_presets",
        "playback/eq_linear_phase",
        "playback/eq_view_advanced",
        "playback/eq_autoeq_profile_json",
    ]
    for k in keys:
        isolated_settings._s.remove(k)
    yield isolated_settings
    for k in keys:
        isolated_settings._s.remove(k)


def _page(settings):
    from jellytoast.settings_eq_page import EqSettingsPage

    return EqSettingsPage(settings)


# ── Construction ───────────────────────────────────────────────────────


class TestBuild:
    def test_builds_with_all_core_widgets(self, qapp, settings):
        page = _page(settings)
        try:
            # 1 pre-amp + 10 bands = 11 sliders / readouts / band labels.
            assert len(page._eq_sliders) == BAND_COUNT + 1
            assert len(page._eq_readouts) == BAND_COUNT + 1
            assert len(page._eq_band_labels) == BAND_COUNT + 1
            assert page._eq_enabled_check is not None
            assert page._eq_linear_phase_check is not None
            assert page._eq_preset_combo is not None
            # Flags initialised in __init__ (the in-dialog version relied
            # on getattr defaults).
            assert page._eq_cast_blocking is False
            assert page._eq_dragging is False
        finally:
            page.deleteLater()

    def test_preset_combo_has_builtins_plus_custom(self, qapp, settings):
        page = _page(settings)
        try:
            datas = [
                page._eq_preset_combo.itemData(i)
                for i in range(page._eq_preset_combo.count())
            ]
            assert "Flat" in datas
            assert "Custom" in datas  # the slider-state sentinel, always last
            assert datas[-1] == "Custom"
        finally:
            page.deleteLater()

    def test_sliders_seed_from_settings(self, qapp, settings):
        settings.eq_preamp = 4.0
        settings.eq_bands = [0.0] * BAND_COUNT
        page = _page(settings)
        try:
            # Column 0 is the pre-amp slider.
            assert page._eq_sliders[0].value() == 4
        finally:
            page.deleteLater()


# ── Enable toggle → settings + signal ──────────────────────────────────


class TestEnableToggle:
    def test_toggle_persists_and_emits(self, qapp, settings):
        from jellytoast.player_state import PlayerBus

        settings.eq_enabled = False
        page = _page(settings)
        try:
            fired: list = []
            PlayerBus.get().eq_changed.connect(lambda en, bands: fired.append(en))
            page._eq_enabled_check.setChecked(True)  # drives _on_eq_enabled_toggled
            assert settings.eq_enabled is True
            assert fired and fired[-1] is True
        finally:
            page.deleteLater()


# ── Preset save / populate ─────────────────────────────────────────────


class TestPresets:
    def test_user_preset_appears_in_combo_after_repopulate(self, qapp, settings):
        settings.eq_user_presets = {
            "MyCurve": {"preamp": 0.0, "bands": [1.0] * BAND_COUNT}
        }
        page = _page(settings)
        try:
            page._populate_eq_preset_combo()
            datas = [
                page._eq_preset_combo.itemData(i)
                for i in range(page._eq_preset_combo.count())
            ]
            assert "MyCurve" in datas
        finally:
            page.deleteLater()


# ── Cast-greying (the self-contained cascade) ──────────────────────────


class TestCastGreying:
    def test_cast_active_disables_then_cleared_reenables(self, qapp, settings):
        settings.eq_enabled = True
        page = _page(settings)
        try:
            # Enabled + not casting → controls live.
            assert page._eq_preset_combo.isEnabled()
            page._on_eq_cast_active()
            assert page._eq_cast_blocking is True
            assert not page._eq_preset_combo.isEnabled()
            assert not page._eq_sliders[0].isEnabled()
            assert not page._eq_caption.isHidden()  # "Casting — EQ inactive" shown
            page._on_eq_cast_cleared()
            assert page._eq_cast_blocking is False
            assert page._eq_preset_combo.isEnabled()
        finally:
            page.deleteLater()


# ── Slider double-click eventFilter (snap-to-0) ────────────────────────


class TestDoubleClickReset:
    def test_double_click_snaps_slider_to_zero(self, qapp, settings):
        page = _page(settings)
        try:
            slider = page._eq_sliders[1]  # a band slider
            slider.setValue(7)
            ev = QMouseEvent(
                QEvent.Type.MouseButtonDblClick,
                QPointF(0, 0),
                Qt.MouseButton.LeftButton,
                Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.NoModifier,
            )
            handled = page.eventFilter(slider, ev)
            assert handled is True
            assert slider.value() == 0
        finally:
            page.deleteLater()

    def test_double_click_on_non_slider_is_ignored(self, qapp, settings):
        page = _page(settings)
        try:
            ev = QMouseEvent(
                QEvent.Type.MouseButtonDblClick,
                QPointF(0, 0),
                Qt.MouseButton.LeftButton,
                Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.NoModifier,
            )
            # The enable checkbox is not in _eq_sliders → falls through.
            assert page.eventFilter(page._eq_enabled_check, ev) is False
        finally:
            page.deleteLater()


# ── Accent re-stamp hook (no crash; the dialog calls this) ─────────────


class TestReapplyAccent:
    def test_reapply_accent_runs(self, qapp, settings):
        page = _page(settings)
        try:
            page.reapply_accent()  # must not raise
        finally:
            page.deleteLater()


# ── Dialog integration: the bit-perfect cross-section reach ────────────


class TestDialogIntegration:
    def _dialog_on_playback(self, qapp):
        from jellytoast.settings_dialog import SettingsDialog

        dlg = SettingsDialog()
        for i in range(dlg.nav.count()):
            if dlg.nav.item(i).text() == "Playback":
                dlg.nav.setCurrentRow(i)
                break
        return dlg

    def test_dialog_exposes_page_and_aliases(self, qapp):
        dlg = self._dialog_on_playback(qapp)
        try:
            assert hasattr(dlg, "_eq_page")
            # The three re-exposed names point at the page's members so
            # the bit-perfect gating (string-name getattr) keeps working.
            assert dlg._eq_enabled_check is dlg._eq_page._eq_enabled_check
            assert dlg._eq_linear_phase_check is dlg._eq_page._eq_linear_phase_check
            assert callable(dlg._refresh_eq_enabled_state)
        finally:
            dlg.deleteLater()

    def test_bit_perfect_on_unticks_eq_through_alias(self, qapp):
        dlg = self._dialog_on_playback(qapp)
        try:
            # Turn EQ on via the page, then flip bit-perfect on — the
            # dialog's handler reaches the page's enable check by the
            # aliased name and unticks it.
            dlg._eq_page._eq_enabled_check.setChecked(True)
            assert dlg.s.eq_enabled is True
            dlg._on_bit_perfect_toggled(True)
            assert dlg.s.eq_enabled is False
            assert dlg._eq_page._eq_enabled_check.isChecked() is False
            # And the gating greys the page's enable check.
            assert not dlg._eq_page._eq_enabled_check.isEnabled()
        finally:
            dlg.deleteLater()
