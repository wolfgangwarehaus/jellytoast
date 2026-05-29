"""Tests for MpvController — auto-advance handoff, toggle_pause cold-
launch promotion, _on_paused path-gate, and prefetch lifecycle.

mpv is replaced with a `FakeMpv` that mimics python-mpv's surface:
attribute-style property reads/writes (`mpv.path`), `[]` for option
lookup, `play(url)` / `command(...)` methods. Construction is gated
on MPV_AVAILABLE=False so `_init_mpv` is skipped — we inject the fake
and call `_connect_bus` manually.
"""

from typing import Any, Dict, List

import pytest

from modules.player_state import (
    NowPlaying,
    PlayerBus,
    set_now_playing,
)

# ── Test doubles ────────────────────────────────────────────────────────────


class FakeMpv:
    """Stand-in for `mpv.MPV`. Backs property reads with attributes,
    option reads/writes with a dict (so `mpv["start"] = "12.5"` is
    inspectable in tests). `play()` records the URL and updates the
    state attributes the way real mpv would (path set, idle cleared)."""

    def __init__(self):
        self.path: Any = None
        self.idle_active: bool = True
        self.core_idle: bool = True
        self.pause: bool = False
        self.playlist_count: int = 0
        self.playlist_pos: int = -1
        self.options: Dict[str, Any] = {}
        self.commands: List[tuple] = []
        self.play_calls: List[str] = []

    def __getitem__(self, key):
        return self.options.get(key)

    def __setitem__(self, key, value):
        self.options[key] = value

    def play(self, url):
        self.play_calls.append(url)
        self.path = url
        self.idle_active = False
        self.core_idle = False
        # Real mpv would replace the playlist with the new entry; reset
        # playlist tracking so a "play after prefetch" looks right.
        self.playlist_count = 1
        self.playlist_pos = 0

    def command(self, *args):
        self.commands.append(args)
        if args[0] == "loadfile" and len(args) >= 3 and args[2] == "append":
            self.playlist_count += 1
        elif args[0] == "playlist-remove":
            self.playlist_count = max(0, self.playlist_count - 1)

    def stop(self):
        self.path = None
        self.idle_active = True
        self.core_idle = True
        self.playlist_count = 0
        self.playlist_pos = -1

    def terminate(self):
        pass


class FakeProvider:
    kind = "fake"

    def get_audio_stream_url(self, item_id):
        return f"stream://{item_id}"

    def get_video_stream_url(self, item_id):
        return f"stream://{item_id}"

    def get_image_url(self, item_id, image_type, width):
        return f"img://{item_id}"

    def report_playback_start(self, *a, **kw): ...
    def report_playback_progress(self, *a, **kw): ...
    def report_playback_stopped(self, *a, **kw): ...
    def get_audio_transcode_url(self, *a, **kw):
        return ""


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def fake_provider(monkeypatch):
    fp = FakeProvider()
    import modules.providers as providers_mod

    monkeypatch.setattr(providers_mod, "_PROVIDER", fp)
    yield fp
    monkeypatch.setattr(providers_mod, "_PROVIDER", None)


@pytest.fixture
def isolated_settings_singleton(tmp_path, monkeypatch):
    import modules.settings as settings_mod

    s = settings_mod.Settings()
    monkeypatch.setattr(s, "_config_dir", tmp_path)
    monkeypatch.setattr(settings_mod, "_settings", s)
    s._s.clear()
    s._s.sync()
    return s


@pytest.fixture
def fresh_bus():
    PlayerBus._instance = None
    yield
    PlayerBus._instance = None


@pytest.fixture
def controller(qapp, fake_provider, isolated_settings_singleton, fresh_bus, monkeypatch):
    """Build an MpvController with a FakeMpv injected. MPV_AVAILABLE is
    flipped off so `_init_mpv` (which would try to spawn a real mpv
    process) is skipped; the fake is wired in manually and `_connect_bus`
    is called explicitly."""
    import modules.player_backend as backend_mod

    monkeypatch.setattr(backend_mod, "MPV_AVAILABLE", False)
    # _MPV_ERROR is only defined when the mpv import itself failed; on
    # a normal install where mpv is present, the constructor's
    # "mpv unavailable" branch references an undefined name. Set a
    # placeholder so MpvController.__init__ can print + return cleanly.
    monkeypatch.setattr(backend_mod, "_MPV_ERROR", "test mode", raising=False)
    from modules.player_backend import MpvController

    c = MpvController()
    c._mpv = FakeMpv()
    c._connect_bus()
    return c


