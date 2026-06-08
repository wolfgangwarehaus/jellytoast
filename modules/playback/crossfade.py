"""Two-handle crossfade plumbing.

Driven by ``MpvController``; the active handle stays owned by
``player_backend.py`` and the Crossfader owns the dormant sibling.
Equal-power ramp via a 50ms ``QTimer`` — the outgoing handle follows
``cos(progress · π/2)`` and the incoming handle follows
``sin(progress · π/2)``, which keeps the *summed power* of the two
streams constant across the fade. That matters because cross-album
transitions are exactly the "uncorrelated content" case where a
linear ramp dips ~3 dB at the midpoint (Sound on Sound: linear vs
constant-power), and the audible result is a noticeable mid-fade
dropout. Same-album adjacent transitions are already routed through
the gapless prefetch path before they reach this code (see
``_should_skip_for_album_continuity`` and
``docs/research/crossfade.md`` §3), so the curve we pick here only
ever applies to genuinely uncorrelated audio.

Opt-in via the ``crossfade_enabled`` setting (Settings → Playback);
the controller skips building the Crossfader entirely while that is
off.

State machine — see ``docs/research/crossfade.md`` §3:

    IDLE → ARMING → CROSSFADING → SWAP → IDLE

The trigger is the current handle's ``time-pos`` crossing
``duration - crossfade_duration_ms``. Smart-album-continuity (adjacent
tracks share AlbumId/albumId) short-circuits the trigger so the
existing gapless prefetch path runs unmolested.
"""

from __future__ import annotations

import logging
import math
from enum import Enum
from typing import Any, Callable, Dict, Optional

from PySide6.QtCore import QObject, QTimer, Signal, Slot

logger = logging.getLogger(__name__)

from modules.player_state import NowPlaying, PlayerBus

# 50ms = 20Hz: matches the sleep-timer fade cadence in player_backend.py
# (``_SLEEP_FADE_TICK_MS``). Same audible smoothness; same constant lives
# in two places intentionally so a tune to one ramp doesn't silently
# retune the other.
RAMP_TICK_MS = 50


class CrossfadeState(str, Enum):
    IDLE = "idle"
    ARMING = "arming"
    CROSSFADING = "crossfading"
    SWAP = "swap"


def _album_id(item: Optional[Dict[str, Any]]) -> str:
    """Tolerate both Jellyfin (``AlbumId``) and Subsonic (``albumId``)
    casings. Returns the raw id string, or empty when neither field
    carries a value. Empty-vs-empty must not register as a match — the
    callers gate on truthiness."""
    if not item:
        return ""
    return str(item.get("AlbumId") or item.get("albumId") or "")


