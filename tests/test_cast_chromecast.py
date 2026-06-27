"""Tests for ``jellytoast.cast_manager._chromecast`` — the Chromecast
media-load / transport flow.

Discovery + the per-protocol gates are already covered by
``test_cast_gating.py``; this file covers the *post-pick* surface that
had zero coverage: connecting a pre-armed session, loading media
(``cast_to_chromecast`` + its async wrapper), the transport controls
(pause/seek/set_volume/stop), and the pure container→MIME classmethod.

Strategy (mirrors ``test_cast_gating.py``):

- A ``_FakeCast`` stands in for a pychromecast Chromecast handle. It
  records ``wait`` / ``set_volume`` / ``quit_app`` calls and owns a
  ``_FakeMediaController`` that records ``play_media`` /
  ``block_until_active`` / ``pause`` / ``play`` / ``seek`` / ``stop``.
  Its media-controller ``status`` is a plain ``SimpleNamespace`` whose
  ``player_state`` the test sets to drive the poll loop's branches.
- ``jellytoast.cast_proxy.resolve_cast_url`` is patched to an identity (or
  recording) function so no proxy / settings machinery runs.
- The poll loop in ``cast_to_chromecast`` calls ``time.sleep`` /
  ``time.monotonic`` through the module-level ``time``; we patch
  ``_chromecast.time`` so the loop runs instantly and deterministically.
- The async wrappers route through ``jellytoast.cast_manager.run_async``
  (the monkeypatch indirection documented in the package ``__init__``);
  we stub it to run the worker inline so the assertion sees the side
  effect synchronously, no QApplication / event loop needed.

No network, no real pychromecast device.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import List, Optional

import pytest

import jellytoast.cast_manager as _cm_mod
import jellytoast.cast_manager._chromecast as _cc_mod
from jellytoast.cast_manager import CastManager
from jellytoast.cast_manager._common import CastDevice

# ── Fakes ──────────────────────────────────────────────────────────────────


class _FakeMediaController:
    def __init__(self) -> None:
        # Recording slots.
        self.play_media_calls: List[tuple] = []
        self.block_calls: List[dict] = []
        self.pause_calls = 0
        self.play_calls = 0
        self.seek_calls: List[float] = []
        self.stop_calls = 0
        # Driveable receiver status. Tests flip player_state /
        # idle_reason to exercise the poll loop's branches.
        self.status = SimpleNamespace(
            player_state="PLAYING",
            idle_reason=None,
            player_is_playing=True,
            content_id="",
            content_type="",
            stream_type="",
            duration=None,
            current_time=0.0,
            media_custom_data={},
            supported_media_commands=0,
            media_metadata={},
        )

    def play_media(self, url, content_type, **kwargs):
        self.play_media_calls.append((url, content_type, kwargs))

    def block_until_active(self, timeout=None):
        self.block_calls.append({"timeout": timeout})

    def pause(self):
        self.pause_calls += 1

    def play(self):
        self.play_calls += 1

    def seek(self, sec):
        self.seek_calls.append(sec)

    def stop(self):
        self.stop_calls += 1


class _FakeCast:
    """In-memory pychromecast Chromecast replacement."""

    def __init__(self, app_id: str = "CC1AD845", uuid: str = "uuid-1"):
        self.app_id = app_id
        self.uuid = uuid
        self.media_controller = _FakeMediaController()
        self.wait_calls = 0
        self.wait_timeouts: List[Optional[float]] = []
        self.set_volume_calls: List[float] = []
        self.quit_app_calls = 0
        self.wait_raises: Optional[Exception] = None

    def wait(self, timeout=None):
        self.wait_calls += 1
        self.wait_timeouts.append(timeout)
        if self.wait_raises is not None:
            raise self.wait_raises

    def set_volume(self, level: float):
        self.set_volume_calls.append(level)

    def quit_app(self):
        self.quit_app_calls += 1


def _device(cast: Optional[object] = None, name: str = "Speaker") -> CastDevice:
    return CastDevice(
        name=name,
        host="10.0.0.42",
        port=8009,
        device_type="chromecast",
        uuid="uuid-1",
        cast_object=cast,
        cast_type="audio",
    )


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def manager():
    return CastManager()


@pytest.fixture(autouse=True)
def fast_time(monkeypatch):
    """Make the ``cast_to_chromecast`` poll loop run instantly.

    The production code resolves ``time.sleep`` / ``time.monotonic``
    through the module-level ``time`` in ``_chromecast``. We replace it
    with a fake clock: ``sleep`` is a no-op that advances a counter,
    ``monotonic`` returns that counter — so the 12 s polling deadline is
    crossed in a handful of (instant) iterations if a test deliberately
    keeps the receiver out of a terminal state.
    """
    clock = {"t": 0.0}

    def _monotonic():
        return clock["t"]

    def _sleep(secs):
        clock["t"] += max(secs, 0.0)

    monkeypatch.setattr(_cc_mod.time, "monotonic", _monotonic)
    monkeypatch.setattr(_cc_mod.time, "sleep", _sleep)
    return clock


@pytest.fixture(autouse=True)
def identity_proxy(monkeypatch):
    """Patch ``resolve_cast_url`` (imported at call time inside
    ``cast_to_chromecast``) to record + return the URL unchanged, so no
    proxy / QSettings machinery runs."""
    seen: List[str] = []

    def _resolve(url):
        seen.append(url)
        return url

    import jellytoast.cast_proxy as _proxy

    monkeypatch.setattr(_proxy, "resolve_cast_url", _resolve)
    return seen


@pytest.fixture
def inline_run_async(monkeypatch):
    """Run the async wrappers' worker inline on the calling thread."""

    def _run(fn, on_result=None, on_error=None):
        try:
            v = fn()
        except Exception as e:  # pragma: no cover — workers don't raise here
            if on_error:
                on_error(e)
            return
        if on_result:
            on_result(v)

    monkeypatch.setattr(_cm_mod, "run_async", _run)
    return _run


