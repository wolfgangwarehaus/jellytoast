"""Tests for the offline path of ``SearchView._local_search``.

The local search runs entirely off downloaded metadata when offline
mode is on. The fields it inspects matter: a query like "Air" must
surface the Moon Safari album (the band is in ``AlbumArtists``), the
Air artist tile (synthesized when no real artist node is downloaded),
and any track whose ``Album`` or ``AlbumArtist`` names the band — not
just tracks literally named "Air".

These tests stub ``modules.offline.list_complete_items`` with a tiny
fixture covering every shape the real search needs to handle, and call
``_local_search`` directly as an unbound method. We never construct a
SearchView (which would pull in QApplication, providers, and the live
DB) — the method only reads module-level limits + the offline API, so
a bare ``object()`` as ``self`` is enough.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from modules import search_view as _sv


# Three downloaded albums + one bare artist + a handful of tracks.
# Shapes mirror Jellyfin's real payloads (AlbumArtists as a list of
# dicts with Id/Name, Artists as a list of strings).
AIR_ALBUM = {
    "Id": "alb-moon",
    "Name": "Moon Safari",
    "AlbumArtist": "Air",
    "AlbumArtists": [{"Id": "art-air", "Name": "Air"}],
    "Type": "MusicAlbum",
}
MOON_OTHER = {  # named "Moon"-something but a different artist
    "Id": "alb-moon2",
    "Name": "Moonchild",
    "AlbumArtist": "Rory Gallagher",
    "AlbumArtists": [{"Id": "art-rory", "Name": "Rory Gallagher"}],
    "Type": "MusicAlbum",
}
OTHER_ALBUM = {
    "Id": "alb-feist",
    "Name": "The Reminder",
    "AlbumArtist": "Feist",
    "AlbumArtists": [{"Id": "art-feist", "Name": "Feist"}],
    "Type": "MusicAlbum",
}

# A real downloaded artist node (no albums, but the user pinned them).
SEXY_SUSHI_ARTIST = {
    "Id": "art-sexy",
    "Name": "Sexy Sushi",
    "Type": "MusicArtist",
}

# A track whose own Name doesn't contain the band name — only the
# Album / AlbumArtist fields do. The "air" query must still find it
# via those fields.
TRACK_FEMME = {
    "Id": "trk-femme",
    "Name": "La femme d'argent",
    "Album": "Moon Safari",
    "AlbumArtist": "Air",
    "Artists": ["Air"],
    "Type": "Audio",
}
TRACK_SEXY = {  # exactly one track whose Name itself contains "sexy"
    "Id": "trk-sexy",
    "Name": "Sexy",
    "Album": "Whatever",
    "AlbumArtist": "Other",
    "Artists": ["Other"],
    "Type": "Audio",
}


def _make_fixture():
    """Build a ``list_complete_items``-style stub bound to the
    fixture above. Returns a fn that takes ``kind`` and yields bare
    metadata dicts — the same shape ``modules.offline.list_complete_items``
    actually emits."""
    by_kind = {
        "track": [TRACK_FEMME, TRACK_SEXY],
        "album": [AIR_ALBUM, MOON_OTHER, OTHER_ALBUM],
        "artist": [SEXY_SUSHI_ARTIST],
    }

    def _stub(kind=None):
        return list(by_kind.get(kind, []))

    return _stub


@pytest.fixture
def stubbed_offline(monkeypatch):
    """Patch ``list_complete_items`` on the offline module that
    ``search_view._local_search`` imports lazily. Returns the same
    fake stub callers can inspect if they want to swap it mid-test."""
    from modules import offline as _offline

    stub = _make_fixture()
    monkeypatch.setattr(_offline, "list_complete_items", stub)
    return stub


def _run(query: str) -> dict:
    """Call ``_local_search`` as an unbound method with a bare object
    as ``self``. The method doesn't touch instance state, so this
    avoids needing a real SearchView (and therefore QApplication)."""
    return _sv.SearchView._local_search(SimpleNamespace(), query)


# ── Songs (Audio bucket) ────────────────────────────────────────────


class TestSongsBucket:
    def test_air_matches_via_album_and_album_artist(self, stubbed_offline):
        out = _run("air")
        names = [t["Name"] for t in out["Audio"]]
        # La femme d'argent has no "air" in its own Name — it matches
        # only because Album=Moon Safari (no) and AlbumArtist=Air (yes).
        assert "La femme d'argent" in names

    def test_sexy_matches_name_directly(self, stubbed_offline):
        out = _run("Sexy")
        names = [t["Name"] for t in out["Audio"]]
        assert "Sexy" in names

    def test_moon_matches_via_album_field(self, stubbed_offline):
        out = _run("moon")
        names = [t["Name"] for t in out["Audio"]]
        # La femme d'argent has Album="Moon Safari" — matches via Album.
        assert "La femme d'argent" in names


# ── Albums (MusicAlbum bucket) ──────────────────────────────────────


class TestAlbumsBucket:
    def test_air_matches_via_album_artists_list(self, stubbed_offline):
        out = _run("air")
        names = [a["Name"] for a in out["MusicAlbum"]]
        # Moon Safari's Name doesn't contain "air", but its
        # AlbumArtists list has {"Name": "Air"}.
        assert "Moon Safari" in names

    def test_moon_matches_album_name(self, stubbed_offline):
        out = _run("moon")
        names = [a["Name"] for a in out["MusicAlbum"]]
        assert "Moon Safari" in names
        assert "Moonchild" in names
        assert "The Reminder" not in names

    def test_sexy_returns_no_albums(self, stubbed_offline):
        out = _run("Sexy")
        # No album has "sexy" in any of its match fields.
        assert out["MusicAlbum"] == []


# ── Artists (MusicArtist bucket — synthesis + dedupe) ───────────────


class TestArtistsBucket:
    def test_air_synthesizes_artist_tile_from_album_artists(
        self,
        stubbed_offline,
    ):
        """The "Air" artist isn't a real artist node, but Moon Safari's
        AlbumArtists references {Id: art-air, Name: Air}. _local_search
        must surface a synthesized {Id, Name, Type: MusicArtist} entry
        so the user gets an artist tile to click."""
        out = _run("air")
        ids = [a["Id"] for a in out["MusicArtist"]]
        names = [a["Name"] for a in out["MusicArtist"]]
        types = [a["Type"] for a in out["MusicArtist"]]
        assert "art-air" in ids
        assert "Air" in names
        # Every synthesized entry must declare its kind so consumers
        # don't have to guess from shape.
        assert all(t == "MusicArtist" for t in types)

    def test_sexy_matches_real_artist_node(self, stubbed_offline):
        out = _run("Sexy")
        names = [a["Name"] for a in out["MusicArtist"]]
        # Sexy Sushi is downloaded as an artist node directly.
        assert "Sexy Sushi" in names

    def test_synthesis_dedupes_against_real_artist_node(self, monkeypatch):
        """A real artist node and an AlbumArtists entry sharing one Id
        collapse to a single tile — the real node's metadata wins so
        any extra fields it carries aren't replaced by the stub."""
        from modules import offline as _offline

        # Real Air artist node (carries extra ImageTags the stub lacks).
        real_air = {
            "Id": "art-air",
            "Name": "Air",
            "Type": "MusicArtist",
            "ImageTags": {"Primary": "deadbeef"},
        }

        def _stub(kind=None):
            if kind == "artist":
                return [real_air]
            if kind == "album":
                return [AIR_ALBUM]
            return []

        monkeypatch.setattr(_offline, "list_complete_items", _stub)
        out = _run("air")
        air_entries = [a for a in out["MusicArtist"] if a["Id"] == "art-air"]
        assert len(air_entries) == 1
        # Real node won — its extra metadata survived.
        assert air_entries[0].get("ImageTags") == {"Primary": "deadbeef"}

    def test_synthesized_entry_name_must_match_query(self, stubbed_offline):
        """A query that names neither a real artist nor any
        AlbumArtists entry returns no artists."""
        out = _run("zzz-no-such-artist")
        assert out["MusicArtist"] == []


# ── Sanity / regressions ────────────────────────────────────────────


class TestSanity:
    def test_empty_query_returns_empty_buckets(self, stubbed_offline):
        out = _run("   ")
        assert out == {"Audio": [], "MusicAlbum": [], "MusicArtist": []}

    def test_case_insensitive(self, stubbed_offline):
        lower = _run("air")
        upper = _run("AIR")
        # Same items in each bucket regardless of casing.
        assert [t["Id"] for t in lower["Audio"]] == [t["Id"] for t in upper["Audio"]]
        assert [a["Id"] for a in lower["MusicAlbum"]] == [a["Id"] for a in upper["MusicAlbum"]]
        assert [a["Id"] for a in lower["MusicArtist"]] == [a["Id"] for a in upper["MusicArtist"]]

    def test_buckets_shape(self, stubbed_offline):
        out = _run("air")
        # Exactly the three Jellyfin Type keys consumers expect.
        assert set(out) == {"Audio", "MusicAlbum", "MusicArtist"}
