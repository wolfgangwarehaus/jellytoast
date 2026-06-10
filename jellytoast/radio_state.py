"""Single source of truth for internet-radio presentation state.

Surfaces that show the now-playing strip (the NP bar, the floating
mini player, the full NowPlayingPage) all want to render the same
logical state when an internet-radio stream is playing: which
station, what the live ICY title decodes into, and what cover art to
show. This module owns that state.

Two raw bus signals drive it:

  * ``queue_context_changed`` — when the active queue's context flips
    in or out of ``INTERNET_RADIO``, we mint a fresh ``RadioState``
    (or clear it).
  * ``radio_title_changed`` — when mpv surfaces a new ICY title we
    parse it into artist + song, fire a ``radio_state_changed`` so
    surfaces re-render text immediately, then kick off the
    MusicBrainz / Cover Art Archive lookup. When that lands, the
    state's ``art_url`` is patched and another
    ``radio_state_changed`` fires.

Surfaces subscribe to ``PlayerBus.radio_state_changed`` and apply
state via one render method instead of duplicating the parse + lookup
plumbing across three files. ``current()`` returns the present
snapshot so a surface constructed mid-session (the user opens the
NowPlayingPage after radio started) can seed itself without waiting
for the next bus event.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Optional


@dataclass
class RadioState:
    """Snapshot of everything a radio surface needs to render.

    All fields are strings (no Optionals) so consumers can blindly
    apply via ``setText`` / image-load helpers without ``None``
    guards. Empty strings mean "no data yet" — the surface decides
    whether to fall back (station logo when art_url is empty, station
    name when song_title is empty, etc.)."""

    station_name: str = ""
    station_logo_url: str = ""
    # Raw text from mpv's icy-title observer — useful for surfaces
    # that want the original (rare) but most consumers only read the
    # parsed pair below.
    icy_title: str = ""
    song_title: str = ""
    song_artist: str = ""
    # Per-track cover URL resolved via MusicBrainz + Cover Art
    # Archive. Empty until the lookup lands (or never, if the track
    # isn't in MB / has no CAA art). Falls back to ``station_logo_url``
    # at render time.
    art_url: str = ""
    # Live-playback gate for the LIVE indicator. ``"playing"`` is the
    # only state where surfaces should announce ``● LIVE``; ``"paused"``
    # and ``"stopped"`` (cold restore, queue installed but never
    # started, mpv idle between tracks) downgrade the badge to a
    # quieter "PAUSED · station" or just the station name. Defaults to
    # ``"stopped"`` so a restored radio queue doesn't claim LIVE until
    # the user actually hits play.
    playback_state: str = "stopped"

    @property
    def is_live(self) -> bool:
        """True only when audio is actively streaming. Surfaces use
        this to decide whether to show the ``● LIVE`` badge or a
        quieter paused / idle variant."""
        return self.playback_state == "playing"

    @property
    def display_title(self) -> str:
        """What the title slot should show — the song name once ICY
        has decoded into a usable pair, else the station name."""
        return self.song_title or self.station_name

    @property
    def display_subtitle(self) -> str:
        """What the subtitle / artist slot should show — the parsed
        artist when ICY gave us both, else empty (the station name
        moves into the LIVE badge instead)."""
        return self.song_artist

    @property
    def display_cover_url(self) -> str:
        """Cover URL to load — per-track MB art if available, else the
        station logo. Empty when neither is known."""
        return self.art_url or self.station_logo_url


# ── Module state ────────────────────────────────────────────────────────────


_current: Optional[RadioState] = None
# Tracks the last ICY title we kicked an art lookup against so a slow
# reply can't paint over a newer track's cover.
_pending_lookup_title: str = ""
_initialised: bool = False


def current() -> Optional[RadioState]:
    """Return the present ``RadioState`` snapshot, or ``None`` when
    the active queue isn't an internet-radio stream. Surfaces call
    this on construction so a mid-session open picks up the current
    radio state without waiting for the next bus event."""
    return _current


def init() -> None:
    """One-time wiring of the bus subscriptions. Idempotent — calling
    twice is a no-op. The app's main() invokes this once before any
    surface that consumes ``radio_state_changed`` exists, so initial
    radio events from a restored session don't land into the void."""
    global _initialised
    if _initialised:
        return
    _initialised = True

    from jellytoast.player_state import PlayerBus

    bus = PlayerBus.get()
    bus.queue_context_changed.connect(_on_queue_context_changed)
    bus.radio_title_changed.connect(_on_icy_title)
    # Live-playback signals drive the LIVE indicator. A restored
    # queue installs a context but never fires playback_started until
    # the user hits play; pause + resume flip the state mid-session.
    bus.playback_started.connect(lambda _np: _set_playback_state("playing"))
    bus.playback_resumed.connect(lambda: _set_playback_state("playing"))
    bus.playback_paused.connect(lambda: _set_playback_state("paused"))
    bus.playback_stopped.connect(lambda: _set_playback_state("stopped"))
    # Restored-session state lands paused (mpv hasn't loaded the URL
    # yet, the user has to press play). Treat it as ``paused`` rather
    # than ``stopped`` so the surface keeps the title / cover visible
    # but the LIVE badge stays off until they actually start it.
    bus.playback_restored.connect(lambda _np: _set_playback_state("paused"))