def _capture(signal) -> List:
    out: List = []
    signal.connect(lambda *args: out.append(args))
    return out


def _np(item_id="x", url=None, position=0, item_type="Audio"):
    return NowPlaying(
        item_id=item_id,
        title=f"Track {item_id}",
        stream_url=url if url is not None else f"stream://{item_id}",
        position=position,
        item_type=item_type,
    )


# ── toggle_pause cold-launch promotion ──────────────────────────────────────


class TestTogglePauseColdLaunch:
    def test_idle_with_now_playing_promotes_to_play(self, controller):
        # mpv has no media loaded, but NowPlaying carries a saved-position
        # restore. The press should promote to play_requested(np).
        controller._mpv.path = None
        controller._mpv.idle_active = True
        np = _np(item_id="abc", position=27488)
        set_now_playing(np)

        plays = _capture(controller.bus.play_requested)
        controller.toggle_pause()

        assert len(plays) == 1
        assert plays[0][0].item_id == "abc"
        assert plays[0][0].position == 27488

    def test_idle_with_no_now_playing_is_noop(self, controller):
        controller._mpv.path = None
        controller._mpv.idle_active = True
        # Empty NowPlaying — no track to resume
        set_now_playing(NowPlaying())

        plays = _capture(controller.bus.play_requested)
        controller.toggle_pause()
        assert plays == []

    def test_loaded_toggles_pause(self, controller):
        controller._mpv.path = "stream://abc"
        controller._mpv.idle_active = False
        controller._mpv.pause = False

        controller.toggle_pause()
        assert controller._mpv.pause is True

        controller.toggle_pause()
        assert controller._mpv.pause is False


# ── _on_paused path-gate ────────────────────────────────────────────────────


class TestOnPausedGate:
    def test_no_path_suppresses_emits(self, controller):
        """The pause property observer fires once on registration with
        mpv's default pause=False. Without a path-gate that would emit
        playback_resumed at boot and lie to the UI."""
        controller._mpv.path = None

        resumes = _capture(controller.bus.playback_resumed)
        pauses = _capture(controller.bus.playback_paused)
        controller._on_paused(False)
        controller._on_paused(True)

        assert resumes == []
        assert pauses == []

    def test_with_path_emits_resumed(self, controller):
        controller._mpv.path = "stream://x"
        resumes = _capture(controller.bus.playback_resumed)
        controller._on_paused(False)
        assert len(resumes) == 1

    def test_with_path_emits_paused(self, controller):
        controller._mpv.path = "stream://x"
        pauses = _capture(controller.bus.playback_paused)
        controller._on_paused(True)
        assert len(pauses) == 1


# ── Auto-advance handoff (gapless) ──────────────────────────────────────────


