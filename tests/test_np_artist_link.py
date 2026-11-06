"""An album preview's artist is a link to the artist's page (desktop)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from jellytoast.player_state import QueueKind


@pytest.fixture
def page(qapp, isolated_settings):
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


def _meta(**kw):
    base = {"Name": "Album", "AlbumArtist": "Air & Co", "AlbumArtists": [{"Id": "ar7", "Name": "Air & Co"}]}
    base.update(kw)
    return base


def test_album_preview_artist_is_a_link_that_asks_for_their_page(page):
    page._preview_kind = QueueKind.ALBUM
    html = page._artist_link(_meta(), "Air & Co")
    assert 'href="ar7"' in html and "Air &amp; Co" in html
    got = []
    page.artist_browse_requested.connect(got.append)
    page._subtitle.linkActivated.emit("ar7")
    assert got == ["ar7"]


def test_no_link_without_an_id_or_for_playlists(page):
    page._preview_kind = QueueKind.ALBUM
    assert page._artist_link(_meta(AlbumArtists=[]), "Air") == "Air"
    page._preview_kind = QueueKind.PLAYLIST
    assert page._artist_link(_meta(), "Curator") == "Curator"
