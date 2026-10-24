"""Deleting a playlist: who may (not someone else's / read-only), the
confirmation (a playlist imported from an .m3u on the server comes back at
the next scan — the file stays), and the desktop confirm → delete flow."""

from __future__ import annotations

import pytest

from jellytoast.providers.subsonic import SubsonicProvider, _playlist_source_file


def _subsonic(username="avtips"):
    p = SubsonicProvider()
    p._username, p._password, p._server_url = username, "secret", "http://example.local"
    return p


# ── Provider: Navidrome's getPlaylists → CanDelete / SourceFile ─────────────


def test_navidrome_playlists_carry_delete_and_source_file(monkeypatch):
    p = _subsonic()
    resp = {
        "playlists": {
            "playlist": [
                {"id": "1", "name": "Mine", "owner": "avtips", "readonly": False, "comment": ""},
                {
                    "id": "2",
                    "name": "Madonna - Ray Of Light",
                    "owner": "avtips",
                    "readonly": False,
                    "comment": "Auto-imported from 'Madonna - Ray Of Light.m3u'",
                },
                {
                    "id": "3",
                    "name": "Weekly-Exploration-2026-Week35",
                    "owner": "explo",
                    "readonly": True,
                    "comment": "Created for avtips by Explo, using ListenBrainz recommendations.",
                },
            ]
        }
    }
    monkeypatch.setattr(p, "_request", lambda *a, **k: resp)
    items = {i["Id"]: i for i in p._get_playlists()["Items"]}
    assert items["1"]["CanDelete"] is True and items["1"]["SourceFile"] == ""
    assert items["2"]["CanDelete"] is True
    assert items["2"]["SourceFile"] == "Madonna - Ray Of Light.m3u"
    assert items["3"]["CanDelete"] is False and items["3"]["SourceFile"] == ""


def test_someone_elses_public_playlist_is_not_deletable(monkeypatch):
    # A plain Subsonic server may not send "readonly"; ownership decides.
    p = _subsonic()
    resp = {"playlists": {"playlist": [{"id": "9", "name": "Theirs", "owner": "bob"}]}}
    monkeypatch.setattr(p, "_request", lambda *a, **k: resp)
    assert p._get_playlists()["Items"][0]["CanDelete"] is False


@pytest.mark.parametrize(
    "comment, expected",
    [
        ("Auto-imported from 'a.m3u'", "a.m3u"),
        ("Auto-imported from 'Deerhunter - Halcyon Digest [4AD Records].m3u'",
         "Deerhunter - Halcyon Digest [4AD Records].m3u"),
        ("Auto-imported from 'it's.m3u8'", "it's.m3u8"),
        ("my notes", ""),
        ("", ""),
        (None, ""),
    ],
)
def test_source_file_parsing(comment, expected):
    assert _playlist_source_file(comment) == expected


# ── Shared actions ──────────────────────────────────────────────────────


class _Prov:
    supports_playlist_editing = True
    is_authenticated = True
    server_url = "http://srv"

    def __init__(self, fail=False):
        self.deleted = []
        self.fail = fail

    def delete_playlist(self, pid):
        if self.fail:
            raise RuntimeError("server said no")
        self.deleted.append(pid)


@pytest.fixture
def pa(qapp, monkeypatch):
    from jellytoast import async_io, offline, playlist_actions

    prov = _Prov()
    monkeypatch.setattr(playlist_actions, "_provider", lambda: prov)
    monkeypatch.setattr(offline, "is_offline_mode", lambda: False)

    def _inline(fn, *a, on_result=None, on_error=None, **k):
        try:
            r = fn(*a, **k)
        except Exception as e:
            if on_error:
                on_error(e)
            return
        if on_result:
            on_result(r)

    monkeypatch.setattr(async_io, "run_async", _inline)
    toasts = []
    monkeypatch.setattr(playlist_actions, "_toast", lambda _h, m: toasts.append(m))
    return playlist_actions, prov, toasts


def test_can_delete(pa, monkeypatch):
    from jellytoast import offline

    actions, _prov, _ = pa
    assert actions.can_delete({"Id": "1"})  # Jellyfin items carry no flag
    assert actions.can_delete({"Id": "1", "CanDelete": True})
    assert not actions.can_delete({"Id": "1", "CanDelete": False})
    monkeypatch.setattr(offline, "is_offline_mode", lambda: True)
    assert not actions.can_delete({"Id": "1"})


def test_message_names_the_file_and_the_rescan(pa):
    actions, _prov, _ = pa
    plain = actions.delete_message("Gym")
    assert "Gym" in plain and "songs stay" in plain
    filed = actions.delete_message("Ray Of Light", "Madonna - Ray Of Light.m3u")
    assert "Madonna - Ray Of Light.m3u" in filed
    assert "doesn't remove that file" in filed and "next library scan" in filed


def test_confirm_then_delete(pa, monkeypatch):
    from jellytoast import frosted_dialog
    from jellytoast.player_state import PlayerBus

    actions, prov, toasts = pa
    asked = []
    monkeypatch.setattr(
        frosted_dialog, "frosted_confirm", lambda _p, title, text, **kw: asked.append(text) or True
    )
    changed = []
    PlayerBus.get().playlists_changed.connect(lambda: changed.append(1))
    actions.confirm_delete(None, {"Id": "p2", "Name": "Ray", "SourceFile": "Ray.m3u"})
    assert "Ray.m3u" in asked[0]
    assert prov.deleted == ["p2"]
    assert changed  # the Playlists views refresh
    assert toasts == []  # no host → no toast


def test_cancel_deletes_nothing(pa, monkeypatch):
    from jellytoast import frosted_dialog

    actions, prov, _ = pa
    monkeypatch.setattr(frosted_dialog, "frosted_confirm", lambda *a, **kw: False)
    actions.confirm_delete(None, {"Id": "p2", "Name": "Ray"})
    assert prov.deleted == []


def test_failed_delete_toasts(pa):
    actions, prov, toasts = pa
    prov.fail = True
    done = []
    actions.delete(object(), "p1", "Gym", on_done=done.append)
    assert done == [False]
    assert toasts == ["Couldn't delete Gym"]