# ── chromecast_audio_mime_for ────────────────────────────────────────────────


class TestAudioMime:
    @pytest.mark.parametrize(
        "container,expected",
        [
            ("mp3", "audio/mpeg"),
            ("flac", "audio/flac"),
            ("ogg", "audio/ogg"),
            ("oga", "audio/ogg"),
            ("opus", "audio/ogg"),
            ("wav", "audio/wav"),
            ("wave", "audio/wav"),
            ("m4a", "audio/mp4"),
            ("mp4", "audio/mp4"),
            ("aac", "audio/aac"),
            ("webm", "audio/webm"),
        ],
    )
    def test_known_containers_map_to_mime(self, container, expected):
        assert CastManager.chromecast_audio_mime_for(container) == expected

    @pytest.mark.parametrize(
        "container",
        ["FLAC", "Mp3", "M4A", "OGG", "WaV"],
    )
    def test_case_insensitive(self, container):
        # Lower-cased before lookup, so mixed/upper case still resolves.
        assert (
            CastManager.chromecast_audio_mime_for(container)
            == CastManager.chromecast_audio_mime_for(container.lower())
        )

    @pytest.mark.parametrize(
        "container",
        ["alac", "ape", "wma", "dsf", "aiff", "", "  ", "unknownext"],
    )
    def test_transcode_fallback_returns_none(self, container):
        # Anything outside the direct-play matrix → None (caller
        # transcodes to MP3 before reaching the cast path).
        assert CastManager.chromecast_audio_mime_for(container) is None

    def test_none_container_returns_none(self):
        assert CastManager.chromecast_audio_mime_for(None) is None

    def test_is_classmethod_callable_on_instance(self, manager):
        assert manager.chromecast_audio_mime_for("flac") == "audio/flac"


# ── connect_to_chromecast ────────────────────────────────────────────────────


