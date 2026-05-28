"""DLNA + Sonos cast PLAY-dispatch wiring.

The 2026-05-28 audit found the DLNA / Sonos / Snapcast backends were
built + unit-tested but their *play* path was never wired into the cast
dispatch — a pick silently misrouted into the AirPlay ``POST /play``.
This covers the DLNA + Sonos half of the fix (the URL-push backends):

- ``cast_payload.dlna_meta_from_np`` — NowPlaying → ``TrackMetadata``
  field mapping, container→MIME derivation, empty/unknown handling.
- ``cast_payload.make_transcode_fn`` — provider 714-fallback URL builder.
- ``CastManager.cast_to_dlna`` — resolves the cast-proxy URL, calls
  ``DlnaController.play`` with the right args, arms ``active_cast`` only
  on success, degrades to ``False`` (no ``active_cast``) when the backend
  is unavailable or the push fails.
- ``CastManager.cast_to_sonos`` — calls the backend ``cast_to_sonos`` with
  the DIDL fields, arms ``active_cast`` on success, graceful when absent.

No network: the dlna / sonos backends + cast_proxy are stubbed.
"""

from __future__ import annotations

import pytest

from modules.cast_manager import CastManager
from modules.cast_manager._common import CastDevice
from modules.player_state import NowPlaying


def _np(**kw):
    base = dict(
        item_id="abc",
        title="Song",
        subtitle="Artist",
        album="Album",
        duration=215000,  # ms
        thumb_url="http://s/art.jpg",
        stream_url="http://s/stream",
        item_type="Audio",
    )
    base.update(kw)
    return NowPlaying(**base)


# ── cast_payload helpers ──────────────────────────────────────────


def test_dlna_meta_from_np_maps_fields():
    from modules.cast_payload import dlna_meta_from_np

    meta = dlna_meta_from_np(_np(raw={"Container": "flac", "AlbumArtist": "AA", "IndexNumber": 3}))
    assert meta.item_id == "abc"
    assert meta.title == "Song"
    assert meta.artist == "Artist"
    assert meta.album == "Album"
    assert meta.album_artist == "AA"
    assert meta.track_number == 3
    assert meta.duration_sec == pytest.approx(215.0)
    assert meta.mime == "audio/flac"  # derived from container via _MIME_BY_CONTAINER
    assert meta.cover_url == "http://s/art.jpg"


def test_dlna_meta_unknown_container_leaves_mime_blank():
    from modules.cast_payload import dlna_meta_from_np

    meta = dlna_meta_from_np(_np(raw={"Container": "weirdcodec"}))
    # Blank mime → controller's decide_push_format pushes native + the
    # 714-retry handles a refusal. album_artist falls back to the artist.
    assert meta.mime == ""
    assert meta.album_artist == "Artist"


def test_dlna_meta_empty_raw():
    from modules.cast_payload import dlna_meta_from_np

    meta = dlna_meta_from_np(_np(raw={}))
    assert meta.mime == ""
    assert meta.track_number == 0


def test_make_transcode_fn_builds_provider_url():
    from modules.cast_payload import make_transcode_fn

    class _Prov:
        def get_audio_transcode_url(self, item_id, max_bitrate_kbps, codec):
            return f"http://s/{item_id}?br={max_bitrate_kbps}&c={codec}"

    fn = make_transcode_fn(_Prov(), "xyz")
    assert fn("orig-url-ignored", 320) == "http://s/xyz?br=320&c=mp3"


# ── CastManager.cast_to_dlna ──────────────────────────────────────


@pytest.fixture
def dlna_dev():
    return CastDevice(
        name="TV", host="10.0.0.5", port=8200, device_type="dlna",
        uuid="udn-1", cast_object=object(),
    )


def _patch_dlna(monkeypatch, *, available=True, play_ok=True, record=None):
    import modules.cast.dlna as _dlna
    import modules.cast_proxy as _proxy

    class _Ctrl:
        def play(self, dev, url, meta, *, transcode_url_fn=None, force_transcode=False):
            if record is not None:
                record.update(
                    dev=dev, url=url, meta=meta,
                    tfn=transcode_url_fn, force=force_transcode,
                )
            return play_ok

    monkeypatch.setattr(_dlna, "is_available", lambda: available)
    monkeypatch.setattr(_dlna, "get_dlna_controller", lambda: _Ctrl())
    monkeypatch.setattr(_proxy, "resolve_cast_url", lambda u: "PROXY::" + u)


