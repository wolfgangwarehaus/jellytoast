"""Phone-width layouts (Plasma Mobile phase 2).

- Width classes flip at responsive.COMPACT_BELOW and broadcast once.
- The transport bar never demands more width than it has (the old fixed
  breakpoint clusters ratcheted the window at 740 px), and goes compact on a
  phone.
- The top bar sheds its secondary controls at phone width; on a mobile shell
  the window controls go while the shell holds the window maximized.
- No content page holds the window wider than a phone (the fixed 420 px
  login card used to — a hidden stack page still counts).
- Settings swaps its sidebar for a section dropdown at compact width.
- Now Playing shows one pane at a time at compact width, even when the
  class flips while the page is hidden.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from jellytoast import platform_compat as pc
from jellytoast import responsive


@pytest.fixture(autouse=True)
def regular_width_class():
    responsive._current = responsive.REGULAR
    yield
    responsive._current = responsive.REGULAR


def _settle(qapp):
    for _ in range(3):
        qapp.processEvents()


class TestWidthClass:
    def test_threshold(self):
        assert responsive.width_class(359) == responsive.COMPACT
        assert responsive.width_class(responsive.COMPACT_BELOW - 1) == responsive.COMPACT
        assert responsive.width_class(responsive.COMPACT_BELOW) == responsive.REGULAR

    def test_update_broadcasts_only_on_change(self, qapp):
        from jellytoast.player_state import PlayerBus

        seen = []
        PlayerBus.get().width_class_changed.connect(seen.append)
        try:
            assert responsive.update(1000) is False
            assert responsive.update(400) is True
            assert responsive.update(380) is False
            assert responsive.update(900) is True
            assert seen == [responsive.COMPACT, responsive.REGULAR]
            assert not responsive.is_compact()
        finally:
            PlayerBus.get().width_class_changed.disconnect(seen.append)


@pytest.fixture
def bar(qapp, monkeypatch):
    from jellytoast import now_playing_bar as _npb

    monkeypatch.setattr(_npb, "load_image_async", lambda *a, **k: None)
    monkeypatch.setattr(pc, "is_mobile_shell", lambda: False)
    # A child of a layout-less host, like the real bar inside the window:
    # a TOP-LEVEL widget's own layout would clamp resize() to its minimum.
    from PySide6.QtWidgets import QWidget

    host = QWidget()
    host.resize(1400, 200)
    b = _npb.NowPlayingBar(host)
    host.show()
    b.set_left_cluster_visible(True)
    b.resize(1300, 108)
    yield b
    host.deleteLater()


class TestTransportBar:
    def test_never_needs_more_than_its_width(self, qapp, bar):
        # Shrinking from wide to phone width, the bar's minimum must never
        # exceed the width it was given — that inversion is what made the
        # window grow to 740 and refuse to shrink back.
        for width in range(1300, 359, -10):
            bar.resize(width, bar.height())
            _settle(qapp)
            assert bar.minimumSizeHint().width() <= 360, width
            # ...and its contents really do fit what it was given.
            assert bar.layout().minimumSize().width() <= width, width

    def test_a_jump_straight_to_phone_width_reflows(self, qapp, bar):
        bar.resize(1300, 108)
        _settle(qapp)
        bar.resize(360, 108)
        _settle(qapp)
        assert bar.width() == 360
        assert bar.layout().minimumSize().width() <= 360
        assert bar.height() == bar._COMPACT_BAR_HEIGHT

    def test_compact_layout(self, qapp, bar):
        bar.resize(400, 108)
        _settle(qapp)
        assert bar.height() == bar._COMPACT_BAR_HEIGHT == bar.thumb.width()
        assert bar.sleep_btn.isHidden() and bar.mini_btn.isHidden()
        assert bar.shuffle_btn.isHidden() and bar.repeat_btn.isHidden()
        assert not bar.play_btn.isHidden() and not bar.cast_btn.isHidden()
        assert bar.streaming_info.isHidden()

    def test_regular_layout(self, qapp, bar):
        bar.resize(400, 108)
        _settle(qapp)
        bar.resize(1000, 108)
        _settle(qapp)
        assert bar.height() == bar._BAR_HEIGHT
        for btn in (bar.sleep_btn, bar.mini_btn, bar.shuffle_btn, bar.repeat_btn):
            assert not btn.isHidden()

    def test_no_mini_player_button_on_a_mobile_shell(self, qapp, bar, monkeypatch):
        monkeypatch.setattr(pc, "is_mobile_shell", lambda: True)
        bar.resize(1000, 108)
        _settle(qapp)
        assert bar.mini_btn.isHidden()


class TestTopBar:
    @pytest.fixture
    def top(self, qapp):
        from jellytoast.top_bar import JtTopBar

        t = JtTopBar(titlebar_mode=True)
        t.show()
        yield t
        t.deleteLater()

    def test_compact_sheds_secondary_controls(self, qapp, top):
        top.resize(360, 48)
        _settle(qapp)
        for w in (top.fwd_btn, top.home_btn, top.view_mode_btn, top.title_label):
            assert w.isHidden()
        assert not top.back_btn.isHidden() and not top.search_btn.isHidden()
        top.resize(1000, 48)
        _settle(qapp)
        for w in (top.fwd_btn, top.home_btn, top.view_mode_btn, top.title_label):
            assert not w.isHidden()

    def test_window_controls_follow_mobile_maximize(self, qapp, top, monkeypatch):
        win = top.window()
        monkeypatch.setattr(pc, "is_mobile_shell", lambda: True)
        monkeypatch.setattr(win, "isMaximized", lambda: True)
        top.update_window_controls()
        assert top.close_btn.isHidden() and top.min_btn.isHidden()
        monkeypatch.setattr(win, "isMaximized", lambda: False)  # un-maximized
        top.update_window_controls()
        assert not top.close_btn.isHidden()
        # Docked mode: still maximized, but the shell gives windows their
        # decorations back — and this bar is ours.
        monkeypatch.setattr(win, "isMaximized", lambda: True)
        monkeypatch.setattr(pc, "is_docked", lambda: True)
        top.update_window_controls()
        assert not top.close_btn.isHidden()
        monkeypatch.setattr(pc, "is_docked", lambda: False)
        monkeypatch.setattr(pc, "is_mobile_shell", lambda: False)
        monkeypatch.setattr(win, "isMaximized", lambda: True)
        top.update_window_controls()
        assert not top.close_btn.isHidden()


def test_login_view_fits_a_phone(qapp, isolated_settings):
    from jellytoast.login_view import LoginView

    view = LoginView()
    try:
        assert view.minimumSizeHint().width() <= 360
    finally:
        view.deleteLater()


class TestSettingsDialog:
    def test_regular_keeps_the_sidebar(self, qapp, isolated_settings):
        from jellytoast.settings_dialog import SettingsDialog

        dlg = SettingsDialog()
        try:
            assert not dlg.nav.isHidden()
            assert not hasattr(dlg, "_section_picker")
            assert dlg.minimumWidth() == dlg.maximumWidth()  # fixed desktop size
        finally:
            dlg.deleteLater()

    def test_compact_uses_a_section_dropdown(self, qapp, isolated_settings):
        from jellytoast.settings_dialog import SettingsDialog

        responsive._current = responsive.COMPACT
        dlg = SettingsDialog()
        try:
            assert dlg.nav.isHidden()
            picker = dlg._section_picker
            assert dlg.minimumWidth() == 0  # resizable / maximizable
            picker.setCurrentIndex(2)
            assert dlg.nav.currentRow() == 2
            assert dlg.show_page("General") is True  # deep links still sync
            assert picker.currentIndex() == dlg.nav.currentRow() == 0
        finally:
            dlg.deleteLater()


class TestNowPlayingCompact:
    @pytest.fixture
    def page(self, qapp, isolated_settings):
        from jellytoast.now_playing_page import NowPlayingPage
        from jellytoast.player_state import QueueContext

        qm = MagicMock()
        qm.context = QueueContext()
        qm.current_index = -1
        qm.current_item = None
        qm.original_items = []
        qm.queue = []
        p = NowPlayingPage(qm)
        yield p
        p.deleteLater()

    def test_one_pane_at_a_time_even_when_flipped_while_hidden(self, page):
        assert page.isHidden()  # e.g. a background page in the content stack
        page._apply_width_class(responsive.COMPACT)
        assert not page._compact_tabs.isHidden()
        assert not page._left_pane.isHidden() and page._right_pane.isHidden()
        page._tab_tracks.setChecked(True)
        assert page._left_pane.isHidden() and not page._right_pane.isHidden()

    def test_regular_shows_both_panes(self, page):
        page._apply_width_class(responsive.COMPACT)
        page._tab_tracks.setChecked(True)
        page._apply_width_class(responsive.REGULAR)
        assert page._compact_tabs.isHidden()
        assert not page._left_pane.isHidden() and not page._right_pane.isHidden()


class TestRealDeviceFindings:
    """Found on august's Plasma Mobile session (601 px wide, two libraries)."""

    def test_top_bar_compacts_when_the_library_picker_does_not_fit(self, qapp, monkeypatch):
        from jellytoast.top_bar import JtTopBar

        top = JtTopBar(titlebar_mode=True)
        try:
            top.set_title("Music")
            top.set_available_libraries(
                [{"Id": "a", "Name": "Lossless Collection"}, {"Id": "b", "Name": "Lossy Archive"}]
            )
            top.set_library_controls_visible(True)
            top.show()
            # Above the fixed 520 floor but too tight for the full bar with the
            # picker (~575 px in the default font; august's wider monospace font
            # overflowed at his 600 px screen).
            top.resize(560, 48)
            _settle(qapp)
            _settle(qapp)
            assert top._compact, "the full bar overlapped search at 560 px"
            # Plenty of room again → the full bar comes back.
            top.resize(1400, 48)
            _settle(qapp)
            assert not top._compact
            assert not top.fwd_btn.isHidden()
        finally:
            top.deleteLater()

    def test_no_blur_body_is_opaque_on_a_mobile_shell(self, monkeypatch):
        from jellytoast.blur import BlurStatus
        from jellytoast.theme import body_color_for, get_active_theme

        theme = get_active_theme()
        if not theme.blur or theme.fallback_body_alpha is None:
            pytest.skip("active theme isn't frosted")
        monkeypatch.setattr(pc, "is_mobile_shell", lambda: True)
        assert body_color_for(theme, BlurStatus.UNSUPPORTED)[3] == 255
        monkeypatch.setattr(pc, "is_mobile_shell", lambda: False)
        assert body_color_for(theme, BlurStatus.UNSUPPORTED)[3] == theme.fallback_body_alpha