class Crossfader(QObject):
    """Owns the dormant mpv sibling + the ping-pong state machine.

    Wired by ``MpvController`` when ``settings.crossfade_enabled`` is
    true. The controller drives ``on_position()`` from its existing
    ``time-pos`` observer (already marshalled onto the Qt thread); the
    Crossfader does the rest.

    The "current" handle stays owned by ``MpvController`` — the
    Crossfader holds a reference but never tears it down. On a SWAP, the
    Crossfader hands its newly-active handle back to the controller via
    the ``swap_handles`` callback so the rest of the app (MPRIS,
    transport bar, EQ chain) keeps pointing at "the one playing right
    now."
    """

    state_changed = Signal(str)  # emits CrossfadeState value

    def __init__(
        self,
        *,
        bus: PlayerBus,
        settings,
        make_handle: Callable[..., Any],
        get_current_handle: Callable[[], Any],
        get_current_item: Callable[[], Optional[Dict[str, Any]]],
        is_casting: Callable[[], bool],
        swap_handles: Callable[[Any], None],
        parent: Optional[QObject] = None,
    ):
        super().__init__(parent)
        self._bus = bus
        self._settings = settings
        self._make_handle = make_handle
        self._get_current_handle = get_current_handle
        self._get_current_item = get_current_item
        self._is_casting = is_casting
        self._swap_handles = swap_handles

        self._state = CrossfadeState.IDLE
        # Sibling handle — built lazily on first arm so the construction
        # cost (mpv spinup) doesn't hit at app launch when crossfade may
        # never actually fire (short tracks, same-album streak, etc.).
        self._sibling: Optional[Any] = None
        # The next-track NowPlaying as last announced by QueueManager via
        # ``queue_prefetch_request``. Snapshotted at trigger time so the
        # user toggling shuffle mid-fade doesn't redirect mid-ramp.
        self._next_np: Optional[NowPlaying] = None
        # Snapshot of the active handle at arm time. The user's "volume"
        # is a target the outgoing handle ramps away from; the sibling
        # ramps up to it.
        self._target_volume: int = 0
        # Tick bookkeeping. We track *progress* (ticks_done / ticks_total)
        # rather than wall-clock so pause-during-fade can resume exactly
        # where it left off without re-reading the wall clock.
        self._ticks_total: int = 0
        self._ticks_done: int = 0
        # Snapshotted at arm time so live setting changes don't disturb a
        # fade in progress (the research doc's "live-apply on next
        # transition" rule §5).
        self._duration_ms: int = 0

        self._tick_timer = QTimer(self)
        self._tick_timer.setInterval(RAMP_TICK_MS)
        self._tick_timer.timeout.connect(self._on_tick)

        # Track the most-recently-known next track. QueueManager fires
        # this every time the queue head moves or shuffle toggles, so it
        # stays fresh without polling.
        self._bus.queue_prefetch_request.connect(self._on_prefetch_request)
        # Active queue kind — read by ``on_position`` to skip crossfade
        # on internet-radio queues. Radio is a single-item live stream
        # with no meaningful "next track" (Next on radio stops the
        # queue rather than advancing), so the fade window calculation
        # would be operating on the live stream's stale duration field
        # and producing undefined behaviour. QueueManager emits this
        # whenever the queue context changes; cache the kind so the
        # per-position-tick gate is a cheap field read.
        from modules.player_state import QueueKind
        self._QueueKind = QueueKind  # alias kept for the gate below
        self._queue_kind: QueueKind = QueueKind.MANUAL
        self._bus.queue_context_changed.connect(self._on_queue_context_changed)

    # ── Public surface (called by MpvController) ────────────────────────────

    @property
    def state(self) -> CrossfadeState:
        return self._state

    @property
    def sibling(self) -> Optional[Any]:
        """Exposed for tests + the controller's shutdown path. None until
        the first arm builds it."""
        return self._sibling

    @property
    def next_np(self) -> Optional[NowPlaying]:
        """Last-known next-track snapshot (from ``queue_prefetch_request``).
        Read by ``MpvController._swap_active_handle`` so the post-swap
        handoff path can match item_id, not just URL."""
        return self._next_np

    def set_target_volume(self, vol: int) -> None:
        """Update the fade's target volume mid-flight — for a volume-slider
        drag DURING a crossfade. Without this the next 50ms ramp tick
        overwrites the new level (it rescales the snapshotted _target_volume)
        and ``_enter_swap`` clamps the incoming handle to the stale target,
        so the user's adjustment is silently lost until the next track. No-op
        unless actively fading — outside a fade the next ``_arm`` snapshots
        ``settings.volume`` fresh."""
        if self._state == CrossfadeState.CROSSFADING:
            self._target_volume = int(max(0, min(100, vol)))

    def on_position(self, pos_ms: int, duration_ms: int) -> None:
        """Driven by the controller's ``time-pos`` observer. The trigger
        decision is here, in one place, so the state machine entry
        criteria live alongside the state machine itself.

        Cheap when nothing's actionable: each tick the controller might
        as well not have called us. Real work only when crossing the
        threshold."""
        if self._state != CrossfadeState.IDLE:
            return
        if duration_ms <= 0:
            return
        if self._is_casting():
            return
        if not self._settings.crossfade_enabled:
            return
        if self._queue_kind == self._QueueKind.INTERNET_RADIO:
            # Radio queues are single-item live streams; the "next
            # track" slot is meaningless and the reported duration is
            # the live-stream-so-far, not a fade target. Let the queue
            # end naturally instead.
            return
        dur_setting = int(self._settings.crossfade_duration_ms)
        # Edge case from the research doc §7: when the current track is
        # shorter than the configured fade window there's nothing
        # meaningful to overlap. Let normal end-of-file run.
        if duration_ms < dur_setting * 2:
            return
        threshold = duration_ms - dur_setting
        if pos_ms < threshold:
            return
        next_np = self._next_np
        if next_np is None or not next_np.stream_url:
            return
        if self._should_skip_for_album_continuity(next_np):
            return
        self._arm(dur_setting, next_np)

    def complete_now(self) -> None:
        """The outgoing track hit its real EOF mid-fade — finish the swap
        NOW. The sibling is already playing the next track; jump it to
        target volume and hand it over via the normal swap, instead of
        letting the controller's EOF→advance→play() path abort the fade and
        restart the next track on the faded-DOWN outgoing handle (which
        leaves it near-silent). No-op unless a fade is in progress.

        The crossfade window ends at ``duration - crossfade_duration`` +
        the ramp length = the track's natural EOF, so swap-vs-EOF is a
        dead heat by design; routing EOF through here makes both orderings
        resolve to the same clean swap."""
        if self._state != CrossfadeState.CROSSFADING:
            return
        self._tick_timer.stop()
        if self._sibling is not None:
            _safe_set(self._sibling, "volume", self._target_volume)
        self._enter_swap()

    def pause(self) -> None:
        """Freeze the ramp; both handles pause. Resume via ``resume()``
        picks up at the same tick progress — wall-clock time off the
        ramp is irrelevant."""
        if self._state != CrossfadeState.CROSSFADING:
            return
        self._tick_timer.stop()
        cur = self._get_current_handle()
        if cur is not None:
            _safe_set(cur, "pause", True)
        if self._sibling is not None:
            _safe_set(self._sibling, "pause", True)

    def resume(self) -> None:
        if self._state != CrossfadeState.CROSSFADING:
            return
        cur = self._get_current_handle()
        if cur is not None:
            _safe_set(cur, "pause", False)
        if self._sibling is not None:
            _safe_set(self._sibling, "pause", False)
        self._tick_timer.start()

    def abort(self) -> None:
        """Hard cancel — used by the controller on stop / seek / a fresh
        play() that bypasses our state machine. Sibling silences, ramp
        timer stops, state returns to IDLE. The active handle is left
        alone (caller owns it)."""
        if self._state == CrossfadeState.IDLE:
            return
        self._tick_timer.stop()
        if self._sibling is not None:
            _safe_set(self._sibling, "volume", 0)
            _safe_stop(self._sibling)
        self._set_state(CrossfadeState.IDLE)

    def shutdown(self) -> None:
        """Tear down the sibling handle on application exit."""
        self._tick_timer.stop()
        if self._sibling is not None:
            try:
                self._sibling.terminate()
            except Exception:
                pass
            self._sibling = None

    # ── Internals ───────────────────────────────────────────────────────────

    @Slot(object)
    def _on_prefetch_request(self, np: Optional[NowPlaying]) -> None:
        # Stash the latest "next" so on_position has something to load
        # when the threshold trips. None is a valid value (queue tail,
        # shuffle disabled, end of queue with repeat off).
        self._next_np = np if (np is not None and getattr(np, "stream_url", "")) else None

    @Slot(object)
    def _on_queue_context_changed(self, ctx) -> None:
        # Cheap field read for the per-tick gate in on_position. ``ctx``
        # is a QueueContext; guard the attribute access so a malformed
        # payload doesn't break the state machine.
        try:
            self._queue_kind = ctx.kind
        except AttributeError:
            self._queue_kind = self._QueueKind.MANUAL

    def _should_skip_for_album_continuity(self, next_np: NowPlaying) -> bool:
        if not self._settings.crossfade_smart_album_continuity:
            return False
        current = self._get_current_item()
        cur_album = _album_id(current)
        next_album = _album_id(getattr(next_np, "raw", None))
        if not cur_album or not next_album:
            return False
        return cur_album == next_album

    def _arm(self, duration_ms: int, next_np: NowPlaying) -> None:
        cur = self._get_current_handle()
        if cur is None:
            return
        try:
            vol = cur["volume"]
            # Treat 0 as a REAL target (the handle is muted — jellytoast
            # models mute as volume=0 — or the user set volume to 0). Only
            # fall back to the persisted volume when mpv hasn't reported
            # one yet (None). ``vol or settings.volume`` would ramp a muted
            # handle up to full volume mid-fade and blare silenced audio.
            self._target_volume = int(vol if vol is not None else self._settings.volume)
        except Exception:
            self._target_volume = int(self._settings.volume)

        if self._sibling is None:
            try:
                self._sibling = self._make_handle(volume=0)
            except Exception as e:
                logger.warning("sibling handle build failed: %s", e)
                return
        else:
            _safe_set(self._sibling, "volume", 0)

        try:
            self._sibling.play(next_np.stream_url)
            _safe_set(self._sibling, "pause", False)
        except Exception as e:
            logger.warning("sibling.play failed: %s", e)
            return

        self._duration_ms = duration_ms
        self._ticks_total = max(1, duration_ms // RAMP_TICK_MS)
        self._ticks_done = 0
        self._set_state(CrossfadeState.ARMING)
        # v1 simplification: we don't gate on a sibling "ready-to-play"
        # property — the linear ramp starts from volume 0 and the
        # sibling silently buffers under the first few ticks. The
        # research doc §7 calls out a slow-link race; addressed in v2
        # once we have a real-world feel for the buffering window.
        self._enter_crossfading()

    def _enter_crossfading(self) -> None:
        self._set_state(CrossfadeState.CROSSFADING)
        self._bus.crossfade_started.emit()
        self._tick_timer.start()

    def _on_tick(self) -> None:
        if self._state != CrossfadeState.CROSSFADING:
            self._tick_timer.stop()
            return
        self._ticks_done += 1
        progress = min(1.0, self._ticks_done / self._ticks_total)
        out_gain, in_gain = _equal_power_gains(progress)
        out_vol = int(round(self._target_volume * out_gain))
        in_vol = int(round(self._target_volume * in_gain))
        cur = self._get_current_handle()
        if cur is not None:
            _safe_set(cur, "volume", out_vol)
        if self._sibling is not None:
            _safe_set(self._sibling, "volume", in_vol)
        if self._ticks_done >= self._ticks_total:
            self._enter_swap()

    def _enter_swap(self) -> None:
        self._tick_timer.stop()
        self._set_state(CrossfadeState.SWAP)
        # Hand the sibling (now playing, at target volume) up to the
        # controller as the new "current." Old current becomes our
        # dormant sibling for the next fade.
        old_current = self._get_current_handle()
        new_current = self._sibling
        # Ensure the freshly-current handle is at exactly target volume —
        # rounding in the ramp may have left it a step short.
        if new_current is not None:
            _safe_set(new_current, "volume", self._target_volume)
        if old_current is not None:
            _safe_set(old_current, "volume", 0)
            _safe_stop(old_current)
        # Old current is now the dormant sibling. Mute + stop are
        # enough — we don't terminate so the next fade reuses it.
        self._sibling = old_current
        try:
            self._swap_handles(new_current)
        except Exception as e:
            logger.warning("swap_handles callback raised: %s", e)
        self._bus.crossfade_ended.emit()
        self._set_state(CrossfadeState.IDLE)

    def _set_state(self, state: CrossfadeState) -> None:
        if self._state == state:
            return
        self._state = state
        self.state_changed.emit(state.value)


def _equal_power_gains(progress: float) -> tuple[float, float]:
    """Equal-power crossfade gains for a fade ``progress`` ∈ [0, 1].

    Returns ``(outgoing_gain, incoming_gain)``. Both gains are in
    [0, 1]; their *summed power* (``out² + in²``) equals 1 across the
    whole fade, which is what keeps the perceived loudness flat for
    uncorrelated content. Endpoints are pinned exactly to ``(1, 0)``
    and ``(0, 1)`` — ``math.cos(math.pi / 2)`` returns ~6e-17 (π is a
    float approximation), and we want the post-swap volume clamp in
    ``_enter_swap`` to be a true no-op on the last tick rather than
    masking a rounding artefact.
    """
    p = max(0.0, min(1.0, progress))
    if p == 0.0:
        return (1.0, 0.0)
    if p == 1.0:
        return (0.0, 1.0)
    angle = p * (math.pi / 2.0)
    return (math.cos(angle), math.sin(angle))


def _safe_set(handle: Any, key: str, value: Any) -> None:
    """mpv handle property writes go through ``handle[key] = value``. The
    real handle raises ``mpv.ShutdownError`` / ``mpv.PropertyError`` on
    bad timings (shutdown race, property not yet known). Swallow — the
    crossfade path is best-effort by design."""
    try:
        handle[key] = value
    except Exception:
        pass


def _safe_stop(handle: Any) -> None:
    try:
        handle.stop()
    except Exception:
        pass
