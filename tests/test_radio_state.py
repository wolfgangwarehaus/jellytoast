"""Tests for ``modules.radio_state`` — the unified presentation
pipeline that turns raw ``queue_context_changed`` /
``radio_title_changed`` events into a single ``RadioState`` snapshot
consumed by every radio surface.

Pieces under test:

* RadioState's ``display_*`` properties (the fallback ladder that
  surfaces rely on for "song-or-station", "art-or-logo", etc.).
* Entering / leaving radio mode emits ``radio_state_changed`` with the
  correct payload (a fresh RadioState, or ``None``).
* An ICY title fires an immediate emit with parsed song/artist (so
  surfaces repaint without waiting for the network), then a second
  emit once the cover lookup lands.
* The MusicBrainz lookup is mocked so the suite never reaches the
  wire.
"""

from __future__ import annotations

from unittest import mock

import pytest

from modules import radio_state
from modules.player_state import (
    PlayerBus,
    QueueContext,
    QueueKind,
)


# ── Fixtures ───────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_state():
    """Each test starts from a clean radio_state + a fresh PlayerBus.
    Without resetting the singleton, signal slots from previous tests
    would still fire and corrupt the next case's spy."""
    PlayerBus._instance = None
    radio_state._reset_for_tests()
    yield
    radio_state._reset_for_tests()
    PlayerBus._instance = None


@pytest.fixture
def bus():
    """Live PlayerBus with radio_state wired up. Tests call into the
    real bus emit so the same code path the app uses is exercised."""
    radio_state.init()
    return PlayerBus.get()


@pytest.fixture
def captured(bus):
    """Capture every ``radio_state_changed`` payload in emission order."""
    events: list = []
    bus.radio_state_changed.connect(lambda s: events.append(s))
    return events


@pytest.fixture
def mock_lookup(monkeypatch):
    """Replace the blocking network lookup with a stub the test
    controls. Default behaviour: return ``None`` (no MB match)."""
    calls: list = []

    def _stub(artist, title):
        calls.append((artist, title))
        return None

    monkeypatch.setattr(radio_state, "_pending_lookup_title", "")
    # Patch lookup_art_url at its import site inside radio_state's
    # _on_icy_title (it's a deferred import for cold-start cost). The
    # closure captures the function by name, so we patch the module
    # the import targets.
    import modules.radio_art as _ra

    monkeypatch.setattr(_ra, "lookup_art_url", _stub)
    return calls


@pytest.fixture
def sync_run_async(monkeypatch):
    """Replace ``run_async`` with a synchronous invoker so test
    cases don't need to drive a real QThreadPool worker. The result
    callback fires synchronously with whatever the wrapped function
    returns."""

    def _sync(fn, *args, on_result=None, on_error=None, **kwargs):
        try:
            result = fn(*args, **kwargs)
        except Exception as e:  # pragma: no cover — defensive
            if on_error is not None:
                on_error(e)
            return
        if on_result is not None:
            on_result(result)

    import modules.async_io as async_mod

    monkeypatch.setattr(async_mod, "run_async", _sync)
    # radio_state imports run_async lazily; rebind on the module
    # so the next call inside _on_icy_title picks up the stub.
    monkeypatch.setattr(radio_state, "run_async", _sync, raising=False)


# ── Dataclass surface ──────────────────────────────────────────────────────


class TestRadioStateProperties:
    def test_display_title_prefers_song(self):
        s = radio_state.RadioState(
            station_name="KEXP",
            song_title="Camel",
            song_artist="Flying Lotus",
        )
        assert s.display_title == "Camel"

    def test_display_title_falls_back_to_station(self):
        s = radio_state.RadioState(station_name="KEXP")
        assert s.display_title == "KEXP"

    def test_display_subtitle_is_artist(self):
        s = radio_state.RadioState(
            station_name="KEXP",
            song_title="Camel",
            song_artist="Flying Lotus",
        )
        assert s.display_subtitle == "Flying Lotus"

    def test_display_subtitle_empty_without_icy(self):
        s = radio_state.RadioState(station_name="KEXP")
        assert s.display_subtitle == ""

    def test_display_cover_prefers_art_then_logo(self):
        s = radio_state.RadioState(
            station_logo_url="https://kexp/logo.png",
            art_url="https://caa/art.jpg",
        )
        assert s.display_cover_url == "https://caa/art.jpg"

    def test_display_cover_falls_back_to_logo(self):
        s = radio_state.RadioState(
            station_logo_url="https://kexp/logo.png",
        )
        assert s.display_cover_url == "https://kexp/logo.png"

    def test_display_cover_empty_when_neither(self):
        s = radio_state.RadioState(station_name="X")
        assert s.display_cover_url == ""