class TestSteppedOutControls:
    """At phone width the transport bar hides shuffle / repeat / the sleep
    timer; the compact Now Playing page carries exactly those, driving the
    bar's own buttons (one source of truth)."""

    @pytest.fixture
    def pair(self, qapp, isolated_settings, monkeypatch):
        from PySide6.QtWidgets import QWidget

        from jellytoast import now_playing_bar as _npb
        from jellytoast.now_playing_page import NowPlayingPage
        from jellytoast.player_state import QueueContext

        monkeypatch.setattr(_npb, "load_image_async", lambda *a, **k: None)
        monkeypatch.setattr(pc, "is_mobile_shell", lambda: False)
        host = QWidget()
        host.resize(1400, 200)
        bar = _npb.NowPlayingBar(host)
        host.show()
        qm = MagicMock()
        qm.context = QueueContext()
        qm.current_index = -1
        qm.current_item = None
        qm.original_items = []
        qm.queue = []
        page = NowPlayingPage(qm)
        page.attach_transport_bar(bar)
        page._apply_width_class(responsive.COMPACT)
        yield bar, page
        page.deleteLater()
        host.deleteLater()

    def test_row_carries_what_the_bar_hid(self, qapp, pair):
        bar, page = pair
        bar.resize(1000, 108)
        _settle(qapp)
        assert page._stepped_row.isHidden()  # bar has everything
        bar.resize(500, 108)  # compact: sleep steps out
        _settle(qapp)
        assert not page._stepped_row.isHidden()
        assert not page._stepped_btns["sleep"].isHidden()
        assert page._stepped_btns["shuffle"].isHidden()
        bar.resize(380, 108)  # narrow: shuffle + repeat too
        _settle(qapp)
        assert not page._stepped_btns["shuffle"].isHidden()
        assert not page._stepped_btns["repeat"].isHidden()

    def test_page_buttons_drive_the_bar(self, qapp, pair):
        bar, page = pair
        bar.resize(380, 108)
        _settle(qapp)
        assert not bar.shuffle_btn.isChecked()
        page._stepped_btns["shuffle"].click()
        assert bar.shuffle_btn.isChecked()
        page._stepped_btns["repeat"].click()
        assert bar._repeat_state == "all"

    def test_hidden_at_regular_width(self, qapp, pair):
        bar, page = pair
        bar.resize(380, 108)
        _settle(qapp)
        page._apply_width_class(responsive.REGULAR)
        assert page._stepped_row.isHidden()