class TestConnect:
    def test_connect_success_waits_and_sets_active(self, manager):
        cast = _FakeCast()
        dev = _device(cast)
        assert manager.connect_to_chromecast(dev) is True
        assert cast.wait_calls == 1
        assert manager.active_cast is dev

    def test_connect_none_cast_object_returns_false(self, manager):
        dev = _device(None)
        assert manager.connect_to_chromecast(dev) is False
        assert manager.active_cast is None

    def test_connect_wait_failure_returns_false(self, manager):
        cast = _FakeCast()
        cast.wait_raises = RuntimeError("socket negotiation failed")
        dev = _device(cast)
        assert manager.connect_to_chromecast(dev) is False
        # active_cast must not be left pointing at a device we never
        # actually reached.
        assert manager.active_cast is None


# ── cast_to_chromecast ───────────────────────────────────────────────────────


class TestCastToChromecast:
    def test_success_plays_media_and_sets_active(self, manager, identity_proxy):
        cast = _FakeCast()
        dev = _device(cast)
        ok = manager.cast_to_chromecast(
            dev, "http://server/stream.flac", title="Song", is_audio=True
        )
        assert ok is True
        assert manager.active_cast is dev
        assert cast.wait_calls == 1
        # play_media was called once with the resolved url.
        assert len(cast.media_controller.play_media_calls) == 1
        url, content_type, kwargs = cast.media_controller.play_media_calls[0]
        assert url == "http://server/stream.flac"
        # block_until_active is invoked with the 8 s session timeout.
        assert cast.media_controller.block_calls == [{"timeout": 8}]
        # The url passed through resolve_cast_url.
        assert "http://server/stream.flac" in identity_proxy

    def test_none_cast_object_returns_false(self, manager):
        dev = _device(None)
        assert manager.cast_to_chromecast(dev, "http://x/a.mp3") is False

    def test_default_audio_content_type_is_mpeg(self, manager):
        cast = _FakeCast()
        dev = _device(cast)
        manager.cast_to_chromecast(dev, "http://x/a.mp3", is_audio=True)
        _, content_type, _ = cast.media_controller.play_media_calls[0]
        assert content_type == "audio/mpeg"

    def test_default_video_content_type_is_mp4(self, manager):
        cast = _FakeCast()
        dev = _device(cast)
        manager.cast_to_chromecast(dev, "http://x/v.mp4", is_audio=False)
        _, content_type, _ = cast.media_controller.play_media_calls[0]
        assert content_type == "video/mp4"

    def test_explicit_content_type_override(self, manager):
        cast = _FakeCast()
        dev = _device(cast)
        manager.cast_to_chromecast(
            dev, "http://x/a.flac", is_audio=True, content_type="audio/flac"
        )
        _, content_type, _ = cast.media_controller.play_media_calls[0]
        assert content_type == "audio/flac"

    def test_live_stream_type_for_radio(self, manager):
        cast = _FakeCast()
        dev = _device(cast)
        manager.cast_to_chromecast(dev, "http://radio/icecast", is_live=True)
        _, _, kwargs = cast.media_controller.play_media_calls[0]
        assert kwargs["stream_type"] == "LIVE"

    def test_buffered_stream_type_for_vod(self, manager):
        cast = _FakeCast()
        dev = _device(cast)
        manager.cast_to_chromecast(dev, "http://x/a.mp3", is_live=False)
        _, _, kwargs = cast.media_controller.play_media_calls[0]
        assert kwargs["stream_type"] == "BUFFERED"

    def test_resume_position_passed_when_above_half_second(self, manager):
        cast = _FakeCast()
        dev = _device(cast)
        manager.cast_to_chromecast(dev, "http://x/a.mp3", current_time=42.0)
        _, _, kwargs = cast.media_controller.play_media_calls[0]
        assert kwargs["current_time"] == 42.0

    def test_resume_position_omitted_at_start(self, manager):
        cast = _FakeCast()
        dev = _device(cast)
        manager.cast_to_chromecast(dev, "http://x/a.mp3", current_time=0.0)
        _, _, kwargs = cast.media_controller.play_media_calls[0]
        assert "current_time" not in kwargs

    def test_title_and_thumb_forwarded(self, manager, identity_proxy):
        cast = _FakeCast()
        dev = _device(cast)
        manager.cast_to_chromecast(
            dev, "http://x/a.mp3", title="My Track", thumb="http://x/cover.jpg"
        )
        _, _, kwargs = cast.media_controller.play_media_calls[0]
        assert kwargs["title"] == "My Track"
        assert kwargs["thumb"] == "http://x/cover.jpg"
        # Both the stream url and the thumb run through resolve_cast_url.
        assert "http://x/cover.jpg" in identity_proxy

    def test_buffering_state_counts_as_success(self, manager):
        cast = _FakeCast()
        cast.media_controller.status.player_state = "BUFFERING"
        cast.media_controller.status.idle_reason = None
        dev = _device(cast)
        assert manager.cast_to_chromecast(dev, "http://x/a.mp3") is True
        assert manager.active_cast is dev

    def test_idle_error_is_failure(self, manager):
        cast = _FakeCast()
        cast.media_controller.status.player_state = "IDLE"
        cast.media_controller.status.idle_reason = "ERROR"
        cast.media_controller.status.player_is_playing = False
        dev = _device(cast)
        assert manager.cast_to_chromecast(dev, "http://x/a.mp3") is False
        # A rejected media session must not leave a stale active_cast.
        assert manager.active_cast is None

    def test_never_starts_is_failure(self, manager):
        # Receiver stays UNKNOWN forever → poll loop times out → final
        # state isn't a playing/paused state → failure.
        cast = _FakeCast()
        cast.media_controller.status.player_state = "UNKNOWN"
        cast.media_controller.status.idle_reason = None
        dev = _device(cast)
        assert manager.cast_to_chromecast(dev, "http://x/a.mp3") is False
        assert manager.active_cast is None

    def test_paused_final_state_counts_as_success(self, manager, fast_time):
        # Stays UNKNOWN through the 12 s poll window (so the loop never
        # early-returns on PLAYING/BUFFERING), then the final-status
        # check — which runs only after the deadline elapses — reads
        # PAUSED and counts that as a success. Drive the flip off the
        # fake clock: the poll loop's deadline is monotonic()+12, so
        # report UNKNOWN until the clock passes 12, PAUSED after.
        cast = _FakeCast()
        dev = _device(cast)

        real_status = cast.media_controller.status

        class _ClockStatus:
            def __getattr__(self, name):
                if name == "player_state":
                    return "PAUSED" if fast_time["t"] >= 12.0 else "UNKNOWN"
                return getattr(real_status, name)

        cast.media_controller.status = _ClockStatus()
        assert manager.cast_to_chromecast(dev, "http://x/a.mp3") is True
        assert manager.active_cast is dev

    def test_play_media_exception_returns_false(self, manager):
        cast = _FakeCast()

        def _boom(*a, **k):
            raise RuntimeError("receiver refused")

        cast.media_controller.play_media = _boom
        dev = _device(cast)
        assert manager.cast_to_chromecast(dev, "http://x/a.mp3") is False


