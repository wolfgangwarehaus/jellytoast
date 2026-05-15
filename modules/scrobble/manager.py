"""ScrobbleManager — wires PlayerBus to ListenBrainz + Last.fm.

Lifecycle of a track:

1. ``playback_started(np)`` snapshots the metadata and resets the
   per-track elapsed counter. Now-playing pings fan out immediately to
   each enabled service (transient — not queued on failure).
2. ``position_updated(ms)`` accumulates *forward* position deltas into
   ``elapsed_ms`` (seeks are ignored because their delta exceeds the
   per-tick cap). Once ``elapsed_ms`` ≥ the eligibility threshold the
   track is flagged ``eligible``.
3. On ``playback_stopped`` / ``playback_ended`` / a ``playback_started``
   for a different track / a ``track_jumped``: if the *outgoing* track
   was eligible and hadn't been scrobbled yet, fan out scrobble writes.
   Failures land in ``modules/scrobble/queue.py`` for later flush.

Eligibility rule (shared by both services):

> A track is scrobbled when it is **longer than 30 s** and has been
> played for **≥ 50 % of its length, or ≥ 4 minutes, whichever comes
> first**.

Network calls all run on the shared thread pool via ``run_async`` so
the GUI thread never stalls. The manager itself is a ``QObject``
constructed once at startup; ``modules.scrobble.get_scrobble_manager``
is the public accessor.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from PySide6.QtCore import QObject, Slot

from modules.async_io import run_async
from modules.player_state import NowPlaying, PlayerBus
from modules.settings import get_settings

from . import lastfm, listenbrainz, queue as scrobble_queue


# Per-tick cap for elapsed bookkeeping. position_updated normally fires
# every 200-500ms during playback, so a real forward delta is small. A
# user seek (or a paused-then-resumed jump) shows up as a multi-second
# delta — skip it so a forward seek can't satisfy the eligibility rule
# without actually listening.
_MAX_TICK_DELTA_MS = 5_000

# Floor on track length: ListenBrainz / Last.fm both ignore tracks
# shorter than 30s.
_MIN_TRACK_DURATION_MS = 30_000

# Cap on elapsed needed to scrobble: 4 minutes (the "or 4min" half of
# the rule). Below this the 50%-of-length half wins.
_MAX_ELIGIBILITY_MS = 4 * 60_000


@dataclass
class _TrackState:
    np: NowPlaying
    started_at_wall: int = 0     # UNIX seconds, UTC, at playback_started
    duration_ms: int = 0
    elapsed_ms: int = 0
    last_position_ms: int = 0
    eligible: bool = False
    scrobbled: bool = False
    now_playing_sent: bool = False
    # Cached service payloads — built once at start so the scrobble
    # path doesn't have to re-derive metadata if the NowPlaying mutates
    # mid-playback (e.g. lyrics fill in).
    track_metadata_lb: Dict[str, Any] = field(default_factory=dict)
    artist: str = ""
    track_name: str = ""
    album: str = ""
    mbid: str = ""

    def threshold_ms(self) -> int:
        """The elapsed-ms target that flips ``eligible`` to True. Half
        the duration, capped at four minutes."""
        return min(self.duration_ms // 2, _MAX_ELIGIBILITY_MS)


class ScrobbleManager(QObject):
    """Singleton glue between PlayerBus and the per-service clients.

    Construct exactly once at startup; the package-level
    ``get_scrobble_manager()`` enforces that. Wires up to the global
    ``PlayerBus`` in ``__init__``."""

    def __init__(self):
        super().__init__()
        self._bus = PlayerBus.get()
        self._settings = get_settings()
        self._current: Optional[_TrackState] = None

        self._bus.playback_started.connect(self._on_playback_started)
        self._bus.duration_set.connect(self._on_duration_set)
        self._bus.position_updated.connect(self._on_position_updated)
        self._bus.playback_stopped.connect(self._on_playback_stopped)
        self._bus.playback_ended.connect(self._on_playback_ended)
        # Reconnect handler — flush the offline queue once the server
        # comes back. Replaces the per-success opportunistic flushes
        # the submit callbacks used to do; signal-driven is cleaner and
        # only fires on the actual reachable<->unreachable edge.
        self._bus.connectivity_changed.connect(self._on_connectivity_changed)

    # ── PlayerBus slots ─────────────────────────────────────────────────────

    @Slot(object)
    def _on_playback_started(self, np: NowPlaying):
        # Different track than the one we were tracking? Submit the old
        # one if it crossed the threshold, then start fresh.
        if self._current is not None and self._current.np.item_id != np.item_id:
            self._maybe_scrobble_current()
        # Restored playback (saved-position resume) re-fires
        # playback_started for the same track — don't reset the counter
        # in that case; the user is continuing, not starting over.
        if self._current is not None and self._current.np.item_id == np.item_id:
            return
        # Only audio scrobbles. Movies / Episodes never enter the queue.
        if not np.is_audio:
            self._current = None
            return
        artist, track_name, album = _split_metadata(np)
        if not artist or not track_name:
            # Without artist+track we can't scrobble. Don't even build
            # state — silently skip.
            self._current = None
            return
        mbid = _extract_mbid(np)
        state = _TrackState(
            np=np,
            started_at_wall=int(time.time()),
            duration_ms=int(np.duration or 0),
            artist=artist,
            track_name=track_name,
            album=album,
            mbid=mbid,
            track_metadata_lb=listenbrainz.build_track_metadata(
                artist_name=artist, track_name=track_name,
                release_name=album, duration_ms=int(np.duration or 0),
                recording_mbid=mbid,
            ),
        )
        self._current = state
        self._send_now_playing(state)

    @Slot(int)
    def _on_duration_set(self, duration_ms: int):
        # mpv may emit duration after playback_started, especially for
        # streamed content — make sure the threshold reflects the real
        # length once we know it.
        if self._current is None:
            return
        if duration_ms > 0:
            self._current.duration_ms = int(duration_ms)
            # Refresh the cached LB payload so submission carries the
            # corrected duration.
            self._current.track_metadata_lb = listenbrainz.build_track_metadata(
                artist_name=self._current.artist,
                track_name=self._current.track_name,
                release_name=self._current.album,
                duration_ms=self._current.duration_ms,
                recording_mbid=self._current.mbid,
            )

    @Slot(int)
    def _on_position_updated(self, position_ms: int):
        st = self._current
        if st is None or st.eligible:
            return
        last = st.last_position_ms
        delta = position_ms - last
        # Normal forward ticks only — seeks (large deltas, or backward
        # jumps) do not contribute to elapsed.
        if 0 < delta < _MAX_TICK_DELTA_MS:
            st.elapsed_ms += delta
        st.last_position_ms = position_ms
        if (st.duration_ms >= _MIN_TRACK_DURATION_MS
                and st.elapsed_ms >= st.threshold_ms()):
            st.eligible = True

    @Slot()
    def _on_playback_stopped(self):
        self._maybe_scrobble_current()
        self._current = None

    @Slot()
    def _on_playback_ended(self):
        self._maybe_scrobble_current()
        self._current = None

    # ── Internals ───────────────────────────────────────────────────────────

    def _send_now_playing(self, st: _TrackState):
        """Fan out playing-now pings to every enabled service. Failures
        are silently dropped — now-playing is transient and a missed
        ping is not worth queueing for later replay."""
        if st.now_playing_sent:
            return
        st.now_playing_sent = True
        if self._settings.listenbrainz_enabled:
            token = self._settings.listenbrainz_token
            base = self._settings.listenbrainz_url
            if token:
                run_async(
                    listenbrainz.send_now_playing,
                    token, st.track_metadata_lb, base,
                )
        if self._settings.lastfm_enabled and lastfm.is_configured():
            sk = self._settings.lastfm_session_key
            if sk:
                run_async(
                    lastfm.update_now_playing,
                    sk, st.artist, st.track_name, st.album,
                    st.duration_ms, st.mbid,
                )

    def _maybe_scrobble_current(self):
        st = self._current
        if st is None or not st.eligible or st.scrobbled:
            return
        st.scrobbled = True
        # Snapshot for the closures so a follow-on track swap doesn't
        # mutate the payload mid-flight.
        listened_at = st.started_at_wall or int(time.time())
        if self._settings.listenbrainz_enabled:
            token = self._settings.listenbrainz_token
            base = self._settings.listenbrainz_url
            if token:
                payload = dict(st.track_metadata_lb)
                run_async(
                    listenbrainz.send_single_listen,
                    token, payload, listened_at, base,
                    on_result=lambda ok, p=payload, ts=listened_at:
                        self._on_lb_submit_result(ok, p, ts),
                    on_error=lambda _e, p=payload, ts=listened_at:
                        self._on_lb_submit_result(False, p, ts),
                )
            else:
                # Enabled but no token? Queue anyway so configuring
                # later catches up.
                _enqueue_lb(st.track_metadata_lb, listened_at)
        if self._settings.lastfm_enabled and lastfm.is_configured():
            sk = self._settings.lastfm_session_key
            artist = st.artist
            track = st.track_name
            album = st.album
            dur = st.duration_ms
            mbid = st.mbid
            if sk:
                run_async(
                    lastfm.scrobble,
                    sk, artist, track, listened_at, album, dur, mbid,
                    on_result=lambda res, a=artist, t=track, al=album,
                                       d=dur, m=mbid, ts=listened_at:
                        self._on_lastfm_submit_result(
                            res, a, t, al, d, m, ts,
                        ),
                    on_error=lambda _e, a=artist, t=track, al=album,
                                       d=dur, m=mbid, ts=listened_at:
                        self._on_lastfm_submit_result(
                            (False, None), a, t, al, d, m, ts,
                        ),
                )
            else:
                _enqueue_lastfm(artist, track, album, dur, mbid, listened_at)

    # ── Submit-result callbacks ────────────────────────────────────────────

    def _on_lb_submit_result(self, ok: bool, payload: Dict[str, Any],
                             listened_at: int):
        if ok:
            return
        _enqueue_lb(payload, listened_at)

    def _on_lastfm_submit_result(self, result, artist: str, track: str,
                                 album: str, duration_ms: int,
                                 mbid: str, listened_at: int):
        ok, _err = result if isinstance(result, tuple) else (bool(result), None)
        if ok:
            return
        _enqueue_lastfm(artist, track, album, duration_ms, mbid, listened_at)

    # ── Connectivity ──────────────────────────────────────────────────────

    @Slot(bool)
    def _on_connectivity_changed(self, reachable: bool):
        """Reconnect handler: when the server comes back, drain any
        scrobbles that queued while we were offline. Going offline is
        a no-op here — submits that fail land in the queue via the
        per-call result handlers."""
        if reachable:
            self.flush_pending()

    # ── Queue flush ────────────────────────────────────────────────────────

    def flush_pending(self):
        """Try to drain the offline queue for every enabled service.
        Called at startup, on a connectivity reconnect, and any time
        the user explicitly requests a flush. No-op if the service is
        disabled or has no token."""
        self._flush_listenbrainz_async()
        self._flush_lastfm_async()

    def _flush_listenbrainz_async(self):
        if not self._settings.listenbrainz_enabled:
            return
        token = self._settings.listenbrainz_token
        base = self._settings.listenbrainz_url
        if not token:
            return
        pending = scrobble_queue.pending("listenbrainz")
        if not pending:
            return
        # Convert stored entries into the wire shape submit-listens
        # expects. Cap at the API's documented batch limit; remaining
        # entries get caught on the next flush.
        listens: List[Dict[str, Any]] = []
        for entry in pending[:listenbrainz.MAX_LISTENS_PER_BATCH]:
            tm = entry.get("track_metadata")
            ts = entry.get("listened_at")
            if isinstance(tm, dict) and isinstance(ts, int):
                listens.append({"listened_at": ts, "track_metadata": tm})
        if not listens:
            return
        count = len(listens)
        run_async(
            listenbrainz.send_listens_batch,
            token, listens, base,
            on_result=lambda ok, c=count: ok and scrobble_queue.remove(
                "listenbrainz", c,
            ),
        )

    def _flush_lastfm_async(self):
        if not (self._settings.lastfm_enabled and lastfm.is_configured()):
            return
        sk = self._settings.lastfm_session_key
        if not sk:
            return
        pending = scrobble_queue.pending("lastfm")
        if not pending:
            return
        batch: List[Dict[str, Any]] = []
        for entry in pending[:lastfm.MAX_LISTENS_PER_BATCH]:
            if entry.get("artist") and entry.get("track") and entry.get("timestamp"):
                batch.append(entry)
        if not batch:
            return
        count = len(batch)
        run_async(
            lastfm.scrobble_batch,
            sk, batch,
            on_result=lambda res, c=count: (
                res[0] if isinstance(res, tuple) else bool(res)
            ) and scrobble_queue.remove("lastfm", c),
        )


# ── Module-level helpers ───────────────────────────────────────────────────

def _enqueue_lb(track_metadata: Dict[str, Any], listened_at: int):
    """Persist a failed/offline ListenBrainz submission for later replay."""
    if not isinstance(track_metadata, dict) or listened_at <= 0:
        return
    scrobble_queue.add("listenbrainz", {
        "track_metadata": track_metadata,
        "listened_at": int(listened_at),
    })


def _enqueue_lastfm(artist: str, track: str, album: str,
                    duration_ms: int, mbid: str, listened_at: int):
    if not artist or not track or listened_at <= 0:
        return
    scrobble_queue.add("lastfm", {
        "artist": artist,
        "track": track,
        "album": album or "",
        "duration_ms": int(duration_ms or 0),
        "mbid": mbid or "",
        "timestamp": int(listened_at),
    })


def _split_metadata(np: NowPlaying) -> tuple[str, str, str]:
    """Pull (artist, track, album) out of a NowPlaying. ``subtitle``
    on Audio items is the joined artist list; both providers populate
    it via ``QueueManager._make_now_playing``."""
    track = (np.title or "").strip()
    album = (np.album or "").strip()
    # Prefer the explicit raw.Artists list when present (Jellyfin); the
    # joined ``subtitle`` is the safe fallback for both providers.
    raw = np.raw or {}
    artist = ""
    artists = raw.get("Artists")
    if isinstance(artists, list) and artists:
        artist = ", ".join(str(a) for a in artists if a).strip()
    if not artist:
        artist = (np.subtitle or "").strip()
    return artist, track, album


def _extract_mbid(np: NowPlaying) -> str:
    """Best-effort MusicBrainz Recording ID lookup. Both providers can
    surface this when their server has the tag, but neither guarantees
    it — empty string is the common outcome."""
    raw = np.raw or {}
    # Jellyfin: ``ProviderIds.MusicBrainzTrack`` (recording-level on
    # most installs; ``MusicBrainzRecording`` on newer ones).
    pids = raw.get("ProviderIds") or {}
    if isinstance(pids, dict):
        for key in ("MusicBrainzRecording", "MusicBrainzTrack"):
            v = pids.get(key)
            if isinstance(v, str) and v:
                return v
    # Subsonic: ``musicBrainzId`` straight on the song dict (when the
    # server is Navidrome with the right metadata).
    v = raw.get("musicBrainzId")
    if isinstance(v, str) and v:
        return v
    return ""