# ── Enter / leave radio mode ───────────────────────────────────────────────


class TestRadioContextTransitions:
    def test_entering_emits_fresh_state(self, bus, captured):
        ctx = QueueContext(
            kind=QueueKind.INTERNET_RADIO,
            source_id="kexp",
            source_label="KEXP 90.3 Seattle",
            source_icon="https://www.kexp.org/apple-touch-icon.png",
        )
        bus.queue_context_changed.emit(ctx)
        assert len(captured) == 1
        s = captured[0]
        assert s is not None
        assert s.station_name == "KEXP 90.3 Seattle"
        assert s.station_logo_url == "https://www.kexp.org/apple-touch-icon.png"
        assert s.icy_title == ""
        assert s.song_title == ""
        assert s.song_artist == ""
        assert s.art_url == ""
        # And the module-level snapshot reflects it.
        assert radio_state.current() is s

    def test_leaving_emits_none(self, bus, captured):
        bus.queue_context_changed.emit(
            QueueContext(kind=QueueKind.INTERNET_RADIO, source_label="K")
        )
        captured.clear()
        bus.queue_context_changed.emit(QueueContext(kind=QueueKind.ALBUM))
        assert captured == [None]
        assert radio_state.current() is None

    def test_non_radio_context_does_not_emit(self, bus, captured):
        # Already not in radio mode — a switch between two non-radio
        # contexts shouldn't fire ``radio_state_changed`` at all.
        bus.queue_context_changed.emit(QueueContext(kind=QueueKind.ALBUM))
        bus.queue_context_changed.emit(QueueContext(kind=QueueKind.PLAYLIST))
        assert captured == []


# ── ICY title arrival ──────────────────────────────────────────────────────


class TestIcyTitleHandling:
    def test_icy_emits_parsed_state_immediately(
        self, bus, captured, mock_lookup, sync_run_async
    ):
        bus.queue_context_changed.emit(
            QueueContext(
                kind=QueueKind.INTERNET_RADIO,
                source_label="KEXP",
                source_icon="https://kexp/logo.png",
            )
        )
        captured.clear()
        bus.radio_title_changed.emit("Flying Lotus - Camel")
        # First emit carries the parsed text (no art yet — lookup is a
        # no-op stub).
        assert len(captured) >= 1
        first = captured[0]
        assert first.song_title == "Camel"
        assert first.song_artist == "Flying Lotus"
        assert first.icy_title == "Flying Lotus - Camel"
        # Art reset to "" so the surface falls back to the logo until
        # the new lookup lands.
        assert first.art_url == ""

    def test_icy_outside_radio_mode_is_ignored(self, bus, captured):
        bus.radio_title_changed.emit("Artist - Title")
        assert captured == []

    def test_one_segment_title_clears_artist(
        self, bus, captured, mock_lookup, sync_run_async
    ):
        bus.queue_context_changed.emit(
            QueueContext(kind=QueueKind.INTERNET_RADIO, source_label="X")
        )
        captured.clear()
        bus.radio_title_changed.emit("Some Station ID")
        s = captured[-1]
        assert s.song_artist == ""
        assert s.song_title == "Some Station ID"

    def test_successful_lookup_patches_art_url(self, bus, captured, monkeypatch):
        bus.queue_context_changed.emit(
            QueueContext(
                kind=QueueKind.INTERNET_RADIO,
                source_label="KEXP",
                source_icon="https://kexp/logo.png",
            )
        )
        captured.clear()

        art = "https://caa/cover.jpg"

        # ``radio_state._on_icy_title`` does a local
        # ``from modules.async_io import run_async`` to keep cold-start
        # cost down — so patching radio_state.run_async won't take.
        # Patch the source module the local import resolves to.
        import modules.radio_art as _ra
        import modules.async_io as _async_io

        monkeypatch.setattr(_ra, "lookup_art_url", lambda a, t: art)

        def _sync(fn, *args, on_result=None, on_error=None, **kwargs):
            try:
                r = fn(*args, **kwargs)
            except Exception as e:
                if on_error is not None:
                    on_error(e)
                return
            if on_result is not None:
                on_result(r)

        monkeypatch.setattr(_async_io, "run_async", _sync)

        bus.radio_title_changed.emit("Flying Lotus - Camel")

        # Two emits in total: initial parse, then the lookup result.
        assert len(captured) == 2
        final = captured[-1]
        assert final.art_url == art
        assert final.song_title == "Camel"
        assert final.song_artist == "Flying Lotus"
        # The module-level current() points at the final.
        assert radio_state.current() is final


# ── Playback-state gating for the LIVE indicator ───────────────────────────


