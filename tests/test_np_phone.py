"""Phone Now Playing (np_phone): on a mobile shell at compact width the live
page becomes one-surface player — it borrows the page's cover / lyrics /
queue pane and hands them back when it steps aside (preview, wide)."""

from unittest.mock import MagicMock

import pytest

from jellytoast import platform_compat as pc
from jellytoast import responsive


@pytest.fixture
def page(qapp, isolated_settings, monkeypatch):
    from PySide6.QtWidgets import QWidget

    from jellytoast import now_playing_bar as _npb
    from jellytoast.now_playing_page import NowPlayingPage
    from jellytoast.player_state import QueueContext

    monkeypatch.setattr(_npb, "load_image_async", lambda *a, **k: None)
    monkeypatch.setattr(pc, "is_mobile_shell", lambda: True)
    host = QWidget()
    bar = _npb.NowPlayingBar(host)
    qm = MagicMock()
    qm.context = QueueContext()
    qm.current_index = -1
    qm.current_item = None
    qm.original_items = []
    qm.queue = []
    p = NowPlayingPage(qm)
    p.attach_transport_bar(bar)
    p.resize(402, 740)
    yield p
    p.deleteLater()
    host.deleteLater()


def test_compact_phone_takes_over_and_hands_back(page):
    seen = []
    page.phone_player_changed.connect(seen.append)
    page._apply_width_class(responsive.COMPACT)
    assert page.phone_player_active() and seen == [True]
    assert page._cover.parentWidget() is page._phone_cover_block
    assert page._right_pane.parentWidget() is page._phone_pane
    assert page._lyrics_scroll.parentWidget() is page._phone_pane
    assert page._compact_tabs.isHidden()

    page._apply_width_class(responsive.REGULAR)
    assert not page.phone_player_active() and seen == [True, False]
    assert page._cover.parentWidget() is page._left_pane
    assert page._right_pane.parentWidget() is page
    assert page._left_v.indexOf(page._lyrics_scroll) == page._lyrics_home_idx


def test_surfaces_swap_one_at_a_time(page):
    page._apply_width_class(responsive.COMPACT)
    page.show()
    page._phone_toggle_surface("queue")
    assert page._phone_surface == "queue"
    assert page._right_pane.isVisibleTo(page) and not page._phone_cover_block.isVisibleTo(page)
    assert page._phone_header.isVisibleTo(page)
    page._phone_toggle_surface("queue")  # same toggle again → back to the cover
    assert page._phone_surface == "cover"
    assert page._phone_cover_block.isVisibleTo(page) and not page._phone_header.isVisibleTo(page)
    page._phone_toggle_surface("lyrics")  # no lyrics loaded → says so
    assert page._phone_no_lyrics.isVisibleTo(page)


def test_not_on_a_desktop_shell(page, monkeypatch):
    monkeypatch.setattr(pc, "is_mobile_shell", lambda: False)
    page._apply_width_class(responsive.COMPACT)
    assert not page.phone_player_active()


def test_album_page_renders_grid_known_item_before_the_server_answers(qapp, isolated_settings, monkeypatch):
    """A Navidrome mid-scan can take 15 s to answer getAlbum; the album page
    showed a blank "Loading…" meanwhile. The grid already has the album —
    render that at once."""
    from unittest.mock import MagicMock

    from jellytoast import disk_cache, library_grid
    from jellytoast import now_playing_page as npp
    from jellytoast.player_state import QueueContext

    monkeypatch.setattr(disk_cache, "load", lambda *a, **k: None)
    monkeypatch.setattr(npp, "run_async", lambda *a, **k: None)  # server never answers
    monkeypatch.setattr(npp, "load_image_async", lambda *a, **k: None)
    library_grid._remember_items([{"Id": "alb1", "Name": "25", "AlbumArtist": "Adele"}])
    qm = MagicMock()
    qm.context = QueueContext()
    qm.current_index = -1
    qm.current_item = None
    qm.original_items = []
    qm.queue = []
    page = npp.NowPlayingPage(qm)
    page.load_preview("alb1", "album")
    assert page._title.text() == "25"
    assert "Adele" in page._subtitle.text()
    page.load_preview("unknown", "album")
    assert page._title.text() == "Loading…"
    page.deleteLater()
