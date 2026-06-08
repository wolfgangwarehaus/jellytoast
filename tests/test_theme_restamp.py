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


class TestAlphabetRailRestamp:
    """The A-Z rail bakes ink_alpha(0.30) into every inactive letter — all
    26 went stale (wrong-contrast) on a dark↔light switch until clicked."""

    def test_letters_restamp(self, themed):
        from modules.library_grid import _AlphabetIndex

        idx = _AlphabetIndex()
        idx.set_current_letter("A")
        before_inactive = idx._buttons["B"].styleSheet()
        before_active = idx._buttons["A"].styleSheet()
        assert "255,255,255" in before_inactive  # white ink wash, dark family
        assert "#ffffff" in before_active

        _flip("frosted_light")
        idx._reapply_theme()  # LibraryGrid fans theme_changed into this

        assert idx._buttons["B"].styleSheet() != before_inactive
        assert "0,0,0" in idx._buttons["B"].styleSheet()
        assert "#000000" in idx._buttons["A"].styleSheet()


class TestHorizontalRailHeaderRestamp:
    """HorizontalRail (suggestions + search rails) bakes TEXT_FAINT into its
    section header; the delegate repaint doesn't reach the QLabel."""

    def test_header_restamps(self, themed):
        from modules.horizontal_rail import HorizontalRail

        rail = HorizontalRail(label="Latest", kind="album")
        before = rail._header.styleSheet()
        assert "rgba(255,255,255" in before
        _flip("frosted_light")  # rail self-subscribes to theme_changed
        after = rail._header.styleSheet()
        assert after != before
        assert "#000000" in after


class TestSearchViewChromeRestamp:
    """The search ✕, empty-state status, and Songs section header all baked
    theme ink that SearchView._reapply_accent never re-stamped."""

    def test_close_status_and_songs_header_restamp(self, themed):
        from modules.search_view import SearchView

        view = SearchView()
        before_close = view._close_btn.styleSheet()
        before_status = view._status.styleSheet()
        # The embedded _SongsSection self-subscribes its own header re-stamp.
        before_head = view._songs_section._header.styleSheet()
        assert "255" in before_close
        assert "rgba(255,255,255" in before_head

        # SearchView self-subscribes _reapply_accent to theme_changed; the
        # _SongsSection self-subscribes _reapply_theme — one flip drives both.
        _flip("frosted_light")

        assert view._close_btn.styleSheet() != before_close
        assert "0,0,0" in view._close_btn.styleSheet()
        assert view._status.styleSheet() != before_status
        assert "#000000" in view._status.styleSheet()
        assert view._songs_section._header.styleSheet() != before_head
        assert "#000000" in view._songs_section._header.styleSheet()


class TestCastSectionRestamp:
    """CastDialog section headers + list rows bake theme ink once; the
    dialog's _reapply_accent now fans a re-stamp into each section."""

    def test_section_header_and_list_restamp(self, themed):
        from modules.cast_dialog import _CastSection

        section = _CastSection("chromecast", "Chromecast")
        before_name = section._name_label.styleSheet()
        before_list = section._list.styleSheet()
        assert "#ffffff" in before_name
        assert "255,255,255" in before_list

        _flip("frosted_light")
        section.reapply_theme()  # CastDialog._reapply_accent fans this out

        assert section._name_label.styleSheet() != before_name
        assert "#000000" in section._name_label.styleSheet()
        assert section._list.styleSheet() != before_list
        assert "0,0,0" in section._list.styleSheet()


class TestNowPlayingLyricsCaptionRestamp:
    """The now-playing "Hide lyrics" toggle + "● Live" re-snap buttons baked
    TEXT_FAINT/TEXT into their QSS; _reapply_theme now re-issues the shared
    caption-button QSS so they follow a dark↔light flip."""

    def test_caption_btn_qss_flips(self, themed):
        from modules.now_playing_page import _lyrics_caption_btn_qss

        before = _lyrics_caption_btn_qss()
        assert "rgba(255,255,255,0.4)" in before  # TEXT_FAINT, dark family
        assert "#ffffff" in before  # :hover TEXT
        _flip("frosted_light")
        after = _lyrics_caption_btn_qss()
        assert after != before
        assert "#000000" in after  # near-black ink, light family


