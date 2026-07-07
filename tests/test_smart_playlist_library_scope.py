"""Smart-playlist evaluation must honor the library selection (#226).

Found live during the 0.1.8 MAS screenshot reshoot: with one library
selected in the picker, the smart-playlist preview returned tracks from
every library on the server. Both providers now resolve the same
``library_selection.fetch_plan`` the browse surfaces use and scope each
server leg to it — ``musicFolderId`` per Subsonic call, ``ParentId``
per Jellyfin ``/Items`` call — merging multi-folder plans client-side
(``refine_items`` re-sorts and re-limits the union).

These tests pin the scoping by stubbing ``_smart_folder_plan`` and
asserting the params each server would have seen.
"""

from __future__ import annotations

import pytest

from jellytoast.providers.jellyfin import JellyfinProvider
from jellytoast.providers.subsonic import SubsonicProvider


@pytest.fixture
def sub(monkeypatch):
    p = SubsonicProvider()
    p._username = "test"
    p._password = "test"
    p._server_url = "https://example.invalid"
    p.calls = []
    p.responses = {}

    def _fake_request(path, params=None, server_url=None):
        p.calls.append((path, dict(params or {})))
        return p.responses.get(path, {})

    monkeypatch.setattr(p, "_request", _fake_request)
    return p


@pytest.fixture
def jf(monkeypatch):
    p = JellyfinProvider()
    p.api.user_id = "u1"
    p.calls = []
    p.next_response = {"Items": []}

    def _fake_get(path, params=None):
        p.calls.append((path, dict(params or {})))
        return p.next_response

    monkeypatch.setattr(p.api, "_get", _fake_get)
    return p


def _sub_song(id_, title="Song"):
    return {"id": id_, "title": title, "year": 2020, "artist": "A",
            "album": "Al", "duration": 180, "suffix": "flac"}


RULES_GENRE = {"match": "all", "rules": [{"field": "genre", "op": "equals", "value": "Pop"}]}
RULES_UNMAPPABLE = {"match": "all", "rules": [{"field": "artist", "op": "contains", "value": "x"}]}


# ── Subsonic ────────────────────────────────────────────────────────────


def test_subsonic_native_leg_scopes_musicfolderid(sub, monkeypatch):
    monkeypatch.setattr(sub, "_smart_folder_plan", lambda: ["lib4"])
    sub.responses["getSongsByGenre"] = {"songsByGenre": {"song": [_sub_song("s1")]}}
    out = sub.query_items(RULES_GENRE)
    calls = [c for c in sub.calls if c[0] == "getSongsByGenre"]
    assert calls and calls[0][1]["musicFolderId"] == "lib4"
    assert [i["Id"] for i in out] == ["s1"]


def test_subsonic_unscoped_plan_omits_musicfolderid(sub, monkeypatch):
    monkeypatch.setattr(sub, "_smart_folder_plan", lambda: [""])
    sub.responses["getSongsByGenre"] = {"songsByGenre": {"song": []}}
    sub.query_items(RULES_GENRE)
    calls = [c for c in sub.calls if c[0] == "getSongsByGenre"]
    assert calls and "musicFolderId" not in calls[0][1]


def test_subsonic_multi_folder_plan_queries_each_and_dedupes(sub, monkeypatch):
    monkeypatch.setattr(sub, "_smart_folder_plan", lambda: ["lib1", "lib2"])
    sub.responses["getSongsByGenre"] = {
        "songsByGenre": {"song": [_sub_song("dup"), _sub_song("only")]}
    }
    out = sub.query_items(RULES_GENRE)
    folder_ids = [c[1].get("musicFolderId") for c in sub.calls if c[0] == "getSongsByGenre"]
    assert folder_ids == ["lib1", "lib2"]
    # Same payload returned for both folders — union must dedupe by Id.
    assert sorted(i["Id"] for i in out) == ["dup", "only"]


def test_subsonic_broad_fetch_scopes_musicfolderid(sub, monkeypatch):
    monkeypatch.setattr(sub, "_smart_folder_plan", lambda: ["lib4"])
    sub.responses["search3"] = {"searchResult3": {"song": [_sub_song("s1", "xylo")]}}
    sub.query_items(RULES_UNMAPPABLE)
    calls = [c for c in sub.calls if c[0] == "search3"]
    assert calls and all(c[1]["musicFolderId"] == "lib4" for c in calls)


def test_subsonic_legacy_album_fallback_scopes_musicfolderid(sub, monkeypatch):
    monkeypatch.setattr(sub, "_smart_folder_plan", lambda: ["lib4"])
    # Empty search3 first page → legacy getAlbumList2 walk.
    sub.responses["search3"] = {"searchResult3": {"song": []}}
    sub.responses["getAlbumList2"] = {"albumList2": {"album": []}}
    sub.query_items(RULES_UNMAPPABLE)
    calls = [c for c in sub.calls if c[0] == "getAlbumList2"]
    assert calls and calls[0][1]["musicFolderId"] == "lib4"


def test_subsonic_starred_leg_scopes_musicfolderid(sub, monkeypatch):
    monkeypatch.setattr(sub, "_smart_folder_plan", lambda: ["lib4"])
    sub.responses["getStarred2"] = {"starred2": {"song": []}}
    sub.query_items({"match": "all", "rules": [
        {"field": "is_favorite", "op": "equals", "value": True}]})
    calls = [c for c in sub.calls if c[0] == "getStarred2"]
    assert calls and calls[0][1]["musicFolderId"] == "lib4"


# ── Jellyfin ────────────────────────────────────────────────────────────


def test_jellyfin_scopes_parentid(jf, monkeypatch):
    monkeypatch.setattr(jf, "_smart_folder_plan", lambda: ["view7"])
    jf.query_items(RULES_GENRE)
    assert jf.calls and jf.calls[0][1]["ParentId"] == "view7"


def test_jellyfin_unscoped_plan_omits_parentid(jf, monkeypatch):
    monkeypatch.setattr(jf, "_smart_folder_plan", lambda: [""])
    jf.query_items(RULES_GENRE)
    assert jf.calls and "ParentId" not in jf.calls[0][1]


def test_jellyfin_multi_view_plan_queries_each_and_dedupes(jf, monkeypatch):
    monkeypatch.setattr(jf, "_smart_folder_plan", lambda: ["v1", "v2"])
    jf.next_response = {"Items": [{"Id": "dup", "Name": "T", "Type": "Audio",
                                   "Genres": ["Pop"], "UserData": {}}]}
    out = jf.query_items(RULES_GENRE)
    parents = [c[1].get("ParentId") for c in jf.calls]
    assert parents == ["v1", "v2"]
    assert [i["Id"] for i in out] == ["dup"]  # deduped union


# ── The plan resolver itself ────────────────────────────────────────────


def test_smart_folder_plan_uses_library_selection_fetch_plan(sub, monkeypatch):
    import jellytoast.library_selection as ls

    monkeypatch.setattr(ls, "fetch_plan", lambda provider: ["a", "b"])
    assert sub._smart_folder_plan() == ["a", "b"]


def test_smart_folder_plan_degrades_to_unscoped_on_error(sub, monkeypatch):
    import jellytoast.library_selection as ls

    def _boom(provider):
        raise RuntimeError("selection layer exploded")

    monkeypatch.setattr(ls, "fetch_plan", _boom)
    assert sub._smart_folder_plan() == [""]