# ── cast_to_chromecast_async / connect_to_chromecast_async ───────────────────


class TestAsyncWrappers:
    def test_cast_async_invokes_on_done_true(self, manager, inline_run_async):
        cast = _FakeCast()
        dev = _device(cast)
        result = {"ok": None}
        manager.cast_to_chromecast_async(
            dev,
            "http://x/a.flac",
            title="T",
            content_type="audio/flac",
            on_done=lambda ok: result.__setitem__("ok", ok),
        )
        assert result["ok"] is True
        # The sync path actually ran with the forwarded args.
        url, content_type, _ = cast.media_controller.play_media_calls[0]
        assert url == "http://x/a.flac"
        assert content_type == "audio/flac"

    def test_cast_async_invokes_on_done_false_on_idle_error(
        self, manager, inline_run_async
    ):
        cast = _FakeCast()
        cast.media_controller.status.player_state = "IDLE"
        cast.media_controller.status.idle_reason = "ERROR"
        dev = _device(cast)
        result = {"ok": None}
        manager.cast_to_chromecast_async(
            dev, "http://x/a.mp3", on_done=lambda ok: result.__setitem__("ok", ok)
        )
        assert result["ok"] is False

    def test_cast_async_no_on_done_is_safe(self, manager, inline_run_async):
        cast = _FakeCast()
        dev = _device(cast)
        # No on_done callback — must not raise.
        manager.cast_to_chromecast_async(dev, "http://x/a.mp3")
        assert len(cast.media_controller.play_media_calls) == 1

    def test_connect_async_invokes_on_done_true(self, manager, inline_run_async):
        cast = _FakeCast()
        dev = _device(cast)
        result = {"ok": None}
        manager.connect_to_chromecast_async(
            dev, on_done=lambda ok: result.__setitem__("ok", ok)
        )
        assert result["ok"] is True
        assert manager.active_cast is dev

    def test_connect_async_invokes_on_done_false(self, manager, inline_run_async):
        dev = _device(None)  # None cast_object → connect returns False
        result = {"ok": None}
        manager.connect_to_chromecast_async(
            dev, on_done=lambda ok: result.__setitem__("ok", ok)
        )
        assert result["ok"] is False


