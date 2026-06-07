"""Regression: surfaces that bake theme tokens into per-widget QSS must
re-stamp on ``PlayerBus.theme_changed`` (the per-surface re-stamp contract,
architecture_live_accent.md).

Each of these widgets previously froze its colours at construction, so a
dark<->light swap while the widget was visible/cached left text in the old
palette — worst case white-on-light = invisible. These tests flip the theme
live and assert the baked QSS actually changes.
"""

from __future__ import annotations

import pytest

from modules import icons
from modules import ui_helpers as u
from modules.player_state import PlayerBus
from modules.settings import get_settings


def _flip(mode: str) -> None:
    """Replicate the live theme-swap fan-out (settings_dialog path):
    set the mode, refresh the token + icon caches, broadcast."""
    get_settings().theme_mode = mode
    u.refresh_theme()
    icons.refresh_theme()
    PlayerBus.get().theme_changed.emit()


@pytest.fixture
def themed(qapp, isolated_settings):
    """Start from frosted_dark; restore auto after."""
    _flip("frosted_dark")
    yield
    _flip("auto")


def _headline_color(es) -> str:
    return es._headline_label.styleSheet()


class TestEmptyStateRestamp:
    def test_headline_and_action_restamp_on_theme_change(self, themed):
        es = u.EmptyState(headline="No results", sub="x", action_label="Retry")
        dark_head = es._headline_label.styleSheet()
        dark_btn = es._action_btn.styleSheet()
        assert "#ffffff" in dark_head  # white ink in the dark family

        _flip("frosted_light")

        light_head = es._headline_label.styleSheet()
        assert light_head != dark_head
        assert "#000000" in light_head  # near-black ink in the light family
        assert es._action_btn.styleSheet() != dark_btn

    def test_restamp_survives_set_state(self, themed):
        es = u.EmptyState(headline="A")
        es.set_state(headline="B", sub="s")
        _flip("frosted_light")
        assert "#000000" in es._headline_label.styleSheet()


class TestSmartPlaylistRowRestamp:
    def test_row_name_restamps(self, themed):
        from modules.smart_playlists_view import _SmartPlaylistRow

        row = _SmartPlaylistRow({"name": "Test", "rules": {"match": "all", "rules": []}})
        before = row._name_label.styleSheet()
        assert "#ffffff" in before
        _flip("frosted_light")
        row._apply_styling()  # the view's _reapply_accent calls this per row
        after = row._name_label.styleSheet()
        assert after != before
        assert "#000000" in after


class TestDownloadRowRestamp:
    def test_row_name_restamps(self, themed):
        from modules.downloads_view import _DownloadRow

        row = _DownloadRow(
            {"item_id": "x", "kind": "album", "name": "Album X", "state": "pending"}
        )
        before = row._name.styleSheet()
        _flip("frosted_light")
        row._reapply_accent()  # the view's _reapply_accent calls this per row
        assert row._name.styleSheet() != before
        assert "#000000" in row._name.styleSheet()


class TestTagEditorDialogRestamp:
    def test_dialog_qss_restamps_on_theme_change(self, themed):
        from modules.tag_editor import TagEditorDialog

        d = TagEditorDialog(
            {"Id": "x", "Name": "t", "Album": "A", "AlbumId": "", "Artists": ["Z"]}
        )
        try:
            before = d.styleSheet()
            assert "#101010" in before  # dark dialog background
            _flip("frosted_light")
            after = d.styleSheet()
            assert after != before
            assert "#f4f4f6" in after  # light dialog background
        finally:
            d.deleteLater()


class TestPairingDialogRestamp:
    def test_header_restamps_on_theme_change(self, themed, monkeypatch):
        from modules import airplay_pairing as ap
        from modules.airplay2 import AirPlay2Device

        # _start_begin kicks off a real pairing round-trip; stub it out.
        monkeypatch.setattr(ap.PairingDialog, "_start_begin", lambda self: None)
        dev = AirPlay2Device.__new__(AirPlay2Device)
        dev.name = "Probe"
        dev.identifier = "probe-id"
        d = ap.PairingDialog(dev)
        try:
            before = d._title.styleSheet()
            assert "#ffffff" in before
            _flip("frosted_light")
            after = d._title.styleSheet()
            assert after != before
            assert "#000000" in after
        finally:
            d.deleteLater()
