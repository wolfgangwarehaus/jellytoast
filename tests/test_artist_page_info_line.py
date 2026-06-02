"""Artist sub-header album-count must survive either async resolve order.

`load_artist` fires two independent async fetches — artist meta (genre) and
the artist's albums (count). Each had its own handler that REPLACED the whole
info line, so whichever resolved LAST won: when meta resolved after albums,
`_on_meta_loaded` overwrote the count with a genre-only (or blank) line. A
single-album artist with no genre therefore showed an empty sub-header even
though its album was loaded and rendered. Found live on Navidrome 2026-06-02
(ABBA: rows=1 but info=""). Both handlers now call `_rebuild_info()`, which
composes the line from shared state, so order no longer matters.
"""

from __future__ import annotations

import modules.artist_page as ap


def test_album_count_survives_meta_resolving_last(qapp, monkeypatch):
    monkeypatch.setattr(ap, "load_image_async", lambda *a, **k: None)
    page = ap.ArtistPage()
    page._artist_id = "art1"
    monkeypatch.setattr(page.api, "get_image_url", lambda *a, **k: "")

    # Albums resolve FIRST → count set.
    page._on_albums_loaded("art1", [{"Name": "Only Album"}])
    assert "1 album" in page._info.text()

    # Meta resolves SECOND (the order that used to clobber the count).
    page._on_meta_loaded("art1", {"Name": "The Artist", "Genres": []})
    assert "1 album" in page._info.text()  # pre-fix: was ""


def test_album_count_survives_meta_resolving_first(qapp, monkeypatch):
    monkeypatch.setattr(ap, "load_image_async", lambda *a, **k: None)
    page = ap.ArtistPage()
    page._artist_id = "art2"
    monkeypatch.setattr(page.api, "get_image_url", lambda *a, **k: "")

    page._on_meta_loaded("art2", {"Name": "A", "Genres": ["Pop"]})
    page._on_albums_loaded("art2", [{"Name": "x"}, {"Name": "y"}])
    info = page._info.text()
    assert "Pop" in info and "2 albums" in info


def test_no_count_until_albums_resolve(qapp, monkeypatch):
    # Meta-only (albums not yet resolved) shows the genre but no spurious
    # "0 albums" — the count appears only once the albums fetch completes.
    monkeypatch.setattr(ap, "load_image_async", lambda *a, **k: None)
    page = ap.ArtistPage()
    page._artist_id = "art3"
    monkeypatch.setattr(page.api, "get_image_url", lambda *a, **k: "")
    page._on_meta_loaded("art3", {"Name": "A", "Genres": ["Rock"]})
    assert page._info.text() == "Rock"