class TestPlaybackStateGating:
    def test_default_is_stopped_on_entry(self, bus, captured):
        bus.queue_context_changed.emit(
            QueueContext(kind=QueueKind.INTERNET_RADIO, source_label="K")
        )
        s = captured[-1]
        assert s.playback_state == "stopped"
        assert s.is_live is False

    def test_playback_started_promotes_to_playing(self, bus, captured):
        bus.queue_context_changed.emit(
            QueueContext(kind=QueueKind.INTERNET_RADIO, source_label="K")
        )
        captured.clear()
        # ``playback_started`` carries a NowPlaying object; the radio
        # subscriber only reads the fact that playback started.
        from modules.player_state import NowPlaying

        bus.playback_started.emit(NowPlaying(item_id="kexp"))
        assert len(captured) == 1
        assert captured[-1].is_live is True
        assert captured[-1].playback_state == "playing"

    def test_pause_demotes_to_paused(self, bus, captured):
        bus.queue_context_changed.emit(
            QueueContext(kind=QueueKind.INTERNET_RADIO, source_label="K")
        )
        from modules.player_state import NowPlaying

        bus.playback_started.emit(NowPlaying(item_id="kexp"))
        captured.clear()
        bus.playback_paused.emit()
        assert captured[-1].playback_state == "paused"
        assert captured[-1].is_live is False

    def test_resume_returns_to_playing(self, bus, captured):
        bus.queue_context_changed.emit(
            QueueContext(kind=QueueKind.INTERNET_RADIO, source_label="K")
        )
        from modules.player_state import NowPlaying

        bus.playback_started.emit(NowPlaying(item_id="kexp"))
        bus.playback_paused.emit()
        captured.clear()
        bus.playback_resumed.emit()
        assert captured[-1].is_live is True

    def test_playback_restored_lands_paused_not_playing(self, bus, captured):
        # Cold restore from session: the queue installs a radio
        # context, then playback_restored fires with a paused NowPlaying.
        # The LIVE indicator must NOT light up — the user hasn't hit
        # play yet.
        bus.queue_context_changed.emit(
            QueueContext(kind=QueueKind.INTERNET_RADIO, source_label="K")
        )
        captured.clear()
        from modules.player_state import NowPlaying

        bus.playback_restored.emit(NowPlaying(item_id="kexp"))
        assert captured[-1].is_live is False
        assert captured[-1].playback_state == "paused"

    def test_stopped_event_clears_to_stopped(self, bus, captured):
        bus.queue_context_changed.emit(
            QueueContext(kind=QueueKind.INTERNET_RADIO, source_label="K")
        )
        from modules.player_state import NowPlaying

        bus.playback_started.emit(NowPlaying(item_id="kexp"))
        captured.clear()
        bus.playback_stopped.emit()
        assert captured[-1].playback_state == "stopped"
        assert captured[-1].is_live is False

    def test_redundant_state_change_no_emit(self, bus, captured):
        bus.queue_context_changed.emit(
            QueueContext(kind=QueueKind.INTERNET_RADIO, source_label="K")
        )
        from modules.player_state import NowPlaying

        bus.playback_started.emit(NowPlaying(item_id="kexp"))
        captured.clear()
        # Already in "playing" — another playback_started shouldn't
        # generate an extra emit. Surfaces re-render is cheap but
        # spurious events still cause QML / Qt jitter.
        bus.playback_started.emit(NowPlaying(item_id="kexp"))
        assert captured == []

    def test_playback_events_outside_radio_dont_emit(self, bus, captured):
        # No radio context active — playback signals from a normal
        # album shouldn't fire ``radio_state_changed``.
        from modules.player_state import NowPlaying

        bus.playback_started.emit(NowPlaying(item_id="t1"))
        bus.playback_paused.emit()
        bus.playback_resumed.emit()
        bus.playback_stopped.emit()
        assert captured == []


# ── Mid-session seed ───────────────────────────────────────────────────────


class TestCurrentSnapshot:
    def test_current_is_none_initially(self, bus):
        assert radio_state.current() is None

    def test_current_returns_active_state(self, bus):
        bus.queue_context_changed.emit(
            QueueContext(kind=QueueKind.INTERNET_RADIO, source_label="K")
        )
        s = radio_state.current()
        assert s is not None
        assert s.station_name == "K"

    def test_init_is_idempotent(self):
        # Calling init() twice doesn't double-subscribe the bus.
        bus = PlayerBus.get()
        radio_state.init()
        radio_state.init()
        events: list = []
        bus.radio_state_changed.connect(lambda s: events.append(s))
        bus.queue_context_changed.emit(
            QueueContext(kind=QueueKind.INTERNET_RADIO, source_label="K")
        )
        # Exactly one emit — not two from a doubled subscription.
        assert len(events) == 1
