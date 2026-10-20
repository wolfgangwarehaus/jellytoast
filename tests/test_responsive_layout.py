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
        from PySide6.QtWidgets import QWidget

        from jellytoast.top_bar import JtTopBar

        # Hosted, not top-level: the offscreen platform can re-size a bare
        # top-level window to its own default when the previous test's
        # window goes away, which made the width under test order-dependent.
        host = QWidget()
        t = JtTopBar(host, titlebar_mode=True)
        host.show()
        yield t
        host.deleteLater()

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

    def test_dropdown_centred_on_a_maximized_phone_bar(self, qapp, top, monkeypatch):
        # The live bug (window controls hiding without a bar resize left the
        # dropdown off-centre) only reproduced in the real app; this pins the
        # end state.
        win = top.window()
        monkeypatch.setattr(pc, "is_mobile_shell", lambda: True)
        monkeypatch.setattr(win, "isMaximized", lambda: False)
        top.set_collection("music")  # a library page: the dropdown shows
        top.resize(460, 48)
        top.update_window_controls()
        _settle(qapp)
        monkeypatch.setattr(win, "isMaximized", lambda: True)
        top.update_window_controls()
        _settle(qapp)
        vb = top.view_btn
        centre = vb.mapTo(top, vb.rect().center()).x()
        assert abs(centre - top.width() // 2) <= 2


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
        from PySide6.QtWidgets import QWidget

        from jellytoast.top_bar import JtTopBar

        host = QWidget()  # hosted: see TestTopBar.top
        top = JtTopBar(host, titlebar_mode=True)
        try:
            top.set_title("Music")
            top.set_available_libraries(
                [{"Id": "a", "Name": "Lossless Collection"}, {"Id": "b", "Name": "Lossy Archive"}]
            )
            top.set_library_controls_visible(True)
            host.show()
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
            host.deleteLater()

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


class TestTouchSizing:
    """On a phone shell every chrome button is a 48 px finger target (play
    56), and the transport bar's own tiers keep that from overflowing down
    to a 360 px phone. Desktop sizes don't change."""

    @pytest.fixture
    def bar_at(self, qapp, isolated_settings, monkeypatch):
        from PySide6.QtWidgets import QVBoxLayout, QWidget

        from jellytoast import now_playing_bar as _npb

        monkeypatch.setattr(_npb, "load_image_async", lambda *a, **k: None)
        made = []

        def make(width, mobile=True):
            monkeypatch.setattr(pc, "is_mobile_shell", lambda: mobile)
            host = QWidget()
            QVBoxLayout(host).setContentsMargins(0, 0, 0, 0)
            bar = _npb.NowPlayingBar(host)
            host.layout().addWidget(bar)
            host.resize(width, 200)
            host.show()
            qapp.processEvents()
            made.append(host)
            return bar

        yield make
        for h in made:
            h.deleteLater()

    def test_icon_buttons_grow_only_on_a_phone(self, qapp, monkeypatch):
        from PySide6.QtCore import QSize

        from jellytoast.icon_button import IconButton
        from jellytoast.ui_helpers import CoverOverlayButton

        for mobile, want in ((True, 48), (False, 34)):
            monkeypatch.setattr(pc, "is_mobile_shell", lambda m=mobile: m)
            b = IconButton()
            b.setIconSize(QSize(18, 18))
            b.setFixedSize(34, 34)
            assert b.width() == want
            assert b.iconSize().width() == (24 if mobile else 18)
        # Hover-revealed cover overlays stay small (touch never shows them).
        monkeypatch.setattr(pc, "is_mobile_shell", lambda: True)
        from PySide6.QtWidgets import QWidget

        host = QWidget()
        assert CoverOverlayButton(host, size=26).width() == 26

    @pytest.mark.parametrize("width", [360, 400, 420, 480, 540, 560, 600, 680, 720, 900])
    def test_phone_bar_fits_every_width(self, bar_at, width):
        bar = bar_at(width)
        assert bar.minimumSizeHint().width() <= width
        assert bar.layout().minimumSize().width() <= width
        visible = [b for b in (bar.shuffle_btn, bar.prev_btn, bar.next_btn, bar.repeat_btn,
                               bar.sleep_btn, bar.cast_btn, bar.vol_btn) if b.isVisibleTo(bar)]
        assert all(b.width() >= 48 and b.height() >= 48 for b in visible)
        assert bar.play_btn.width() == 56
        assert not bar.mini_btn.isVisibleTo(bar)

    @pytest.mark.parametrize(
        "width, out",
        [
            (900, set()),
            (700, {"sleep"}),
            (530, {"sleep", "shuffle", "repeat"}),
            (380, {"sleep", "shuffle", "repeat", "cast"}),
        ],
    )
    def test_phone_tiers_step_controls_out(self, bar_at, width, out):
        bar = bar_at(width)
        assert {k for k, hidden in bar.stepped_out().items() if hidden} == out

    def test_desktop_bar_unchanged(self, bar_at):
        bar = bar_at(900, mobile=False)
        assert bar.prev_btn.width() == 36 and bar.play_btn.width() == 44
        assert bar.height() == bar._BAR_HEIGHT
        assert not any(bar.stepped_out().values())

    def test_now_playing_row_carries_cast(self, bar_at, monkeypatch):
        from jellytoast.now_playing_page import NowPlayingPage
        from jellytoast.player_state import QueueContext

        bar = bar_at(380)
        qm = MagicMock()
        qm.context = QueueContext()
        qm.current_index = -1
        qm.current_item = None
        qm.original_items = []
        qm.queue = []
        page = NowPlayingPage(qm)
        page.attach_transport_bar(bar)
        page._apply_width_class(responsive.COMPACT)
        try:
            assert not page._stepped_btns["cast"].isHidden()
            hits = []
            bar.cast_requested.connect(lambda: hits.append(1))
            page._stepped_btns["cast"].click()
            assert hits == [1]
        finally:
            page.deleteLater()


class TestPhoneTopBar:
    """Touch + compact: back, the view dropdown, search and a ⋮ menu; the
    rest moves into the menu. A narrow desktop window keeps its buttons."""

    @pytest.fixture
    def top_for(self, qapp, monkeypatch):
        from PySide6.QtWidgets import QWidget

        from jellytoast.top_bar import JtTopBar

        hosts = []

        def make(mobile):
            monkeypatch.setattr(pc, "is_mobile_shell", lambda: mobile)
            host = QWidget()
            top = JtTopBar(host)
            top.set_available_libraries([{"Id": "a", "Name": "A"}, {"Id": "b", "Name": "B"}])
            top.set_library_controls_visible(True)
            host.show()
            top.resize(400, 60)
            _settle(qapp)
            hosts.append(host)
            return top

        yield make
        for h in hosts:
            h.deleteLater()

    def test_phone_moves_secondary_controls_to_the_menu(self, top_for):
        top = top_for(True)
        assert top._compact and not top.more_btn.isHidden()
        for w in (top.settings_btn, top.shuffle_all_btn, top.sort_btn, top.library_btn):
            assert w.isHidden()
        assert top.height() == 60 and top.search_btn.width() == 48

    def test_narrow_desktop_keeps_its_buttons(self, top_for):
        top = top_for(False)
        assert top._compact and top.more_btn.isHidden()
        for w in (top.settings_btn, top.shuffle_all_btn, top.sort_btn, top.library_btn):
            assert not w.isHidden()

    def test_menu_offers_what_was_hidden(self, top_for, monkeypatch):
        top = top_for(True)
        seen = []
        monkeypatch.setattr(
            type(top), "_exec_menu_with_kbd_grab",
            lambda self, menu, pt: seen.extend(a.text() for a in menu.actions() if a.text()),
        )
        top._show_more_menu()
        for label in ("Libraries…", "Shuffle all", "Sort…", "Show as list", "Home", "Settings"):
            assert label in seen


def test_phone_top_bar_recentres_the_dropdown(qapp, monkeypatch):
    """Going compact on a phone shows ⋮ and hides settings / library: the
    view dropdown must re-centre afterwards, not keep its old position."""
    from PySide6.QtWidgets import QWidget

    from jellytoast.top_bar import JtTopBar

    monkeypatch.setattr(pc, "is_mobile_shell", lambda: True)
    host = QWidget()
    top = JtTopBar(host)
    try:
        top.view_btn.setText("Albums")
        top.view_btn.show()
        host.show()
        top.resize(900, 60)
        _settle(qapp)
        top.resize(400, 60)
        for _ in range(3):
            _settle(qapp)
        centre = top._center_cluster.geometry().x() + top.view_btn.width() // 2
        assert abs(centre - top.width() // 2) <= 12
    finally:
        host.deleteLater()


class TestCompactNowPlayingCover:
    """Phone width: the cover grows toward the screen width on the Playing
    tab, moderately while lyrics share the pane, and back to the desktop
    size at regular width."""

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
        p.resize(400, 700)
        p.show()
        yield p
        p.deleteLater()

    def test_sizes(self, qapp, page):
        page._apply_width_class(responsive.COMPACT)
        _settle(qapp)
        big = page._cover.width()
        assert big > type(page).COVER_SIZE and big <= page._COMPACT_COVER_MAX
        page._lyrics_scroll.show()
        page._fit_compact_cover()
        assert page._cover.width() <= page._COMPACT_COVER_SHARED
        page._lyrics_scroll.hide()
        page._apply_width_class(responsive.REGULAR)
        assert page._cover.width() == type(page).COVER_SIZE


def test_queue_controls_row_hidden_while_browsing(qapp, isolated_settings, monkeypatch):
    """Browsing an album (preview) on a phone: no shuffle / repeat / sleep /
    cast row — those drive the live queue, not the album being looked at."""
    from PySide6.QtWidgets import QWidget

    from jellytoast import now_playing_bar as _npb
    from jellytoast.now_playing_page import NowPlayingPage
    from jellytoast.player_state import QueueContext

    monkeypatch.setattr(_npb, "load_image_async", lambda *a, **k: None)
    monkeypatch.setattr(pc, "is_mobile_shell", lambda: False)
    from PySide6.QtWidgets import QVBoxLayout

    host = QWidget()
    QVBoxLayout(host).setContentsMargins(0, 0, 0, 0)
    bar = _npb.NowPlayingBar(host)
    host.layout().addWidget(bar)
    host.resize(400, 200)
    host.show()
    _settle(qapp)
    qm = MagicMock()
    qm.context = QueueContext()
    qm.current_index = -1
    qm.current_item = None
    qm.original_items = []
    qm.queue = []
    page = NowPlayingPage(qm)
    try:
        page.attach_transport_bar(bar)
        page._apply_width_class(responsive.COMPACT)
        assert not page._stepped_row.isHidden()
        page._preview_id = "album-1"
        page._label_compact_tabs(True)
        assert page._stepped_row.isHidden()
        assert page._tab_playing.text() == "Album"
        page._preview_id = ""
        page._label_compact_tabs(False)
        assert not page._stepped_row.isHidden()
        assert page._tab_playing.text() == "Playing"
    finally:
        page.deleteLater()
        host.deleteLater()


class TestPhoneMiniPlayer:
    """At phone width (mobile shell) the bar is a mini player — cover ·
    title / artist · play · next over a thin progress line — and the full
    transport comes back while the live Now Playing page is up."""

    @pytest.fixture
    def bar_at(self, qapp, isolated_settings, monkeypatch):
        from PySide6.QtWidgets import QVBoxLayout, QWidget

        from jellytoast import now_playing_bar as _npb

        monkeypatch.setattr(_npb, "load_image_async", lambda *a, **k: None)
        made = []

        def make(width, mobile=True):
            monkeypatch.setattr(pc, "is_mobile_shell", lambda: mobile)
            host = QWidget()
            QVBoxLayout(host).setContentsMargins(0, 0, 0, 0)
            bar = _npb.NowPlayingBar(host)
            host.layout().addWidget(bar)
            host.resize(width, 300)
            host.show()
            qapp.processEvents()
            made.append(host)
            return bar

        yield make
        for h in made:
            h.deleteLater()

    @pytest.mark.parametrize("width", [360, 400, 540])
    def test_phone_width_shows_the_mini_player(self, bar_at, width):
        bar = bar_at(width)
        assert bar.is_mini()
        assert bar.height() == bar._MINI_HEIGHT
        assert bar.layout().minimumSize().width() <= width
        assert bar.mini_play_btn.width() >= 48 and bar.mini_next_btn.width() >= 48

    @pytest.mark.parametrize("width", [360, 400, 420])
    def test_now_playing_brings_back_the_full_transport(self, qapp, bar_at, width):
        bar = bar_at(width)
        bar.set_now_playing_open(True)
        qapp.processEvents()
        assert not bar.is_mini()
        assert bar.seek_bar.isVisibleTo(bar) and bar.prev_btn.isVisibleTo(bar)
        assert bar.layout().minimumSize().width() <= width
        bar.set_now_playing_open(False)
        qapp.processEvents()
        assert bar.is_mini()

    def test_wide_phone_and_desktop_keep_the_full_bar(self, bar_at):
        assert not bar_at(700).is_mini()
        assert not bar_at(400, mobile=False).is_mini()

    def test_mini_mirrors_title_play_state_and_progress(self, qapp, bar_at, monkeypatch):
        from types import SimpleNamespace

        from jellytoast import now_playing_bar as _npb
        from jellytoast.icons import icon

        bar = bar_at(400)
        bar._track_title, bar._track_subtitle = "Project Wildfire", "Binaerpilot"
        bar._apply_text_layout(bar.width())
        assert bar.mini_title.text() == "Project Wildfire"
        assert bar.mini_artist.text() == "Binaerpilot"
        pause = icon("pause")
        bar._set_play_icon(pause)
        assert bar.mini_play_btn.icon().cacheKey() == bar.play_btn.icon().cacheKey()
        monkeypatch.setattr(_npb, "get_now_playing", lambda: SimpleNamespace(duration=200_000))
        bar._on_position(50_000)
        assert bar.mini_progress._fraction == pytest.approx(0.25)

    def test_tapping_the_mini_title_opens_now_playing(self, qapp, bar_at):
        from PySide6.QtCore import QPoint, Qt
        from PySide6.QtTest import QTest

        bar = bar_at(400)
        opened = []
        bar.show_now_playing_requested.connect(lambda: opened.append(1))
        QTest.mouseClick(bar.mini_title, Qt.MouseButton.LeftButton, pos=QPoint(10, 5))
        assert opened == [1]
        # Pressed then released elsewhere (a swipe that left): no open.
        area = bar.mini_title.parentWidget()
        QTest.mousePress(area, Qt.MouseButton.LeftButton, pos=QPoint(5, 5))
        QTest.mouseRelease(area, Qt.MouseButton.LeftButton, pos=QPoint(-500, 5))
        assert opened == [1]


def test_phone_popups_are_opaque(monkeypatch):
    """Plasma Mobile claims blur but nothing blurs behind a popup there: menus
    must be opaque on a phone shell regardless of the theme / blur status."""
    from jellytoast import blur, ui_helpers

    monkeypatch.setattr(blur, "status", lambda: blur.BlurStatus.ACTIVE)
    monkeypatch.setattr(pc, "is_mobile_shell", lambda: True)
    assert ui_helpers.popup_blur_active() is False


def test_tap_opened_menu_drops_the_buttons_focus_ring(qapp, monkeypatch):
    from PySide6.QtWidgets import QApplication, QMenu, QWidget

    from jellytoast import keyboard_focus
    from jellytoast.top_bar import JtTopBar

    host = QWidget()
    top = JtTopBar(host, titlebar_mode=True)
    host.show()
    host.activateWindow()
    class _Menu(QMenu):  # stands in for the modal exec: just park focus
        def exec(self, *_a):
            top.view_btn.setFocus()

    keyboard_focus.note_pointer_input()
    top._exec_menu_with_kbd_grab(_Menu(), None)
    assert QApplication.focusWidget() is not top.view_btn
    keyboard_focus.note_keyboard_input()  # keyboard user: keep the ring
    top._exec_menu_with_kbd_grab(_Menu(), None)
    assert QApplication.focusWidget() is top.view_btn or not top.view_btn.isVisible()
    keyboard_focus.note_pointer_input()
    host.deleteLater()


def test_top_bar_focus_ring_only_for_keyboard_focus(qapp):
    """A page swap / click handing focus to "Now Playing" must not light the
    accent ring (the stuck purple outline); Tab focus still does."""
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QWidget

    from jellytoast import keyboard_focus
    from jellytoast.top_bar import JtTopBar

    host = QWidget()
    top = JtTopBar(host, titlebar_mode=True)
    host.show()
    host.activateWindow()
    _settle(qapp)
    btn = top.view_btn
    btn.show()
    keyboard_focus.note_pointer_input()
    btn.setFocus(Qt.FocusReason.OtherFocusReason)
    _settle(qapp)
    assert not btn.property("kbdFocus")
    btn.clearFocus()
    btn.setFocus(Qt.FocusReason.TabFocusReason)
    _settle(qapp)
    assert btn.property("kbdFocus") is True
    assert 'QPushButton[kbdFocus="true"]:focus' in btn.styleSheet()
    host.deleteLater()


def test_no_top_bar_focus_ring_on_a_phone_even_from_keys(qapp, monkeypatch):
    """A phone (streamed input can include stray key events) never shows the
    accent ring on "Now Playing"; docked keeps it for keyboard users."""
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QWidget

    from jellytoast import keyboard_focus, touch
    from jellytoast.top_bar import JtTopBar

    monkeypatch.setattr(pc, "is_mobile_shell", lambda: True)
    monkeypatch.setattr(pc, "is_docked", lambda: False)
    monkeypatch.setattr(touch, "_hover_shell", (-1e9, True))
    host = QWidget()
    top = JtTopBar(host, titlebar_mode=True)
    host.show()
    host.activateWindow()
    _settle(qapp)
    btn = top.view_btn
    btn.show()
    keyboard_focus.note_keyboard_input()
    btn.setFocus(Qt.FocusReason.TabFocusReason)
    _settle(qapp)
    assert not btn.property("kbdFocus")
    keyboard_focus.note_pointer_input()
    host.deleteLater()


def test_phone_top_bar_buttons_take_no_focus(qapp, monkeypatch):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QPushButton, QWidget

    from jellytoast import touch
    from jellytoast.top_bar import JtTopBar

    monkeypatch.setattr(pc, "is_mobile_shell", lambda: True)
    monkeypatch.setattr(pc, "is_docked", lambda: False)
    monkeypatch.setattr(touch, "_hover_shell", (-1e9, True))
    host = QWidget()
    top = JtTopBar(host, titlebar_mode=True)
    _settle(qapp)
    assert all(
        b.focusPolicy() == Qt.FocusPolicy.NoFocus for b in top.findChildren(QPushButton)
    )
    host.deleteLater()


def test_phone_popups_are_fully_opaque(monkeypatch):
    from jellytoast import ui_helpers

    monkeypatch.setattr(pc, "is_mobile_shell", lambda: True)
    monkeypatch.setattr(pc, "is_docked", lambda: False)
    assert ui_helpers._popup_fill_opaque_on_macos("rgba(67, 67, 67, 0.65)") == "rgb(67, 67, 67)"
    monkeypatch.setattr(pc, "is_mobile_shell", lambda: False)
    if not ui_helpers.IS_MACOS:
        assert ui_helpers._popup_fill_opaque_on_macos("rgba(67, 67, 67, 0.65)").endswith("0.65)")