# ── Bus slots ──────────────────────────────────────────────────────────────


def _set_playback_state(new_state: str) -> None:
    """Patch the current ``RadioState.playback_state`` and emit if it
    actually changed. No-ops when not in radio mode — non-radio
    playback signals don't generate radio_state_changed events."""
    global _current
    if _current is None:
        return
    if _current.playback_state == new_state:
        return
    from jellytoast.player_state import PlayerBus

    _current = replace(_current, playback_state=new_state)
    PlayerBus.get().radio_state_changed.emit(_current)


def _on_queue_context_changed(ctx) -> None:
    global _current, _pending_lookup_title
    from jellytoast.player_state import PlayerBus, QueueKind

    is_radio = bool(ctx) and getattr(ctx, "kind", None) == QueueKind.INTERNET_RADIO
    if not is_radio:
        if _current is not None:
            _current = None
            _pending_lookup_title = ""
            PlayerBus.get().radio_state_changed.emit(None)
        return
    _current = RadioState(
        station_name=str(getattr(ctx, "source_label", "") or ""),
        station_logo_url=str(getattr(ctx, "source_icon", "") or ""),
    )
    _pending_lookup_title = ""
    PlayerBus.get().radio_state_changed.emit(_current)


def _on_icy_title(raw: str) -> None:
    global _current, _pending_lookup_title
    from jellytoast.async_io import run_async
    from jellytoast.player_state import PlayerBus
    from jellytoast.radio_art import lookup_art_url, parse_icy_title

    if _current is None:
        # Title changed signal but we're not in radio mode — ignore.
        return
    clean = (raw or "").strip()
    if not clean:
        return

    artist, song = parse_icy_title(clean)
    # First emit immediately so the title + subtitle update without
    # waiting for the lookup. The art URL is left at whatever was
    # there (often the station logo); the second emit, post-lookup,
    # patches in the per-track art.
    _current = replace(
        _current,
        icy_title=clean,
        song_title=song,
        song_artist=artist,
        # Reset the per-track art so the surface falls back to the
        # logo until the new lookup lands. Otherwise a stale cover
        # from the previous track would linger until MB resolves.
        art_url="",
    )
    PlayerBus.get().radio_state_changed.emit(_current)

    if not artist or not song:
        # Title shape we can't search (no artist/song split) — leave
        # the cover at the station logo.
        return

    _pending_lookup_title = clean
    title_at_request = clean

    def _on_result(url):
        global _current
        # Stale-reply guard — a newer ICY title arrived while this
        # lookup was in flight. Drop the result.
        if _pending_lookup_title != title_at_request:
            return
        if _current is None:
            return
        if not url:
            return
        _current = replace(_current, art_url=str(url))
        PlayerBus.get().radio_state_changed.emit(_current)

    run_async(lookup_art_url, artist, song, on_result=_on_result, on_error=lambda _e: None)


# ── Test hook ──────────────────────────────────────────────────────────────


def _reset_for_tests() -> None:
    """Reset module-level state. Used by the test suite to start each
    case from a clean slate; not for production callers."""
    global _current, _pending_lookup_title, _initialised
    _current = None
    _pending_lookup_title = ""
    _initialised = False
