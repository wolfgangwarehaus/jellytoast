"""Artist album-tile (and header) cover loads are async; a pixmap that
resolves AFTER the user has navigated to a different artist must be dropped,
not painted onto the new artist's tiles. The entry guard in
``_on_albums_loaded`` only covers the synchronous dispatch — the per-tile
callbacks now re-check the still-current ``_artist_id``.

Driven on a real ArtistPage with load_image_async stubbed to a recorder, so
no network and no Qt painting (mirrors test_artist_page_info_line).
"""

from __future__ import annotations

import jellytoast.artist_page as ap


def _capture_tile_callbacks(monkeypatch):
    captured = []

    def _rec(key, url, w, h, on_result, **k):
        if "artistalbumtile" in key:
            captured.append(on_result)

    monkeypatch.setattr(ap, "load_image_async", _rec)
    return captured


def test_album_tile_cover_does_not_bleed_after_artist_switch(qapp, monkeypatch):
    from PySide6.QtGui import QPixmap

    captured = _capture_tile_callbacks(monkeypatch)
    page = ap.ArtistPage()
    monkeypatch.setattr(page.api, "get_image_url", lambda *a, **k: "http://x/cover")

    page._artist_id = "A"
    page._on_albums_loaded("A", [{"Id": "albA0", "Name": "a0"}])
    assert captured  # one tile cover callback was dispatched for artist A

    set_cover_calls = []
    monkeypatch.setattr(
        page._model, "set_cover", lambda r, pix: set_cover_calls.append((r, pix))
    )

    # User navigates to a different artist before A's cover resolves.
    page._artist_id = "B"
    pix = QPixmap(4, 4)
    pix.fill()
    for cb in captured:
        cb(pix)

    # The stale A pixmap is dropped — it must NOT paint onto B's model.
    assert set_cover_calls == []


def test_album_tile_cover_sets_when_artist_unchanged(qapp, monkeypatch):
    from PySide6.QtGui import QPixmap

    captured = _capture_tile_callbacks(monkeypatch)
    page = ap.ArtistPage()
    monkeypatch.setattr(page.api, "get_image_url", lambda *a, **k: "http://x/cover")

    page._artist_id = "A"
    page._on_albums_loaded("A", [{"Id": "albA0", "Name": "a0"}])

    set_cover_calls = []
    monkeypatch.setattr(
        page._model, "set_cover", lambda r, pix: set_cover_calls.append((r, pix))
    )

    pix = QPixmap(4, 4)
    pix.fill()
    for cb in captured:
        cb(pix)

    # Still on artist A → the cover IS applied to row 0.
    assert len(set_cover_calls) == 1
    assert set_cover_calls[0][0] == 0
