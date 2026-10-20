"""#239: a track with its own embedded art shows that, not the album's."""

from jellytoast import offline
from jellytoast.queue_manager import _own_track_art
from jellytoast.ui_helpers import art_stem


def _online(monkeypatch):
    monkeypatch.setattr(offline, "is_offline_mode", lambda: False)


def test_navidrome_embedded_art_is_used(monkeypatch):
    _online(monkeypatch)
    song = {"Id": "s1", "AlbumId": "a1", "AlbumPrimaryImageTag": "mf-s1_da9f1329fed320a1"}
    assert _own_track_art(song) == ("mf-s1_da9f1329fed320a1", "mf-s1_da9f1329fed320a1")


def test_navidrome_track_without_art_keeps_the_album(monkeypatch):
    _online(monkeypatch)
    song = {"Id": "s1", "AlbumId": "a1", "AlbumPrimaryImageTag": "al-a1_29d817d89685276d"}
    assert _own_track_art(song) is None


def test_jellyfin_track_image_differs_from_album(monkeypatch):
    _online(monkeypatch)
    track = {"Id": "t1", "AlbumPrimaryImageTag": "albhash", "ImageTags": {"Primary": "trackhash"}}
    assert _own_track_art(track) == ("t1", "trackhash")
    plain = {"Id": "t2", "AlbumPrimaryImageTag": "albhash", "ImageTags": {}}
    assert _own_track_art(plain) is None


def test_offline_keeps_album_art(monkeypatch):
    monkeypatch.setattr(offline, "is_offline_mode", lambda: True)
    assert _own_track_art({"AlbumPrimaryImageTag": "mf-s1_abc"}) is None


def test_same_embedded_picture_shares_one_cache_slot():
    # Two tracks of one album embedding the same image (same hash).
    assert art_stem("mf-s1_7aec65882144c5cc", "mf-s1_7aec65882144c5cc") == art_stem(
        "mf-s2_7aec65882144c5cc", "mf-s2_7aec65882144c5cc"
    )
    # Album keys are unchanged.
    assert art_stem("a1", "al-a1_7aec") == "a1@al-a1_7aec"