# ── Transport controls ───────────────────────────────────────────────────────


class TestTransport:
    def _armed(self, manager, **mc_status):
        cast = _FakeCast()
        for k, v in mc_status.items():
            setattr(cast.media_controller.status, k, v)
        dev = _device(cast)
        manager.active_cast = dev
        return cast

    # ── pause / play toggle ──────────────────────────────────────────
    def test_pause_when_playing_calls_pause(self, manager):
        cast = self._armed(manager, player_is_playing=True)
        manager.chromecast_pause()
        assert cast.media_controller.pause_calls == 1
        assert cast.media_controller.play_calls == 0

    def test_pause_when_paused_calls_play(self, manager):
        cast = self._armed(manager, player_is_playing=False)
        manager.chromecast_pause()
        assert cast.media_controller.play_calls == 1
        assert cast.media_controller.pause_calls == 0

    def test_pause_noop_when_no_active_cast(self, manager):
        manager.active_cast = None
        manager.chromecast_pause()  # must not raise

    def test_pause_noop_for_non_chromecast_active(self, manager):
        cast = _FakeCast()
        dev = _device(cast)
        dev.device_type = "airplay"
        manager.active_cast = dev
        manager.chromecast_pause()
        assert cast.media_controller.pause_calls == 0
        assert cast.media_controller.play_calls == 0

    # ── seek ─────────────────────────────────────────────────────────
    def test_seek_forwards_seconds(self, manager):
        cast = self._armed(manager)
        manager.chromecast_seek(123.5)
        assert cast.media_controller.seek_calls == [123.5]

    def test_seek_noop_when_no_active_cast(self, manager):
        manager.active_cast = None
        manager.chromecast_seek(10.0)  # must not raise

    # ── volume ───────────────────────────────────────────────────────
    def test_set_volume_scales_percent_to_fraction(self, manager):
        cast = self._armed(manager)
        manager.chromecast_set_volume(50)
        assert cast.set_volume_calls == [0.5]

    def test_set_volume_clamps_high(self, manager):
        cast = self._armed(manager)
        manager.chromecast_set_volume(150)
        assert cast.set_volume_calls == [1.0]

    def test_set_volume_clamps_negative(self, manager):
        cast = self._armed(manager)
        manager.chromecast_set_volume(-20)
        assert cast.set_volume_calls == [0.0]

    def test_set_volume_swallows_exception(self, manager):
        cast = self._armed(manager)

        def _boom(level):
            raise RuntimeError("volume command failed")

        cast.set_volume = _boom
        # Logged + swallowed, not raised.
        manager.chromecast_set_volume(50)

    def test_set_volume_noop_when_no_active_cast(self, manager):
        manager.active_cast = None
        manager.chromecast_set_volume(50)  # must not raise

    # ── stop ─────────────────────────────────────────────────────────
    def test_stop_stops_media_quits_app_clears_active(self, manager):
        cast = self._armed(manager)
        manager.chromecast_stop()
        assert cast.media_controller.stop_calls == 1
        assert cast.quit_app_calls == 1
        assert manager.active_cast is None

    def test_stop_clears_active_even_on_exception(self, manager):
        cast = self._armed(manager)

        def _boom():
            raise RuntimeError("stop failed")

        cast.media_controller.stop = _boom
        manager.chromecast_stop()
        # The except swallows; active_cast is still cleared.
        assert manager.active_cast is None

    def test_stop_noop_clears_active_when_non_chromecast(self, manager):
        cast = _FakeCast()
        dev = _device(cast)
        dev.device_type = "sonos"
        manager.active_cast = dev
        manager.chromecast_stop()
        assert cast.media_controller.stop_calls == 0
        # stop always clears active_cast at the end regardless of type.
        assert manager.active_cast is None