def test_cast_to_dlna_happy_path(monkeypatch, dlna_dev):
    from modules.cast.dlna import TrackMetadata

    rec = {}
    _patch_dlna(monkeypatch, record=rec)
    m = CastManager()
    meta = TrackMetadata(item_id="abc", title="Song")

    def tfn(_u, _b):
        return "transcoded"

    ok = m.cast_to_dlna(dlna_dev, "http://s/stream", meta, transcode_url_fn=tfn)

    assert ok is True
    assert m.active_cast is dlna_dev
    assert rec["dev"] is dlna_dev.cast_object  # the DlnaDevice, not the CastDevice
    assert rec["url"] == "PROXY::http://s/stream"  # routed through the cast proxy
    assert rec["meta"] is meta
    assert rec["tfn"] is tfn


def test_cast_to_dlna_unavailable_no_active_cast(monkeypatch, dlna_dev):
    from modules.cast.dlna import TrackMetadata

    _patch_dlna(monkeypatch, available=False)
    m = CastManager()
    ok = m.cast_to_dlna(dlna_dev, "http://s/stream", TrackMetadata(item_id="a", title="t"))
    assert ok is False
    assert m.active_cast is None


def test_cast_to_dlna_play_failure_no_active_cast(monkeypatch, dlna_dev):
    from modules.cast.dlna import TrackMetadata

    _patch_dlna(monkeypatch, play_ok=False)
    m = CastManager()
    ok = m.cast_to_dlna(dlna_dev, "http://s/stream", TrackMetadata(item_id="a", title="t"))
    assert ok is False
    assert m.active_cast is None


# ── CastManager.cast_to_sonos ─────────────────────────────────────


@pytest.fixture
def sonos_dev():
    return CastDevice(
        name="Living Room", host="10.0.0.9", port=0, device_type="sonos",
        uuid="rincon-1", cast_object=object(),
    )


def _patch_sonos(monkeypatch, *, available=True, ok=True, record=None):
    import modules.cast.sonos as _sonos

    def _cast(zone, url, *, title="", artist="", album="", art_url=""):
        if record is not None:
            record.update(zone=zone, url=url, title=title, artist=artist, album=album, art_url=art_url)
        return ok

    monkeypatch.setattr(_sonos, "is_available", lambda: available)
    monkeypatch.setattr(_sonos, "cast_to_sonos", _cast)


def test_cast_to_sonos_happy_path(monkeypatch, sonos_dev):
    rec = {}
    _patch_sonos(monkeypatch, record=rec)
    m = CastManager()

    ok = m.cast_to_sonos(
        sonos_dev, "http://s/stream", title="T", artist="A", album="Al", art_url="http://s/art"
    )

    assert ok is True
    assert m.active_cast is sonos_dev
    assert rec["zone"] is sonos_dev.cast_object  # the SonosZone coordinator handle
    assert rec["url"] == "http://s/stream"
    assert rec["title"] == "T"
    assert rec["artist"] == "A"
    assert rec["album"] == "Al"
    assert rec["art_url"] == "http://s/art"


def test_cast_to_sonos_unavailable_no_active_cast(monkeypatch, sonos_dev):
    _patch_sonos(monkeypatch, available=False)
    m = CastManager()
    ok = m.cast_to_sonos(sonos_dev, "http://s/stream")
    assert ok is False
    assert m.active_cast is None


def test_cast_to_sonos_failure_no_active_cast(monkeypatch, sonos_dev):
    _patch_sonos(monkeypatch, ok=False)
    m = CastManager()
    ok = m.cast_to_sonos(sonos_dev, "http://s/stream")
    assert ok is False
    assert m.active_cast is None


# ── cast dialog row label (per-protocol, not chromecast-vs-AirPlay) ──
# Regression for the 2026-05-28 GUI-walk bug: the row labelled every
# non-Chromecast device "· AirPlay", so a DLNA renderer read
# "192.168.x.x · AirPlay".


@pytest.mark.parametrize(
    "dtype,expected",
    [
        ("chromecast", "Chromecast"),
        ("airplay", "AirPlay"),
        ("dlna", "DLNA"),
        ("sonos", "Sonos"),
        ("snapcast", "Snapcast"),
    ],
)
def test_cast_device_row_labels_per_protocol(qapp, dtype, expected):
    from PySide6.QtWidgets import QLabel
    from modules.now_playing_bar import _CastDeviceRow

    dev = CastDevice(name="Dev", host="h", port=1, device_type=dtype, uuid="u", cast_object=object())
    row = _CastDeviceRow(dev, False)
    try:
        texts = [w.text() for w in row.findChildren(QLabel) if w.text()]
        assert any(expected in t for t in texts), f"{dtype}: {texts}"
        if dtype != "airplay":
            assert not any("AirPlay" in t for t in texts), f"{dtype} mislabelled AirPlay: {texts}"
    finally:
        row.deleteLater()
