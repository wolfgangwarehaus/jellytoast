"""Live font-family swap + the keep/auto-revert safety prompt.

Font family now applies LIVE (no restart): persist → app.setFont → emit
theme_changed (delegates already rebuild their fonts on it). A change shows an
AppearanceRevertDialog that auto-reverts in N seconds unless kept — so a
broken/illegible pick undoes itself.
"""

from __future__ import annotations

from jellytoast import appearance_confirm, ui_helpers
from jellytoast.appearance_confirm import AppearanceRevertDialog


def test_apply_font_settings_live_sets_app_font(qapp):
    ui_helpers.set_boot_default_font(qapp.font())
    from jellytoast.settings import get_settings

    s = get_settings()
    orig = s.font_family
    try:
        s.font_family = "DejaVu Serif"
        ui_helpers.apply_font_settings_live()
        assert qapp.font().family() == "DejaVu Serif"
        # Empty → restores the captured boot default, not QFont("").
        s.font_family = ""
        ui_helpers.apply_font_settings_live()
        assert qapp.font().family() == ui_helpers._BOOT_DEFAULT_FONT.family()
    finally:
        s.font_family = orig
        ui_helpers.apply_font_settings_live()


def _revert_dlg(qapp, seconds=5):
    reverts = {"n": 0}
    d = AppearanceRevertDialog(
        None, on_revert=lambda: reverts.__setitem__("n", reverts["n"] + 1), seconds=seconds
    )
    # SHOW it: QDialog.closeEvent only calls reject() when the dialog is visible,
    # which is exactly the path that used to recurse (close↔closeEvent↔reject)
    # and freeze the prompt. An unshown dialog never hit it, so these tests must
    # show the dialog to guard the real bug.
    d.show()
    qapp.processEvents()
    return d, reverts


def test_revert_dialog_keep_does_not_revert(qapp):
    # _finish() hides synchronously, so check before processing events — the
    # deferred deleteLater would otherwise delete the wrapper we assert on.
    d, reverts = _revert_dlg(qapp)
    d._do_keep()  # explicit Keep
    assert reverts["n"] == 0
    assert not d.isVisible()  # Keep must actually dismiss the prompt (regression)


def test_revert_dialog_revert_button_reverts_and_closes(qapp):
    d, reverts = _revert_dlg(qapp)
    d.reject()  # Revert now / ✕ / Esc all route here
    assert reverts["n"] == 1
    assert not d.isVisible()


def test_revert_dialog_close_reverts(qapp):
    d, reverts = _revert_dlg(qapp)
    d.close()  # a stray WM / programmatic close is still a (non-Keep) dismissal
    assert reverts["n"] == 1
    assert not d.isVisible()


def test_revert_dialog_timeout_reverts(qapp):
    d, reverts = _revert_dlg(qapp, seconds=1)
    d._tick()  # 1 → 0 → finish → revert
    assert reverts["n"] == 1
    assert not d.isVisible()


def test_revert_dialog_keep_is_idempotent(qapp):
    # After Keep, a further dismissal (the timer tick, a stray close) must not
    # re-fire — the old freeze left every control re-entrant.
    d, reverts = _revert_dlg(qapp)
    d._do_keep()
    d.close()
    d._tick()
    assert reverts["n"] == 0  # still kept, never reverted


def test_font_scale_change_applies_live_and_reverts(qapp, monkeypatch):
    import jellytoast.design_tokens as dt
    from jellytoast.settings import get_settings
    from jellytoast.settings_dialog import SettingsDialog

    s = get_settings()
    orig = s.font_scale
    s.font_scale = "default"
    dt.refresh_fonts()
    try:
        d = SettingsDialog()
        d.nav.setCurrentRow(4)  # Display
        assert dt.TYPE_BODY.size_px == 13  # baseline

        captured = {}
        monkeypatch.setattr(
            appearance_confirm,
            "show_appearance_revert",
            lambda revert, **k: captured.update(revert=revert),
        )

        idx = d._font_size_combo.findData("largest")
        d._font_size_combo.blockSignals(True)
        d._font_size_combo.setCurrentIndex(idx)
        d._font_size_combo.blockSignals(False)
        d._on_font_scale_changed()

        # Tokens recomputed LIVE (13 → round(13*1.25) = 16), setting persisted.
        assert dt.TYPE_BODY.size_px == 16
        assert s.font_scale == "largest"
        assert d._active_font_scale == "largest"
        assert "revert" in captured

        captured["revert"]()  # auto-revert
        assert s.font_scale == "default"
        assert dt.TYPE_BODY.size_px == 13
        assert d._active_font_scale == "default"
        d.deleteLater()
    finally:
        s.font_scale = orig
        dt.refresh_fonts()


def test_font_family_change_applies_live_and_reverts(qapp, monkeypatch):
    ui_helpers.set_boot_default_font(qapp.font())
    from jellytoast.settings_dialog import SettingsDialog

    d = SettingsDialog()
    d.nav.setCurrentRow(4)  # navigate to Display → builds + retains the font combo
    initial = d._active_font_family

    captured = {}
    monkeypatch.setattr(
        appearance_confirm, "show_appearance_revert", lambda revert, **k: captured.update(revert=revert)
    )

    fam = d._font_family_combo.itemData(1)  # first installed family after "System default"
    d._font_family_combo.blockSignals(True)
    d._font_family_combo.setCurrentIndex(1)
    d._font_family_combo.blockSignals(False)
    d._on_font_family_changed()

    assert d.s.font_family == fam
    assert d._active_font_family == fam
    assert "revert" in captured  # prompt was shown

    captured["revert"]()  # simulate auto-revert
    assert d.s.font_family == initial
    assert d._active_font_family == initial
    d.deleteLater()