class TestNowPlayingLyricsBodyRestamp:
    """Unsynced lyric lines + the status fallback label ("No lyrics…") bake
    ink at build time; _restamp_lyrics_theme re-colors them on a flip."""

    def _host(self):
        from PySide6.QtWidgets import QScrollArea, QVBoxLayout, QWidget

        from modules.np_lyrics import _LyricsMixin

        class _LyricsHost(_LyricsMixin, QWidget):
            def __init__(self):
                super().__init__()
                self._lyrics_scroll = QScrollArea(self)
                container = QWidget()
                self._lyrics_layout = QVBoxLayout(container)
                self._lyrics_layout.addStretch(1)
                self._lyrics_scroll.setWidget(container)
                self._lyrics_widgets = []
                self._lyrics_starts_ms = []
                self._lyrics_synced = False
                self._active_line_idx = -1
                self._user_off_live = False
                self._status_label = None

            def _update_lyrics_visibility(self):
                pass

            def _update_live_btn_visibility(self):
                pass

        return _LyricsHost()

    def test_status_label_recolors(self, themed):
        host = self._host()
        host._set_lyrics_text("No lyrics available", muted=False)
        before = host._status_label.styleSheet()
        assert "rgba(255,255,255,0.7)" in before  # TEXT_DIM, dark family
        _flip("frosted_light")
        host._restamp_lyrics_theme()
        after = host._status_label.styleSheet()
        assert after != before
        assert "#000000" in after

    def test_unsynced_lines_recolor(self, themed):
        host = self._host()
        # Unsynced payload → lines built at a uniform faint falloff.
        host._render_lyrics_payload({"Lyrics": [{"Text": "verse one"}, {"Text": "verse two"}]})
        assert host._lyrics_synced is False
        before = host._lyrics_widgets[0].styleSheet()
        assert "rgba(255,255,255" in before  # ink_alpha falloff, dark
        _flip("frosted_light")
        host._restamp_lyrics_theme()
        after = host._lyrics_widgets[0].styleSheet()
        assert after != before
        assert "rgba(0,0,0" in after


class TestMiniPlayerRadioBadgeRestamp:
    """A live-radio theme flip ran _apply_panel_theme (forcing the subtitle to
    plain TEXT_DIM) without re-stamping the accent "● LIVE" badge — so the
    badge silently lost its accent. _reapply_theme now re-stamps it."""

    def test_live_badge_keeps_accent_after_flip(self, themed, monkeypatch):
        from modules import mini_player as _mp
        from modules.radio_state import RadioState

        mp = _mp.FloatingMiniPlayer()
        try:
            state = RadioState(station_name="Jazz FM", playback_state="playing")
            assert state.is_live
            monkeypatch.setattr("modules.radio_state.current", lambda: state)
            mp._is_radio = True

            _flip("frosted_light")
            mp._reapply_theme()

            for panel in (mp.compact, mp.expanded):
                # Accent badge preserved, not clobbered to plain TEXT_DIM.
                assert u.ACCENT in panel.subtitle.styleSheet()
        finally:
            mp.deleteLater()

    def test_non_radio_subtitle_recolors_after_flip(self, themed):
        from modules import mini_player as _mp

        mp = _mp.FloatingMiniPlayer()
        try:
            mp._is_radio = False
            before = mp.compact.subtitle.styleSheet()
            assert "rgba(255,255,255,0.7)" in before  # TEXT_DIM, dark
            _flip("frosted_light")
            mp._reapply_theme()
            after = mp.compact.subtitle.styleSheet()
            assert after != before
            assert "#000000" in after
        finally:
            mp.deleteLater()


class TestGroupVolumePopupChromeRestamp:
    """The group-volume popup's _reapply_accent re-stamped the body fill +
    sliders but not its text chrome (chevron toggle, hover-name, group label,
    "No speakers found") — those stayed wrong-contrast on a flip."""

    def test_text_chrome_restamps(self, themed):
        from PySide6.QtWidgets import QWidget

        from modules.volume_button import _GroupVolumePopup

        host = QWidget()  # keep a ref — a temporary parent GCs the popup
        popup = _GroupVolumePopup(host)
        popup.set_members([])  # → the "No speakers found" placeholder
        before_toggle = popup._toggle_btn.styleSheet()
        before_group = popup._group_label.styleSheet()
        before_empty = popup._empty_label.styleSheet()
        assert "rgba(255,255,255,0.4)" in before_toggle  # TEXT_FAINT, dark
        assert "rgba(255,255,255,0.4)" in before_empty

        _flip("frosted_light")
        popup._reapply_accent()

        assert popup._toggle_btn.styleSheet() != before_toggle
        assert "#000000" in popup._toggle_btn.styleSheet()
        assert popup._group_label.styleSheet() != before_group
        assert "#000000" in popup._group_label.styleSheet()
        assert "#000000" in popup._empty_label.styleSheet()


