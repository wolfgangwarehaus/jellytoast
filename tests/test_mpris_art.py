"""MPRIS cover art is a LOCAL file, never the server URL.

The server image URL carries the API key / Subsonic token; publishing it as
``mpris:artUrl`` put credentials on the session bus for any process to read,
and the lockscreen / media widget had to fetch it (no art offline). Now the
metadata carries a ``file://`` copy saved under the cache folder, or no art.
"""

from __future__ import annotations

import os
import time

import pytest

pytest.importorskip("dbus_next")

from jellytoast.media_controls import _mpris  # noqa: E402
from jellytoast.player_state import NowPlaying  # noqa: E402


def _np(**kw):
    base = dict(
        item_id="t1",
        image_id="al1",
        art_tag="v2",
        title="Song",
        subtitle="Artist",
        album="Album",
        thumb_url="https://music.example/rest/getCoverArt?id=al1&u=me&t=SECRET&s=salt",
    )
    base.update(kw)
    return NowPlaying(**base)


@pytest.fixture
def player(monkeypatch):
    p = _mpris.MprisPlayer.__new__(_mpris.MprisPlayer)
    p._metadata = {}
    sent = []
    monkeypatch.setattr(p, "emit_properties_changed", lambda d: sent.append(d), raising=False)
    return p, sent


class TestMetadata:
    def test_never_publishes_the_server_url(self, player):
        p, sent = player
        p.update_metadata(_np())
        md = sent[-1]["Metadata"]
        assert "mpris:artUrl" not in md
        assert "SECRET" not in repr(md)

    def test_publishes_a_local_file(self, player):
        p, sent = player
        p.update_metadata(_np(), "file:///home/u/.cache/jellytoast/mpris-art/abc.png")
        assert sent[-1]["Metadata"]["mpris:artUrl"].value.startswith("file:///")


class TestArtCache:
    def test_path_follows_the_art_identity(self, qapp, monkeypatch, tmp_path):
        monkeypatch.setattr(_mpris, "_art_dir", lambda: tmp_path)
        a = _mpris._cached_art_path(_np())
        same_album_other_track = _mpris._cached_art_path(_np(item_id="t2"))
        new_art_version = _mpris._cached_art_path(_np(art_tag="v3"))
        assert a.parent == tmp_path and a.suffix == ".png"
        assert a == same_album_other_track  # one copy per cover, not per track
        assert a != new_art_version  # art changed on the server → new copy

    def test_no_identity_no_path(self, qapp, monkeypatch, tmp_path):
        monkeypatch.setattr(_mpris, "_art_dir", lambda: tmp_path)
        assert _mpris._cached_art_path(_np(item_id="", image_id="", art_tag="")) is None

    def test_prune_keeps_the_newest(self, tmp_path):
        now = time.time()
        for i in range(_mpris._ART_KEEP + 5):
            f = tmp_path / f"{i:03d}.png"
            f.write_bytes(b"x")
            os.utime(f, (now + i, now + i))
        _mpris._prune_art_dir(tmp_path)
        left = sorted(f.name for f in tmp_path.glob("*.png"))
        assert len(left) == _mpris._ART_KEEP
        assert left[0] == f"{5:03d}.png"  # the 5 oldest went


class TestRestoredSession:
    def test_restored_track_reads_paused_where_it_left_off(self, qapp, player, monkeypatch):
        # Last session's track comes back paused: the lock screen should
        # offer it (Paused at its position), not read Stopped at 0:00.
        p, sent = player
        p._position = 0
        svc = _mpris.MprisService.__new__(_mpris.MprisService)
        svc._player = p
        svc._art_item = ""
        monkeypatch.setattr(svc, "_schedule", lambda fn: fn(), raising=False)
        monkeypatch.setattr(svc, "_publish_art", lambda np: None, raising=False)
        monkeypatch.setattr(_mpris, "_cached_art_path", lambda np: None)
        np = _np()
        np.position = 211_000
        svc._on_restored(np)
        assert sent[-1] == {"PlaybackStatus": "Paused"}
        assert not any(d.get("PlaybackStatus") == "Playing" for d in sent)
        assert sent[0]["Metadata"]["xesam:title"].value == "Song"
        assert p._position == 211_000_000
