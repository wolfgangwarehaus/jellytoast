"""QA pass, 2026-10-26: small-window clipping and art placeholders.

- Radio station rows: a long stream URL set the row's minimum width, so at
  378 px the cards ran past the page edge. ElidedLabel ends it with "…".
- Horizontal rails fade a tile out at an edge with more to scroll, instead
  of cutting it mid-word ("Rosalía Vila Tobe").
- An artist with no photo (Subsonic: no coverArt) shows the themed
  placeholder; asking anyway fetched Navidrome's white star stand-in.
"""

from __future__ import annotations

import jellytoast.artist_page as ap


def test_elided_label_shrinks_and_keeps_the_full_text(qapp):
    from jellytoast.ui_helpers import ElidedLabel

    url = "https://kexp-mp3-128.streamguys1.com/kexp128.mp3"
    lbl = ElidedLabel(url)
    assert lbl.minimumSizeHint().width() < 60
    assert lbl.text() == url
    lbl.resize(120, 20)
    lbl.grab()  # paint once: the cut text puts the full one in the tooltip
    assert lbl.toolTip() == url
    lbl.resize(2000, 20)
    lbl.grab()
    assert lbl.toolTip() == ""


def test_radio_station_row_fits_a_narrow_page(qapp):
    from jellytoast.radio_view import _StationRow

    row = _StationRow(
        {"name": "KEXP 90.3 Seattle", "streamUrl": "https://kexp-mp3-128.streamguys1.com/kexp128.mp3"}
    )
    # 378 px window − page margins: the row must fit well under 300.
    assert row.minimumSizeHint().width() < 260


def _rail(qapp, n):
    from jellytoast.horizontal_rail import HorizontalRail

    rail = HorizontalRail("Artists", "artist")
    rail.resize(400, rail.HEIGHT)
    items = [{"Id": f"a{i}", "Name": f"Artist {i}", "ImageTags": {}} for i in range(n)]
    rail.set_items(items)
    rail.show()
    for _ in range(3):
        qapp.processEvents()
    return rail


def test_rail_fades_only_the_edge_with_more_to_scroll(qapp):
    rail = _rail(qapp, 12)
    view = rail._view
    assert view.horizontalScrollBar().maximum() > 0
    assert view.viewport().graphicsEffect() is not None  # more to the right
    view.horizontalScrollBar().setValue(view.horizontalScrollBar().maximum())
    assert view.viewport().graphicsEffect() is not None  # more to the left
    rail.deleteLater()

    short = _rail(qapp, 1)
    assert short._view.viewport().graphicsEffect() is None  # all of it fits
    short.deleteLater()


def test_rail_does_not_fetch_art_the_server_says_is_missing(qapp, monkeypatch):
    import jellytoast.horizontal_rail as hr

    asked = []

    class _Api:
        def get_image_url(self, *a, **k):
            asked.append(a)
            return "http://x/img"

    monkeypatch.setattr(hr, "get_provider", lambda: _Api())
    monkeypatch.setattr(hr, "load_image_async", lambda *a, **k: None)
    rail = hr.HorizontalRail("Artists", "artist")
    rail.set_items(
        [
            {"Id": "noart", "Name": "Rosalía Vila Tobella", "ImageTags": {}},
            {"Id": "art", "Name": "Rosalia", "ImageTags": {"Primary": "ar-1"}},
        ]
    )
    assert [a[0] for a in asked] == ["art"]
    rail.deleteLater()


def test_artist_without_a_photo_keeps_the_themed_placeholder(qapp, monkeypatch):
    asked = []
    page = ap.ArtistPage()
    monkeypatch.setattr(page.api, "get_image_url", lambda *a, **k: asked.append(a) or "")
    monkeypatch.setattr(page, "_fetch_top_songs", lambda *a, **k: None)
    page._artist_id = "noart"
    page._show_cover_placeholder()
    placeholder = page._cover.pixmap().cacheKey()
    page._on_meta_loaded("noart", {"Id": "noart", "Name": "Rosalía Vila Tobella", "ImageTags": {}})
    assert asked == []
    assert page._cover.pixmap().cacheKey() == placeholder
    page.deleteLater()