class TestAboutDialogRestamp:
    """_AboutDialog baked TEXT/TEXT_DIM into its labels with no theme_changed
    hook — wrong-contrast on an OS-auto flip while open."""

    def test_labels_restamp(self, themed):
        from modules.settings_dialog import _AboutDialog

        d = _AboutDialog()
        try:
            before = d._lbl_title.styleSheet()
            before_desc = d._lbl_desc.styleSheet()
            assert "#ffffff" in before
            assert "rgba(255,255,255,0.7)" in before_desc  # TEXT_DIM, dark
            _flip("frosted_light")  # dialog self-subscribes theme_changed
            assert d._lbl_title.styleSheet() != before
            assert "#000000" in d._lbl_title.styleSheet()
            assert "#000000" in d._lbl_desc.styleSheet()
        finally:
            d.deleteLater()


class TestAlternateUrlsDialogRestamp:
    """_AlternateUrlsDialog (login view) baked BG/TEXT_DIM/ACCENT into its
    chrome + rows with no theme_changed hook."""

    def test_dialog_and_rows_restamp(self, themed):
        import types

        from modules.login_view import _AlternateUrlsDialog

        settings = types.SimpleNamespace(
            server_hostnames=[{"url": "http://alt:8096", "label": "LAN"}]
        )
        d = _AlternateUrlsDialog(settings)
        try:
            before_intro = d._intro.styleSheet()
            before_row = d._rows[0].url_edit.styleSheet()
            assert "rgba(255,255,255,0.7)" in before_intro  # TEXT_DIM, dark
            _flip("frosted_light")  # dialog self-subscribes theme_changed
            assert d._intro.styleSheet() != before_intro
            assert "#000000" in d._intro.styleSheet()
            assert d._rows[0].url_edit.styleSheet() != before_row
            assert "#000000" in d._rows[0].url_edit.styleSheet()
        finally:
            d.deleteLater()


class TestDownloadsPausedCountsRestamp:
    """_QueueAggregateBlock._reapply_accent re-stamped the queue counts to
    bright TEXT unconditionally, ignoring _is_paused — so a flip while paused
    lost the paused dimming."""

    def test_paused_counts_stay_dim(self, themed):
        from modules.downloads_view import _QueueAggregateBlock

        block = _QueueAggregateBlock()
        block._is_paused = True
        block._last_fraction = 0.5
        block._reapply_accent()
        qss = block._counts.styleSheet()
        # Dim (TEXT_DIM) in the dark family — NOT bright #ffffff.
        assert "rgba(255,255,255,0.7)" in qss
        assert "#ffffff" not in qss

    def test_active_counts_stay_bright(self, themed):
        from modules.downloads_view import _QueueAggregateBlock

        block = _QueueAggregateBlock()
        block._is_paused = False
        block._reapply_accent()
        assert "#ffffff" in block._counts.styleSheet()


class TestDisabledSliderFillsAreThemeAware:
    """The EQ + crossfade sliders' :disabled fills were hardcoded
    rgba(255,255,255,a) — off-theme (light grey on a near-white groove)
    on a light theme. They now use ink_alpha(a) so the disabled state
    flips with the theme. (Both QSS builders are pure, self-free.)"""

    def test_eq_slider_disabled_fill_flips(self, themed):
        from modules.settings_eq_page import EqSettingsPage

        # Pure QSS builder — never touches self.
        before = EqSettingsPage._eq_slider_qss(object())
        assert "255, 255, 255" not in before  # no hardcoded white left
        assert "rgba(255,255,255" in before  # ink_alpha(...) dark family
        _flip("frosted_light")
        after = EqSettingsPage._eq_slider_qss(object())
        assert after != before
        assert "rgba(0,0,0" in after  # ink flipped to near-black

    def test_horiz_slider_disabled_fill_flips(self, themed):
        from modules.settings_dialog import SettingsDialog

        before = SettingsDialog._horiz_slider_qss(object())
        assert "255, 255, 255" not in before
        _flip("frosted_light")
        after = SettingsDialog._horiz_slider_qss(object())
        assert after != before
        assert "rgba(0,0,0" in after
