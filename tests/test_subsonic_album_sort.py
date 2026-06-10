"""Regression for the Subsonic Favorites/Played filter being silently
dropped under any non-default sort (audit 2026-06-01, §1.7).

``SubsonicProvider._get_albums`` maps the Jellyfin sort key to a
getAlbumList2 ``type``. The content filters (IsFavorite → ``starred``,
IsPlayed → ``frequent``) used to sit at the *end* of the elif chain, so
any mapped sort key (AlbumArtist, PremiereDate, …) won the branch and the
filter was dropped — Favorites + sort-by-AlbumArtist returned the WHOLE
library, and the AlbumArtist client re-sort branch was unreachable. The
fix checks the filters FIRST (a filter is a content selector, not a sort)
and applies the sort client-side. We stub ``_request`` and assert the
``type`` parameter actually sent to the server.
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

import pytest

from jellytoast.providers.subsonic import SubsonicProvider


class _FakeRequest:
    """Records (endpoint, params) and returns a canned albumList2."""

    def __init__(self, albums: "List[Dict[str, Any]] | None" = None):
        self.calls: List[Tuple[str, Dict[str, Any]]] = []
        self._albums = albums or []

    def __call__(self, endpoint: str, params: Dict[str, Any]):
        self.calls.append((endpoint, dict(params)))
        return {"albumList2": {"album": self._albums}}

    @property
    def last_type(self) -> str:
        return self.calls[-1][1]["type"]


@pytest.fixture
def provider(monkeypatch):
    prov = SubsonicProvider()
    fake = _FakeRequest()
    monkeypatch.setattr(prov, "_request", fake)
    return prov, fake


def _albums(prov, **kw):
    defaults = dict(
        parent_id="",
        limit=100,
        start_index=0,
        sort_by="SortName",
        sort_order="Ascending",
        genre_id="",
        filters="",
    )
    defaults.update(kw)
    return prov._get_albums(**defaults)


class TestFilterWinsOverSort:
    def test_favorite_under_albumartist_sort_uses_starred(self, provider):
        prov, fake = provider
        _albums(prov, sort_by="AlbumArtist", filters="IsFavorite")
        # The bug: AlbumArtist won → "alphabeticalByArtist" (whole library).
        assert fake.last_type == "starred"

    def test_favorite_under_year_sort_uses_starred(self, provider):
        prov, fake = provider
        _albums(prov, sort_by="PremiereDate", filters="IsFavorite")
        assert fake.last_type == "starred"

    def test_favorite_default_sort_still_starred(self, provider):
        prov, fake = provider
        _albums(prov, sort_by="SortName", filters="IsFavorite")
        assert fake.last_type == "starred"

    def test_played_filter_under_sort_uses_frequent(self, provider):
        prov, fake = provider
        _albums(prov, sort_by="AlbumArtist", filters="IsPlayed")
        assert fake.last_type == "frequent"

    def test_no_filter_honors_sort_unchanged(self, provider):
        # Without a content filter, the sort key still maps as before.
        prov, fake = provider
        _albums(prov, sort_by="AlbumArtist", filters="")
        assert fake.last_type == "alphabeticalByArtist"
        _albums(prov, sort_by="DateCreated", filters="")
        assert fake.last_type == "newest"
        _albums(prov, sort_by="SortName", filters="")
        assert fake.last_type == "alphabeticalByName"


class TestStarredClientResort:
    """The AlbumArtist client re-sort branch (was dead) now fires when
    Favorites is combined with sort-by-AlbumArtist."""

    def test_albumartist_resort_applies_under_starred(self, monkeypatch):
        prov = SubsonicProvider()
        # Server returns star-date order; client must re-sort by artist.
        raw = [
            {"id": "1", "name": "Zebra", "artist": "The Beatles"},
            {"id": "2", "name": "Apple", "artist": "ABBA"},
        ]
        fake = _FakeRequest(raw)
        monkeypatch.setattr(prov, "_request", fake)
        # Keep adaptation trivial + predictable: AlbumArtist == artist.
        monkeypatch.setattr(
            prov,
            "_adapt_album",
            lambda a: {"Id": a["id"], "Name": a["name"], "AlbumArtist": a["artist"]},
        )
        out = _albums(
            prov, sort_by="AlbumArtist", sort_order="Ascending", filters="IsFavorite"
        )
        names = [it["AlbumArtist"] for it in out["Items"]]
        # ABBA before The Beatles (article-stripped → "Beatles").
        assert names == ["ABBA", "The Beatles"]
