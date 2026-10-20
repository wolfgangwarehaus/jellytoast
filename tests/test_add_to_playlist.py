"""Add to playlist (#238): provider calls (Subsonic + Jellyfin), the shared
submenu, and the new-playlist prompt."""

from urllib.parse import parse_qs

import pytest

from jellytoast.providers import subsonic as sub_mod
from jellytoast.providers.subsonic import SubsonicProvider


def _subsonic():
    p = SubsonicProvider()
    p._username = "me"
    p._password = "secret"
    p._server_url = "http://example.local"
    return p


class _Recorder:
    def __init__(self, responses=None):
        self.calls = []
        self.responses = responses or {}

    def __call__(self, path, params=None, server_url=None):
        self.calls.append((path, dict(params or {})))
        return self.responses.get(path, {})


# ── Subsonic ─────────────────────────────────────────────────────────────


def test_query_repeats_list_params():
    q = parse_qs(sub_mod._build_query({"playlistId": "7", "songIdToAdd": ["a", "b"]}))
    assert q["songIdToAdd"] == ["a", "b"] and q["playlistId"] == ["7"]


def test_query_keeps_plain_strings_whole():
    assert parse_qs(sub_mod._build_query({"query": "abc"}))["query"] == ["abc"]


def test_subsonic_add_batches(monkeypatch):
    p = _subsonic()
    rec = _Recorder()
    monkeypatch.setattr(p, "_request", rec)
    ids = [f"s{i}" for i in range(120)]
    p.add_to_playlist("pl1", ids)
    assert [c[0] for c in rec.calls] == ["updatePlaylist"] * 3
    sent = [i for _path, params in rec.calls for i in params["songIdToAdd"]]
    assert sent == ids
    assert all(params["playlistId"] == "pl1" for _p, params in rec.calls)


def test_subsonic_create_returns_id_and_adds_the_rest(monkeypatch):
    p = _subsonic()
    rec = _Recorder({"createPlaylist": {"playlist": {"id": "42"}}})
    monkeypatch.setattr(p, "_request", rec)
    ids = [f"s{i}" for i in range(60)]
    assert p.create_playlist("Road trip", ids) == "42"
    assert rec.calls[0] == ("createPlaylist", {"name": "Road trip", "songId": ids[:50]})
    assert rec.calls[1] == ("updatePlaylist", {"playlistId": "42", "songIdToAdd": ids[50:]})


def test_subsonic_create_finds_id_when_server_returns_none(monkeypatch):
    p = _subsonic()
    rec = _Recorder(
        {
            "createPlaylist": {},
            "getPlaylists": {
                "playlists": {
                    "playlist": [
                        {"id": "1", "name": "Road trip", "owner": "me", "created": "2026-01-01"},
                        {"id": "9", "name": "Road trip", "owner": "me", "created": "2026-10-20"},
                    ]
                }
            },
        }
    )
    monkeypatch.setattr(p, "_request", rec)
    assert p.create_playlist("Road trip", ["a"]) == "9"


def test_subsonic_lists_only_owned_playlists(monkeypatch):
    p = _subsonic()
    rec = _Recorder(
        {
            "getPlaylists": {
                "playlists": {
                    "playlist": [
                        {"id": "2", "name": "b mine", "owner": "me"},
                        {"id": "3", "name": "theirs", "owner": "someone"},
                        {"id": "1", "name": "A mine", "owner": "ME"},
                    ]
                }
            }
        }
    )
    monkeypatch.setattr(p, "_request", rec)
    assert [x["Id"] for x in p.get_playlists()] == ["1", "2"]


# ── Jellyfin ─────────────────────────────────────────────────────────────


def test_jellyfin_add_and_create(monkeypatch):
    from jellytoast.jellyfin_api import JellyfinAPI

    api = JellyfinAPI.__new__(JellyfinAPI)
    api.user_id = "u1"
    posts = []

    def _post(path, payload=None, *, strict=False):
        posts.append((path, payload, strict))
        return {"Id": "new"} if path == "/Playlists" else None

    monkeypatch.setattr(api, "_post", _post)
    api.add_to_playlist("p9", [f"t{i}" for i in range(55)])
    assert len(posts) == 2 and all(p[2] for p in posts)
    assert posts[0][0].startswith("/Playlists/p9/Items?ids=t0,t1,") and "userId=u1" in posts[0][0]
    posts.clear()
    assert api.create_playlist("Mix", ["a", "b"]) == "new"
    assert posts[0][0] == "/Playlists"
    assert posts[0][1] == {"Name": "Mix", "Ids": ["a", "b"], "UserId": "u1", "MediaType": "Audio"}