class TestAutoAdvanceHandoff:
    def test_handoff_skips_loadfile_when_item_id_matches(self, controller):
        """The Subsonic gapless fix: mpv is already on the prefetched URL,
        and play() is called with a NowPlaying for the same item_id. The
        URL strings differ (rotating salt) but the handoff should still
        be recognized — no loadfile-replace."""
        # Simulate the post-handoff state: mpv has advanced gaplessly
        # to the prefetched entry, so its `path` matches what we
        # prefetched (URL_v1).
        prefetched_url = "stream://B?u=avtips&t=v1_token&s=v1_salt"
        controller._mpv.path = prefetched_url
        controller._mpv.idle_active = False
        controller._mpv.core_idle = False
        controller._prefetched_url = prefetched_url
        controller._prefetched_item_id = "B"

        # play() is called with a freshly-built URL (rotated salt).
        np = _np(item_id="B", url="stream://B?u=avtips&t=v2_token&s=v2_salt")
        plays_before = len(controller._mpv.play_calls)
        controller.play(np)
        assert len(controller._mpv.play_calls) == plays_before
        # State cleared after handoff
        assert controller._prefetched_url is None
        assert controller._prefetched_item_id is None

    def test_no_handoff_when_item_id_differs(self, controller):
        """Different item_id from what we prefetched — must reload."""
        controller._mpv.path = "stream://B?t=v1"
        controller._mpv.idle_active = False
        controller._mpv.core_idle = False
        controller._prefetched_url = "stream://B?t=v1"
        controller._prefetched_item_id = "B"

        np = _np(item_id="C", url="stream://C?t=fresh")
        controller.play(np)
        assert controller._mpv.play_calls == ["stream://C?t=fresh"]

    def test_no_handoff_when_mpv_idle(self, controller):
        """Even if item_id matches, an idle mpv must reload."""
        controller._mpv.path = None
        controller._mpv.idle_active = True
        controller._mpv.core_idle = True
        controller._prefetched_url = "stream://B?t=v1"
        controller._prefetched_item_id = "B"

        np = _np(item_id="B", url="stream://B?t=v2")
        controller.play(np)
        assert controller._mpv.play_calls == ["stream://B?t=v2"]

    def test_jellyfin_stable_url_still_handoffs(self, controller):
        """Jellyfin URLs are stable across calls — the handoff should
        still recognize the gapless transition because _prefetched_url
        equals np.stream_url AND item_id matches."""
        url = "http://jf/Audio/B/stream?api_key=K&MediaSourceId=B"
        controller._mpv.path = url
        controller._mpv.idle_active = False
        controller._mpv.core_idle = False
        controller._prefetched_url = url
        controller._prefetched_item_id = "B"

        np = _np(item_id="B", url=url)
        controller.play(np)
        assert controller._mpv.play_calls == []  # no reload


# ── Prefetch lifecycle ──────────────────────────────────────────────────────


class TestPrefetchLifecycle:
    def test_prefetch_appends_and_records_state(self, controller):
        """When prefetch fires while mpv is playing, append the next
        URL and remember the item_id so the handoff check can match."""
        controller._mpv.path = "stream://A"
        controller._mpv.idle_active = False
        controller._mpv.core_idle = False

        np = _np(item_id="B", url="stream://B?t=v1")
        controller._on_prefetch_request(np)
        # loadfile-append issued
        assert any(cmd[:2] == ("loadfile", "stream://B?t=v1") for cmd in controller._mpv.commands)
        assert controller._prefetched_url == "stream://B?t=v1"
        assert controller._prefetched_item_id == "B"

    def test_prefetch_skips_when_mpv_idle(self, controller):
        """No point queueing two cold starts back-to-back."""
        controller._mpv.path = None
        controller._mpv.idle_active = True
        np = _np(item_id="B")
        controller._on_prefetch_request(np)
        assert controller._prefetched_url is None
        assert controller._mpv.commands == []

    def test_prefetch_skips_same_url(self, controller):
        """RepeatMode.ONE case — mpv would gaplessly re-play the same
        track natively if we set a loop, but we don't want a duplicate
        playlist entry."""
        controller._mpv.path = "stream://A"
        controller._mpv.idle_active = False
        controller._mpv.core_idle = False

        np = _np(item_id="A", url="stream://A")
        controller._on_prefetch_request(np)
        # No append issued
        assert not any(cmd[0] == "loadfile" for cmd in controller._mpv.commands)

    def test_prefetch_none_clears_state(self, controller):
        """An end-of-queue peek emits None — should drop any prior
        prefetch."""
        controller._mpv.path = "stream://A"
        controller._mpv.idle_active = False
        controller._mpv.playlist_count = 2
        controller._mpv.playlist_pos = 0
        controller._prefetched_url = "stream://B"
        controller._prefetched_item_id = "B"

        controller._on_prefetch_request(None)
        assert controller._prefetched_url is None
        assert controller._prefetched_item_id is None

    def test_play_clears_prefetch_state(self, controller):
        """Explicit play() does loadfile-replace, wiping mpv's playlist.
        Our prefetch tracking must follow."""
        controller._prefetched_url = "stream://stale"
        controller._prefetched_item_id = "stale"

        np = _np(item_id="X", url="stream://X")
        controller.play(np)
        assert controller._prefetched_url is None
        assert controller._prefetched_item_id is None
