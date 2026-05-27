"""
Player state: NowPlaying dataclass + Qt signal bus + repeat/shuffle modes.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, List, Dict, Any
from PySide6.QtCore import QObject, Signal


class RepeatMode(str, Enum):
    OFF = "off"
    ALL = "all"
    ONE = "one"


class QueueKind(str, Enum):
    """Source of the active queue. Drives the right-pane rendering on the
    now-playing page (album track listing vs flat queue view), distinguishes
    library-shuffle from album-shuffle, and stamps MPRIS metadata.

    Modeled after Music Assistant's `PlayerQueue` envelope and Strawberry's
    `Playlist::special_type_`. Adding a new kind: pick a label that survives
    when the user serializes their session, and update the queue page
    pane-picker that switches on this enum.
    """

    ALBUM = "album"
    PLAYLIST = "playlist"
    ARTIST = "artist"
    SHUFFLE = "shuffle"  # library-wide random
    SEARCH = "search"
    MANUAL = "manual"  # user-built ad-hoc queue
    INSTANT_MIX = "instant_mix"  # Jellyfin-radio-style auto-extension
    # Live HTTP/Icecast/HLS stream. Single-item queue (the station),
    # no seek bar (replaced by elapsed + LIVE pip in the now-playing
    # treatment), Next stops the queue rather than advancing. The
    # station's ICY title — when present — drives the displayed track
    # title via PlayerBus.radio_title_changed.
    INTERNET_RADIO = "internet_radio"


@dataclass
class QueueContext:
    """Where the active queue came from. Immutable for the lifetime of one
    queue install — replacing the queue (e.g. clicking a different album)
    creates a new QueueContext, never mutates the existing one.

    ``seed_kind`` + ``radio_played_ids`` are only meaningful when the
    queue was spawned by a seeded-radio entry point (right-click "Start
    radio from…", "More like this", etc.). For every other queue kind
    they stay at their defaults and callers ignore them.
    """

    kind: QueueKind = QueueKind.MANUAL
    source_id: str = ""  # AlbumId / PlaylistId / etc., empty for shuffle/manual
    source_label: str = ""  # human-readable name for the right pane header
    source_icon: str = ""  # cover-art URL (album/playlist art), or empty
    # Seeded-radio metadata. ``seed_kind`` is one of "similar",
    # "instant_mix", "genre", or None for non-radio queues — the
    # RadioFeeder reads this to know which provider call to use when
    # the tail nears empty. ``radio_played_ids`` accumulates every item
    # id played in this radio session so the refill loop can dedupe,
    # avoiding the Supersonic-known artist-cluster repeat issue.
    seed_kind: Optional[str] = None
    radio_played_ids: List[str] = field(default_factory=list)


@dataclass
class Queue:
    """Per Strawberry's `Playlist`: keep `original_items` immutable for the
    lifetime of this queue and mutate only `play_order` (a permutation of
    indices into `original_items`). That lets the queue page render the
    "album order" right pane without losing it when shuffle is on, since
    the displayed list doesn't move — only the play head's walk through it.

    `manual_overlay` is reserved for Strawberry-style "Play next" inserts
    that should ride on top of any context (album/playlist) without
    converting it to a manual queue. Currently unused — the queue UI will
    wire it up; `next()` will drain this list FIFO before stepping
    `play_order`. Until then it stays empty.
    """

    context: QueueContext = field(default_factory=QueueContext)
    original_items: List[Dict[str, Any]] = field(default_factory=list)
    play_order: List[int] = field(default_factory=list)
    current_index: int = -1  # index INTO play_order, not original_items
    manual_overlay: List[Dict[str, Any]] = field(default_factory=list)
    # True once the queue has diverged from its source context (user
    # added a track, dragged a row, or removed an item). The
    # now-playing page's right-pane kicker shows "QUEUE" instead of
    # "ALBUM" / "PLAYLIST" / etc. when this is set, so the user knows
    # the list isn't a faithful copy of the source anymore. Reset to
    # False whenever ``play_now`` installs a fresh queue.
    is_modified: bool = False

    @property
    def is_empty(self) -> bool:
        return not self.original_items

    @property
    def length(self) -> int:
        return len(self.play_order)

    @property
    def current_item(self) -> Optional[Dict[str, Any]]:
        if 0 <= self.current_index < len(self.play_order):
            return self.original_items[self.play_order[self.current_index]]
        return None

    def play_ordered(self) -> List[Dict[str, Any]]:
        """Items in playback order — what MPRIS, the mini player, and the
        flat-queue right pane all want to render."""
        return [
            self.original_items[i] for i in self.play_order if 0 <= i < len(self.original_items)
        ]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "context": {
                "kind": self.context.kind.value,
                "source_id": self.context.source_id,
                "source_label": self.context.source_label,
                "source_icon": self.context.source_icon,
                "seed_kind": self.context.seed_kind,
                "radio_played_ids": list(self.context.radio_played_ids),
            },
            "original_items": self.original_items,
            "play_order": self.play_order,
            "current_index": self.current_index,
            "manual_overlay": self.manual_overlay,
            "is_modified": self.is_modified,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Queue":
        ctx_raw = data.get("context") or {}
        try:
            kind = QueueKind(ctx_raw.get("kind", "manual"))
        except ValueError:
            kind = QueueKind.MANUAL
        # Seeded-radio fields are additive — older session JSON without
        # them still loads via the defaults below.
        seed_kind = ctx_raw.get("seed_kind")
        if seed_kind == "":
            seed_kind = None
        ctx = QueueContext(
            kind=kind,
            source_id=ctx_raw.get("source_id", ""),
            source_label=ctx_raw.get("source_label", ""),
            source_icon=ctx_raw.get("source_icon", ""),
            seed_kind=seed_kind,
            radio_played_ids=list(ctx_raw.get("radio_played_ids") or []),
        )
        items = data.get("original_items") or []
        play_order = data.get("play_order") or list(range(len(items)))
        current = data.get("current_index", -1)
        if current >= len(play_order):
            current = -1
        return cls(
            context=ctx,
            original_items=items,
            play_order=play_order,
            current_index=current,
            manual_overlay=data.get("manual_overlay") or [],
            is_modified=bool(data.get("is_modified", False)),
        )


@dataclass
class NowPlaying:
    item_id: str = ""
    # Stable identity of the cover artwork — for audio this is the
    # AlbumId (so every track on an album shares one cache slot in
    # the now-playing surfaces), otherwise the item id itself. Empty
    # = consumers should fall back to item_id.
    image_id: str = ""
    title: str = ""
    subtitle: str = ""  # artist / series
    album: str = ""
    year: str = ""
    duration: int = 0  # ms
    position: int = 0  # ms
    is_paused: bool = False
    thumb_url: str = ""
    stream_url: str = ""
    item_type: str = ""  # Movie | Episode | Audio
    is_favorite: bool = False
    # True when stream_url points at a downloaded local blob (a file://
    # URI) rather than a server stream — set by QueueManager when it
    # prefers the offline copy. Lets surfaces show an offline indicator.
    is_local: bool = False
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_audio(self) -> bool:
        return self.item_type == "Audio"

    @property
    def is_video(self) -> bool:
        return self.item_type in ("Movie", "Episode")

    @property
    def position_ticks(self) -> int:
        return self.position * 10_000

    @property
    def duration_ticks(self) -> int:
        return self.duration * 10_000


class PlayerBus(QObject):
    """Single source of truth for cross-component playback events."""

    # ── Playback control (UI → backend) ─────────────────────────────────────
    play_requested = Signal(object)  # NowPlaying
    pause_toggled = Signal()
    stop_requested = Signal()
    seek_requested = Signal(int)  # ms (absolute)
    seek_relative = Signal(int)  # ms (delta)
    volume_changed = Signal(int)  # 0–100
    mute_toggled = Signal()

    # ── Queue control ────────────────────────────────────────────────────────
    # Install a new queue. `context` is a `QueueContext` from
    # `modules.player_state`; emitters that don't care can pass
    # `QueueContext()` (defaults to MANUAL) but the now-playing page
    # relies on real values to pick the right pane content (album track
    # listing vs flat queue), so prefer being explicit.
    queue_play_now = Signal(list, int, object)  # items, start_index, context
    queue_add_next = Signal(list)  # items
    queue_add_end = Signal(list)  # items
    # Drag-reorder in the now-playing page's right pane. Both indices
    # are play-order indices (what the user sees in the rendered list);
    # QueueManager translates to original_items mutations.
    queue_move_item = Signal(int, int)  # src_play_idx, dest_play_idx
    # Right-click → "Remove from queue" on a track row in the
    # now-playing page. Index is play-order.
    queue_remove_at = Signal(int)  # play_idx
    queue_clear = Signal()
    next_track = Signal()
    prev_track = Signal()
    # `queue_changed` emits the play-ordered items + index into them, so
    # MPRIS / mini player / now-playing chrome don't have to know about
    # the underlying `Queue` model.
    queue_changed = Signal(list, int)  # full_queue (play order), index
    # Fires when the queue's source changes (album → playlist → shuffle …).
    # The queue page subscribes to this to repaint the right-pane heading
    # without re-rendering the whole track list on every jump.
    queue_context_changed = Signal(object)  # QueueContext
    track_jumped = Signal(int)  # index in queue

    repeat_changed = Signal(str)
    shuffle_changed = Signal(bool)
    replaygain_changed = Signal(str)  # "no" | "track" | "album"
    # Fired by the Settings → Playback EQ surface (slider release,
    # preset pick, enabled toggle) so MpvController can rebuild the
    # `anequalizer` filter chain mid-track without rebuffering.
    # Payload is (enabled, bands) — bands is a list of 10 dB floats
    # in `modules.eq_presets.BAND_FREQUENCIES` order. When `enabled`
    # is False, listeners should clear the filter; the band list is
    # still carried (typically the user's last curve) so the UI can
    # preview it without re-emitting on re-enable.
    eq_changed = Signal(bool, list)
    # Fired by Settings → Playback → Bit-perfect mode whenever the user
    # toggles the master "no DSP, no resample, no attenuation" lock.
    # Payload is the new enabled state. Subscribers (volume slider,
    # normalization combo, EQ + crossfade sections, the now-playing
    # Lossless badge) re-evaluate their disabled / visible state. When
    # toggled True, the setter has already force-persisted dependents
    # (volume=100, replaygain="no", eq_enabled=False, crossfade_enabled
    # =False) and emitted the corresponding signals — listeners that
    # already react to volume_state / replaygain_changed / eq_changed
    # don't need to re-subscribe here.
    bit_perfect_changed = Signal(bool)
    # Fired by PlayerBackend when the *runtime* bit-perfect contract
    # transitions. Distinct from ``bit_perfect_changed`` (the user-facing
    # SETTING flag): the contract requires the setting on AND the
    # server isn't transcoding AND the source codec is itself lossless.
    # Playing an MP3 with bit-perfect checked emits ``False`` here even
    # though the setting is on. The volume-popup lock + the now-playing
    # "Bit Perfect" badge react to this signal so they only assert the
    # lock when there's an actual lossless contract to preserve.
    bit_perfect_active_changed = Signal(bool)
    # Fired by Settings → Playback → Bit-perfect mode → Exclusive output.
    # Payload is the new enabled state. PlayerBackend listens to push
    # the change to its live mpv handle so it takes effect on the next
    # track without a full app restart; the setting is already
    # persisted by the dialog's toggle handler.
    audio_exclusive_changed = Signal(bool)
    lyrics_font_size_changed = Signal(str)  # "small" | "default" | "large" | "largest"
    # Fired after the user picks a new accent / theme in Settings →
    # Display. By the time subscribers run, `modules.ui_helpers` and
    # `modules.icons` have already refreshed their module-level
    # constants (ACCENT, ACCENT_DEEP, ICON_ACCENT, GLOBAL_STYLE…) and
    # the QApplication has the new stylesheet + palette. Subscribers
    # should re-pull whichever ui_helpers / icons constant they need
    # and rebuild any stylesheets / icons / palettes they own. Theme
    # mode + font_scale are still restart-required (they bake into
    # too many surfaces); only accent changes flow live today.
    theme_changed = Signal()
    # Fired by Settings → Hotkeys whenever a binding is changed or
    # reset. The main window re-installs its QShortcuts in response so
    # a rebind takes effect immediately, no restart.
    hotkeys_changed = Signal()
    # Fired by MpvController when audio-bitrate updates during
    # playback. Carries (codec, kbps). Reflects what's actually
    # being decoded, so a Jellyfin-transcoded MP3 stream from a
    # FLAC source reports MP3 here — what the user is hearing,
    # not what the metadata claims.
    streaming_info_updated = Signal(str, int)
    # Fired by MpvController when an internet-radio stream's ICY
    # metadata (``metadata/by-key/icy-title``) changes. Carries the
    # raw title string — typically ``"Artist - Track"`` but station-
    # specific (some embed jingles, ad markers, or just the station
    # name during fillers). Subscribers: the now-playing surfaces
    # while ``QueueContext.kind == QueueKind.INTERNET_RADIO`` — they
    # render this in place of the static track title slot. Empty /
    # unchanged values are filtered out at the source; consumers
    # always get a non-empty changed string.
    radio_title_changed = Signal(str)
    # Unified radio presentation state. ``modules.radio_state`` owns the
    # parse + cover-lookup pipeline and fires this signal whenever any
    # piece of the user-visible state changes — entering/leaving radio
    # mode, ICY title arriving, MusicBrainz cover lookup landing. The
    # payload is a ``modules.radio_state.RadioState`` (or ``None`` when
    # leaving radio mode). Surfaces should subscribe to this instead of
    # the raw ``queue_context_changed`` / ``radio_title_changed`` pair
    # so all the radio screens render identically. See ``radio_state``
    # for the dataclass + ``current()`` snapshot accessor for surfaces
    # that construct mid-session.
    radio_state_changed = Signal(object)
    # Fired by ``QueueManager``'s seeded-radio feeder after a successful
    # refill, carrying the number of tracks just appended to the queue
    # (post-dedupe). Subscribers (future toast surface) can flash a
    # short "Radio extended by N tracks" affordance. Zero-count refills
    # don't fire — see ``radio_exhausted`` for the no-results case.
    radio_extended = Signal(int)
    # Fired by the seeded-radio feeder when both the seeded provider
    # call and its random-library fallback return zero tracks. The
    # queue continues playing what's already in it; this is a hint to
    # surface "radio reached the end" rather than crash or silently
    # stall on the next track-change event.
    radio_exhausted = Signal()
    # Fired when the main window's device-pixel-ratio changes — typically
    # because the user dragged it between monitors with different KDE
    # scales, or because KDE's global scale slider moved. By signal-fire
    # time, Qt has already updated `widget.devicePixelRatioF()` to the
    # new value. Subscribers should re-issue cover-load requests so the
    # resulting pixmaps are sized for the new physical target; the L1
    # cache is keyed by physical size and will naturally cache-miss on
    # the cross-DPR request, then re-derive from the L2 raw cache (the
    # decoded source, which is DPR-agnostic).
    dpr_changed = Signal()
    # Hint to MpvController: the "next" track has changed. Carries either
    # a NowPlaying for the next item (so mpv can append it to its playlist
    # for gapless handoff) or None to signal "no next — drop any pending
    # prefetch." Emitted whenever the current track changes, when shuffle
    # toggles, and when the queue is mutated near the head.
    queue_prefetch_request = Signal(object)

    # ── State updates (backend → UI) ────────────────────────────────────────
    position_updated = Signal(int)  # ms
    duration_set = Signal(int)  # ms
    playback_started = Signal(object)  # NowPlaying
    playback_paused = Signal()
    playback_resumed = Signal()
    playback_stopped = Signal()
    playback_ended = Signal()
    # Fired by the Crossfader (env-gated behind JT_CROSSFADE=1) on the
    # CROSSFADING-entry and SWAP-exit transitions of its state machine.
    # No payload for v1 — future UI grey-out hints subscribe here without
    # needing to know which handles are involved.
    crossfade_started = Signal()
    crossfade_ended = Signal()
    # Fired when the user toggles ``crossfade_enabled`` in Settings →
    # Playback. The crossfade engine itself doesn't need this (it reads
    # the setting per gapless-handoff tick), but the now-playing
    # streaming-info badge does — its "Crossfade" segment can't wait
    # for the next track to refresh.
    crossfade_changed = Signal(bool)
    # Fired when the user picks a new ``audio_quality`` in Settings →
    # Playback. The next track URL build reads the setting directly;
    # this signal exists so PlayerBackend can recompute the runtime
    # bit-perfect contract — flipping quality from "original" to a
    # transcoded tier breaks the contract even though the codec hasn't
    # changed yet.
    audio_quality_changed = Signal(str)
    # Fired once at app launch when a saved queue + saved position pair
    # restores: the UI shows the track + slider position as if paused,
    # but mpv hasn't loaded anything yet. The first play press reads
    # the carried NowPlaying.position and starts mpv at that offset.
    playback_restored = Signal(object)  # NowPlaying with .position set
    volume_state = Signal(int)
    mute_state = Signal(bool)

    # ── Cast ────────────────────────────────────────────────────────────────
    cast_started = Signal(str)
    cast_stopped = Signal()
    cast_devices_updated = Signal(list)

    # ── Snapcast (A24, Option B: control surface only) ─────────────────────
    # Kept separate from the cast_* family because Snapcast isn't a "push
    # URL" model — see docs/research/casting_snapcast.md §10. The popup
    # renders Snapcast rows differently (server header + group rows +
    # client rows w/ volume sliders) and consumers wire to discrete
    # signals so a Chromecast volume tick doesn't repaint the Snapcast
    # tree (and vice-versa).
    snapcast_servers_changed = Signal(list)  # list[dict] {host, port, hostname, version}
    snapcast_state_changed = Signal(dict)  # {groups, clients, streams, connected}
    snapcast_clients_changed = Signal(list)  # list[dict] (client snapshots)
    snapcast_groups_changed = Signal(list)  # list[dict] (group snapshots)
    snapcast_streams_changed = Signal(list)  # list[dict] (stream snapshots)
    snapcast_client_volume_changed = Signal(str, int, bool)  # client_id, pct, muted
    snapcast_group_changed = Signal(str)  # group_id
    snapcast_active_group_changed = Signal(str)  # group_id, "" = unpaired
    snapcast_connection_changed = Signal(bool)  # True = connected to a server
    snapcast_error = Signal(str)  # human-readable

    # ── Favorite ────────────────────────────────────────────────────────────
    favorite_toggled = Signal(str, bool)  # item_id, is_favorite

    # ── Sleep timer ─────────────────────────────────────────────────────────
    # Session-scoped countdown owned by the Player. `sleep_timer_started`
    # carries the initial seconds-remaining; `sleep_timer_fired` fires
    # before the pause/fade action so a UI surface can flash a final
    # "Sleeping…" toast. No persistence — a fresh launch starts cold.
    sleep_timer_started = Signal(int)
    sleep_timer_cancelled = Signal()
    sleep_timer_fired = Signal()
    # UI → Player request signals. The now-playing bar's sleep-timer
    # menu emits these; PlayerBackend wires them to start/cancel so the
    # bar doesn't need a direct Player reference. `(seconds, on_fire)`
    # mirrors `start_sleep_timer`'s arguments.
    sleep_timer_requested = Signal(int, str)
    sleep_timer_cancel_requested = Signal()

    # ── UI ──────────────────────────────────────────────────────────────────
    open_main_window = Signal()
    show_mini_player = Signal()
    hide_mini_player = Signal()
    navigate_to_item = Signal(dict)  # item dict
    show_now_playing = Signal()
    notify_track = Signal(object)  # NowPlaying

    # ── Offline / downloads ─────────────────────────────────────────────────
    # Emitted by modules.offline.manager as a download moves through its
    # lifecycle, for both leaf tracks and the user-requested root node.
    # state ∈ pending|downloading|complete|failed|removed; fraction is
    # 0.0–1.0 (best-effort — 0.0 when the server sends no Content-Length).
    # Safe to emit from a pool worker: a queued connection marshals it
    # onto the GUI thread.
    download_progress = Signal(str, str, float)  # item_id, state, fraction
    # Aggregate stats for the download queue, emitted at ~1 Hz by
    # ``modules.offline.manager`` while at least one job is active or
    # queued. Payload: (active, total_session, speed_bps, eta_seconds).
    # ``active`` is ``len(_active)``; ``total_session`` is the running
    # counter of jobs dispatched this drain cycle (resets to 0 on the
    # drain edge). ``speed_bps`` is the sum of per-job byte-rates over
    # a rolling ~3-second window. ``eta_seconds`` carries the longest
    # remaining-job projection; ``-1.0`` means "calculating / unknown"
    # (no Content-Length, no samples yet, or > 12 h) and ``0.0`` means
    # "idle" — a final ``(0, 0, 0.0, 0.0)`` lands on every drain edge
    # so subscribers can hide their UI immediately. Emitted on the GUI
    # thread from a ``QTimer``.
    download_queue_stats = Signal(int, int, float, float)
    # Queue-level pause / resume. Fired by ``modules.offline.manager`` when
    # the user-driven pause flag flips. Lets the downloads screen swap its
    # pause button for resume (and vice versa) without polling.
    download_queue_paused = Signal()
    download_queue_resumed = Signal()
    # Wi-Fi-only gate flipped. Fired by ``modules.offline.manager`` when
    # the user toggles "Only download on Wi-Fi" so the Downloads screen
    # checkbox stays in sync with programmatic / future-auto-detect
    # flips. Payload is the new flag value.
    downloads_wifi_only_changed = Signal(bool)
    # Server reachability transitions. Fired by
    # ``modules.offline.connectivity`` when an N-consecutive-failure
    # threshold flips the state; not fired per call. Subscribers:
    # ScrobbleManager (flush queue on reconnect), future offline-mode
    # banner / library filter.
    connectivity_changed = Signal(bool)  # True = reachable
    # Offline mode flipped — either by the user toggling it directly or
    # by auto-offline reacting to ``connectivity_changed``. Views read
    # from downloads.db only when this is True.
    offline_mode_changed = Signal(bool)  # True = offline mode active
    # Active host switched to an alternate URL (Tailscale ↔ LAN). The
    # payload is the new host's label so a toast / status chip can say
    # which hostname is in use. Emitted by the connectivity tracker
    # after a successful failover probe; the connectivity-changed
    # signal is *not* fired when an alternate absorbs the failure.
    host_switched = Signal(str)  # label of the now-active host
    # Persistent auth-failure detected: N consecutive provider calls
    # came back with a definitive auth-reject (HTTP 401/403 for
    # Jellyfin, Subsonic error 40 for Subsonic). Fired by the
    # connectivity tracker after the threshold trips. Subscriber:
    # MainWindow drops to LoginView so the user can re-enter creds
    # instead of seeing a silent "No albums yet" empty state with no
    # path to recovery. Reset by any successful auth-bearing call.
    auth_failed = Signal()
    # The user clicked "Refresh album art" in Settings (or any code
    # path that wipes both the disk cover cache and the in-memory
    # pixmap/raw caches). Visible cover-rendering surfaces should
    # respond by re-issuing their cover loads — without this, tiles
    # already painted in the current session keep showing stale art
    # until the user navigates away and back or restarts.
    image_cache_cleared = Signal()

    # ── Visualizer ──────────────────────────────────────────────────────────
    # Emitted by ``modules.visualizer.VisualizerEngine`` once per FFT
    # frame (throttled to ~30 Hz) when the ``JT_VISUALIZER=1`` env flag
    # is set. Payload is a list of floats in [0.0, 1.0] — one entry per
    # log-spaced mel band (default 32 bands across 50 Hz–16 kHz). Dormant
    # otherwise; visualizer-render widgets land in a follow-up branch.
    visualizer_bands_changed = Signal(list)

    # ── Scrobble ────────────────────────────────────────────────────────────
    # Fired when the server-side scrobble flags change (e.g. after a
    # fresh Subsonic login re-runs ``modules.scrobble.navidrome_detect``
    # and persists new values into ``server/scrobbles_lastfm`` /
    # ``server/scrobbles_listenbrainz``). Subscribers re-read both
    # ``settings.server_scrobbles_lastfm`` and
    # ``settings.server_scrobbles_listenbrainz`` and refresh their UI —
    # primarily the now-playing bar / page scrobble badge. No payload:
    # the settings are the source of truth, this is just a refresh ping.
    scrobble_status_changed = Signal()

    _instance: Optional["PlayerBus"] = None

    @classmethod
    def get(cls) -> "PlayerBus":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    # Cached runtime state, mirrored from the latest
    # ``bit_perfect_active_changed`` emit so a listener constructed
    # after the most recent transition can read the current value
    # synchronously without waiting for the next track-codec arrival.
    # Owned by PlayerBackend — other code should only read.
    bit_perfect_active: bool = False
    # Cached cast state, mirrored from ``cast_started`` / ``cast_stopped``.
    # Lets call-sites that aren't connected to those signals (the
    # Settings dialog's bit-perfect toggle, for example) check
    # synchronously whether a cast session is live — so they can skip
    # actions that would clobber the cast device's volume / state.
    # Owned by PlayerBackend.
    cast_active: bool = False


# ── Now-playing global state ────────────────────────────────────────────────

_now_playing = NowPlaying()


def get_now_playing() -> NowPlaying:
    return _now_playing


def set_now_playing(item: NowPlaying):
    global _now_playing
    _now_playing = item