# ── The shared submenu ───────────────────────────────────────────────────


class _FakeProvider:
    supports_playlist_editing = True
    server_url = "http://srv"

    def __init__(self):
        self.added = []
        self.created = []

    def is_authenticated(self):
        return True

    def get_playlists(self):
        return [{"Id": "p1", "Name": "Chill"}, {"Id": "p2", "Name": "Gym"}]

    def add_to_playlist(self, pid, ids):
        self.added.append((pid, list(ids)))

    def create_playlist(self, name, ids):
        self.created.append((name, list(ids)))
        return "p3"


@pytest.fixture
def actions(qapp, monkeypatch):
    from jellytoast import async_io, offline, playlist_actions

    prov = _FakeProvider()
    monkeypatch.setattr(playlist_actions, "_provider", lambda: prov)
    monkeypatch.setattr(offline, "is_offline_mode", lambda: False)

    def _inline(fn, *a, on_result=None, on_error=None, **k):
        try:
            r = fn(*a, **k)
        except Exception as e:  # pragma: no cover — surfaced via on_error
            if on_error:
                on_error(e)
            return
        if on_result:
            on_result(r)

    monkeypatch.setattr(async_io, "run_async", _inline)
    toasts = []
    monkeypatch.setattr(playlist_actions, "_toast", lambda _h, m: toasts.append(m))
    playlist_actions.invalidate_cache()
    yield playlist_actions, prov, toasts
    playlist_actions.invalidate_cache()


def test_submenu_lists_playlists_and_adds(actions):
    from PySide6.QtWidgets import QMenu, QWidget

    from jellytoast.player_state import PlayerBus

    pa, prov, toasts = actions
    host = QWidget()
    menu = QMenu()
    sub = pa.add_playlist_submenu(menu, lambda: ["t1", "t2"], host=host)
    sub.aboutToShow.emit()
    labels = [a.text() for a in sub.actions() if not a.isSeparator()]
    assert labels == ["New playlist…", "Chill", "Gym"]
    changed = []
    PlayerBus.get().playlists_changed.connect(lambda: changed.append(1))
    next(a for a in sub.actions() if a.text() == "Gym").trigger()
    assert prov.added == [("p2", ["t1", "t2"])]
    assert toasts == ["Added 2 songs to Gym"] and changed
    host.deleteLater()


def test_new_playlist_creates_with_the_songs(actions, monkeypatch):
    from PySide6.QtWidgets import QWidget

    from jellytoast import frosted_dialog

    pa, prov, toasts = actions
    monkeypatch.setattr(frosted_dialog, "frosted_text_input", lambda *a, **k: "Road trip")
    pa._new_playlist(QWidget(), lambda: ["t9"])
    assert prov.created == [("Road trip", ["t9"])]
    assert toasts == ["Added to Road trip"]


def test_hidden_when_offline_or_signed_out(actions, monkeypatch):
    from PySide6.QtWidgets import QMenu, QWidget

    from jellytoast import offline

    pa, prov, _ = actions
    monkeypatch.setattr(offline, "is_offline_mode", lambda: True)
    assert pa.add_playlist_submenu(QMenu(), lambda: [], host=QWidget()) is None
    monkeypatch.setattr(offline, "is_offline_mode", lambda: False)
    monkeypatch.setattr(prov, "is_authenticated", lambda: False)
    assert pa.add_playlist_submenu(QMenu(), lambda: [], host=QWidget()) is None


def test_text_input_needs_text(qapp):
    from jellytoast.frosted_dialog import FrostedInputDialog

    dlg = FrostedInputDialog(None, title="New playlist")
    assert not dlg._confirm.isEnabled()
    dlg.edit.setText("  Mix  ")
    assert dlg._confirm.isEnabled() and dlg.value() == "Mix"
    dlg.deleteLater()