# ── Internet-radio casting (MIME + proxy bypass) ─────────────────────────────


class TestRadioCast:
    """Casting a live internet-radio stream. Two fixes verified here:
    the declared MIME is derived from the stream URL (an AAC SomaFM stream
    sent as audio/mpeg was rejected by the receiver), and a live stream skips
    the local cast proxy (a public CDN the device fetches directly — proxying
    it via a firewalled port only broke the cast)."""

    @pytest.mark.parametrize(
        "url, expected",
        [
            ("https://ice2.somafm.com/groovesalad-128-aac", "audio/aac"),
            ("https://ice2.somafm.com/indiepop-128-aac", "audio/aac"),
            ("https://kexp-mp3-128.streamguys1.com/kexp128.mp3", "audio/mpeg"),
            ("https://example.com/stream.ogg", "audio/ogg"),
            ("https://example.com/stream.opus", "audio/ogg"),
            ("https://example.com/stream.flac", "audio/flac"),
            ("https://example.com/stream.m4a", "audio/aac"),
            ("https://example.com/whatever", "audio/mpeg"),  # unknown → broadest
            ("https://ice2.somafm.com/groovesalad-128-aac?x=1", "audio/aac"),  # query stripped
            ("", "audio/mpeg"),
        ],
    )
    def test_radio_mime_for(self, url, expected):
        from jellytoast.cast_manager._manager import _radio_mime_for

        assert _radio_mime_for(url) == expected

    def test_live_stream_bypasses_proxy_and_is_marked_live(self, manager, identity_proxy):
        cast = _FakeCast()
        dev = _device(cast)
        radio_url = "https://ice2.somafm.com/groovesalad-128-aac"
        ok = manager.cast_to_chromecast(
            dev, radio_url, title="Groove Salad", is_audio=True,
            content_type="audio/aac", is_live=True,
        )
        assert ok is True
        url, content_type, kwargs = cast.media_controller.play_media_calls[0]
        # URL passed through verbatim — NOT routed through the proxy.
        assert url == radio_url
        assert radio_url not in identity_proxy
        # Declared MIME honoured; endless stream marked LIVE.
        assert content_type == "audio/aac"
        assert kwargs.get("stream_type") == "LIVE"

    def test_non_live_track_still_uses_proxy(self, manager, identity_proxy):
        # Regression guard: a normal (VOD) track must still resolve through the
        # proxy so the Tailscale / remote / self-signed routing keeps working.
        cast = _FakeCast()
        dev = _device(cast)
        manager.cast_to_chromecast(
            dev, "http://server/stream.flac", is_audio=True, is_live=False,
        )
        assert "http://server/stream.flac" in identity_proxy
        _, _, kwargs = cast.media_controller.play_media_calls[0]
        assert kwargs.get("stream_type") == "BUFFERED"
