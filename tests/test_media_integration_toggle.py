"""OS media-integration enable/disable toggle (Settings → Playback).

Covers the setting (default on, round-trips) and the Playback-page
toggle wiring. The actual MPRIS/SMTC service start is gated on this
setting at launch (jellytoast/app.py) — that one-liner gate isn't
booted here; the default-on guarantee (current behaviour preserved)
is pinned by the setting test.
"""

from __future__ import annotations


def test_media_integration_setting_defaults_on_and_round_trips(isolated_settings):
    # Default must be ON so existing installs keep OS media controls.
    assert isolated_settings.media_integration_enabled is True
    isolated_settings.media_integration_enabled = False
    assert isolated_settings.media_integration_enabled is False
    isolated_settings.media_integration_enabled = True
    assert isolated_settings.media_integration_enabled is True


def test_playback_page_media_integration_toggle(qapp, isolated_settings):
    from jellytoast.settings_dialog import SettingsDialog

    dlg = SettingsDialog()
    try:
        # Pages build lazily on first navigation — materialise Playback.
        # Keep the page referenced: it owns the checkbox's C++ object, so
        # dropping it would delete the widget out from under us.
        page = dlg._build_playback()
        assert page is not None
        chk = dlg._media_integration_check
        assert chk.isChecked() is True  # reflects the default-on setting
        chk.setChecked(False)  # user opts out
        assert isolated_settings.media_integration_enabled is False
        chk.setChecked(True)  # and back on
        assert isolated_settings.media_integration_enabled is True
    finally:
        dlg.deleteLater()
