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
   Failures land in ``jellytoast/scrobble/queue.py`` for later flush.

Eligibility rule (shared by both services):

> A track is scrobbled when it is **longer than 30 s** and has been
> played for **≥ 50 % of its length, or ≥ 4 minutes, whichever comes
> first**.

Network calls all run on the shared thread pool via ``run_async`` so
the GUI thread never stalls. The manager itself is a ``QObject``
constructed once at startup; ``jellytoast.scrobble.get_scrobble_manager``
is the public accessor.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from PySide6.QtCore import QObject, Slot

from jellytoast.async_io import run_async
from jellytoast.player_state import NowPlaying, PlayerBus
from jellytoast.settings import get_settings

from . import lastfm, listenbrainz
from . import queue as scrobble_queue

# Per-tick cap for elapsed bookkeeping. position_updated normally fires
# every 200-500ms during playback, so a real forward delta is small. A
# user seek (or a paused-then-resumed jump) shows up as a multi-second
# delta — skip it so a forward seek can't satisfy the eligibility rule
# without actually listening. The boundary is inclusive: a delta of
# exactly 5_000 ms counts as a normal (large) forward tick; only
# strictly greater deltas are treated as seeks.
_MAX_TICK_DELTA_MS = 5_000

# Floor on track length: ListenBrainz / Last.fm both require tracks
# to be *longer than* 30 s (a track of exactly 30 s is below the
# floor, matching the published scrobbling rules).
_MIN_TRACK_DURATION_MS = 30_000

# Cap on elapsed needed to scrobble: 4 minutes (the "or 4min" half of
# the rule). Below this the 50%-of-length half wins.
_MAX_ELIGIBILITY_MS = 4 * 60_000


@dataclass
class _TrackState:
    np: NowPlaying
    started_at_wall: int = 0  # UNIX seconds, UTC, at playback_started
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
        # Item id of the most recently scrobbled track + a one-shot flag
        # the cast path sets (``note_cast_handoff``) right before it
        # re-emits ``playback_started`` purely to re-render the bar on the
        # cast device. Together they stop a just-scrobbled track from
        # being re-armed and double-counted across a cast handoff.
        self._recently_scrobbled_item_id: str = ""
        self._suppress_rescrobble_once: bool = False
        # Per-service flush guards. A reconnect emits BOTH
        # connectivity_changed and offline_mode_changed (each calls
        # flush_pending), so without these the second call kicks off a
        # duplicate async submit before the first lands. Set when a
        # service's async flush starts, cleared in its result/error
        # callback (a synchronous guard wouldn't span run_async).
        self._lb_flush_in_flight: bool = False
        self._lf_flush_in_flight: bool = False

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
        # The user-facing offline toggle is a second drain trigger:
        # a manual offline → online flip might land while the network
        # is still up (connectivity_changed never fires), so we'd
        # otherwise sit on the queue until the next real outage.
        self._bus.offline_mode_changed.connect(self._on_offline_mode_changed)

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
                artist_name=artist,
                track_name=track_name,
                release_name=album,
                duration_ms=int(np.duration or 0),
                recording_mbid=mbid,
            ),
        )
        # Cast-handoff re-emit: if the cast path just re-fired
        # playback_started for a track we JUST scrobbled, carry the
        # scrobbled flag over so the cast device's position feed can't
        # re-cross eligibility and double-count the same listen.
        if self._suppress_rescrobble_once:
            self._suppress_rescrobble_once = False
            if np.item_id == self._recently_scrobbled_item_id:
                state.scrobbled = True
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
            # Re-evaluate eligibility now that we know the duration —
            # without this, a track whose duration arrives AFTER
            # position has already crossed threshold would scrobble
            # only if another position tick fires before the track
            # ends. For a track ending on the same tick the duration
            # arrives, _maybe_scrobble_current would otherwise see
            # eligible=False and skip.
            st = self._current
            if (
                not st.eligible
                and st.duration_ms > _MIN_TRACK_DURATION_MS
                and st.elapsed_ms >= st.threshold_ms()
            ):
                st.eligible = True

    @Slot(int)
    def _on_position_updated(self, position_ms: int):
        st = self._current
        if st is None or st.eligible:
            return
        last = st.last_position_ms
        delta = position_ms - last
        # Normal forward ticks only — seeks (delta strictly greater
        # than the cap, or backward jumps) do not contribute to
        # elapsed. The upper bound is inclusive so a tick that lands
        # exactly on the cap still counts.
        if 0 < delta <= _MAX_TICK_DELTA_MS:
            st.elapsed_ms += delta
        st.last_position_ms = position_ms
        if st.duration_ms > _MIN_TRACK_DURATION_MS and st.elapsed_ms >= st.threshold_ms():
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

    def _lb_in_app_active(self) -> bool:
        """Whether jellytoast should scrobble to ListenBrainz itself — the
        double-scrobble guard. True when LB is enabled AND we're not deferring
        to a server-side scrobbler. ``server_scrobbles_listenbrainz`` is set
        when recent LB listens carry a non-jellytoast ``submission_client``
        (the server, e.g. Navidrome, is already scrobbling this account); the
        user can force in-app anyway via ``scrobble_in_app_anyway`` (e.g. when
        the 'other' submitter is a different app, not their server)."""
        if not self._settings.listenbrainz_enabled:
            return False
        # getattr defaults keep this safe against minimal settings stubs and
        # any pre-feature config: absent flags == no server scrobbler, no
        # override == normal in-app scrobbling.
        if getattr(self._settings, "server_scrobbles_listenbrainz", False) and not getattr(
            self._settings, "scrobble_in_app_anyway", False
        ):
            return False
        return True

    def _send_now_playing(self, st: _TrackState):
        """Fan out playing-now pings to every enabled service. Failures
        are silently dropped — now-playing is transient and a missed
        ping is not worth queueing for later replay.

        Skipped entirely while in offline mode: now-playing pings are
        ephemeral (the listener has tuned past the track by the time
        the network comes back) and the network call would just sit
        and time out, wasting cycles + a thread slot."""
        if st.now_playing_sent:
            return
        st.now_playing_sent = True
        if self._settings.offline_mode:
            return
        if self._lb_in_app_active():
            token = self._settings.listenbrainz_token
            base = self._settings.listenbrainz_url
            if token:
                run_async(
                    listenbrainz.send_now_playing,
                    token,
                    st.track_metadata_lb,
                    base,
                )
        if self._settings.lastfm_enabled and lastfm.is_configured():
            sk = self._settings.lastfm_session_key
            if sk:
                run_async(
                    lastfm.update_now_playing,
                    sk,
                    st.artist,
                    st.track_name,
                    st.album,
                    st.duration_ms,
                    st.mbid,
                )

    def _maybe_scrobble_current(self):
        st = self._current
        if st is None or not st.eligible or st.scrobbled:
            return
        st.scrobbled = True
        self._recently_scrobbled_item_id = st.np.item_id
        # Snapshot for the closures so a follow-on track swap doesn't
        # mutate the payload mid-flight.
        listened_at = st.started_at_wall or int(time.time())
        # In offline mode the submit would just sit and time out before
        # landing in the queue via its failure callback — skip the
        # network attempt and enqueue directly. The
        # offline_mode_changed / connectivity_changed handlers drain
        # the queue when service is restored.
        offline = bool(self._settings.offline_mode)
        if self._lb_in_app_active():
            token = self._settings.listenbrainz_token
            base = self._settings.listenbrainz_url
            if token and not offline:
                payload = dict(st.track_metadata_lb)
                run_async(
                    listenbrainz.send_single_listen,
                    token,
                    payload,
                    listened_at,
                    base,
                    on_result=lambda ok, p=payload, ts=listened_at: self._on_lb_submit_result(
                        ok, p, ts
                    ),
                    on_error=lambda _e, p=payload, ts=listened_at: self._on_lb_submit_result(
                        False, p, ts
                    ),
                )
            else:
                # Enabled but no token (or offline)? Queue anyway so
                # configuring later / reconnecting drains it.
                _enqueue_lb(st.track_metadata_lb, listened_at)
        if self._settings.lastfm_enabled and lastfm.is_configured():
            sk = self._settings.lastfm_session_key
            artist = st.artist
            track = st.track_name
            album = st.album
            dur = st.duration_ms
            mbid = st.mbid
            if sk and not offline:
                run_async(
                    lastfm.scrobble,
                    sk,
                    artist,
                    track,
                    listened_at,
                    album,
                    dur,
                    mbid,
                    on_result=lambda res, a=artist, t=track, al=album, d=dur, m=mbid, ts=listened_at: (
                        self._on_lastfm_submit_result(
                            res,
                            a,
                            t,
                            al,
                            d,
                            m,
                            ts,
                        )
                    ),
                    on_error=lambda _e, a=artist, t=track, al=album, d=dur, m=mbid, ts=listened_at: (
                        self._on_lastfm_submit_result(
                            (False, None),
                            a,
                            t,
                            al,
                            d,
                            m,
                            ts,
                        )
                    ),
                )
            else:
                _enqueue_lastfm(artist, track, album, dur, mbid, listened_at)

    def note_cast_handoff(self):
        """Flag the next ``playback_started`` as a cast-handoff re-render
        (not a fresh listen). The cast path stops local playback — which
        may scrobble the current track — then re-emits playback_started
        purely to re-render the bar on the cast device; without this flag
        the cast device's position feed re-arms and double-counts a track
        that was already scrobbled. Consumed once in
        ``_on_playback_started``."""
        self._suppress_rescrobble_once = True

    def flush_current_on_quit(self):
        """Persist the in-flight eligible track to the offline queue
        SYNCHRONOUSLY on shutdown so a quit doesn't lose it.

        A normal scrobble submits via ``run_async``, but at quit the pool
        worker can be killed and the GUI-thread result callback can't run
        once the event loop stops — so the network submit (and its
        enqueue-on-failure) silently drops. Writing straight to the queue
        here means the next launch's ``flush_pending`` sends it. Covers
        the window-close / SIGTERM path (which never emits
        playback_stopped) and the tray-Quit path (call this BEFORE the
        stop so its async submit never arms)."""
        st = self._current
        if st is None or not st.eligible or st.scrobbled:
            return
        st.scrobbled = True
        self._recently_scrobbled_item_id = st.np.item_id
        listened_at = st.started_at_wall or int(time.time())
        if self._lb_in_app_active():
            _enqueue_lb(dict(st.track_metadata_lb), listened_at)
        if self._settings.lastfm_enabled and lastfm.is_configured():
            _enqueue_lastfm(
                st.artist, st.track_name, st.album, st.duration_ms, st.mbid, listened_at
            )

    # ── Submit-result callbacks ────────────────────────────────────────────

    def _on_lb_submit_result(self, ok: bool, payload: Dict[str, Any], listened_at: int):
        if ok:
            return
        _enqueue_lb(payload, listened_at)

    def _on_lastfm_submit_result(
        self,
        result,
        artist: str,
        track: str,
        album: str,
        duration_ms: int,
        mbid: str,
        listened_at: int,
    ):
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

    @Slot(bool)
    def _on_offline_mode_changed(self, offline: bool):
        """User-initiated offline → online flip is a second drain
        trigger. ``connectivity_changed`` only fires on the underlying
        network state edge, so a user who manually disables the
        offline-mode toggle while the network was up the whole time
        would never see ``connectivity_changed`` and the queue would
        sit until the next real outage cycled it. Drain on the toggle
        edge too. Going offline is a no-op here — the gate inside
        ``_maybe_scrobble_current`` takes care of new submits."""
        if not offline:
            self.flush_pending()

    # ── Queue flush ────────────────────────────────────────────────────────

    def flush_pending(self):
        """Try to drain the offline queue for every enabled service.
        Called at startup, on a connectivity reconnect, and any time
        the user explicitly requests a flush. No-op if the service is
        disabled or has no token."""
        # Honour offline mode here too. flush_pending fires at startup +
        # on every connectivity edge; _send_now_playing /
        # _maybe_scrobble_current already gate on offline_mode, but the
        # queue drain used to POST regardless — so an offline user still
        # phoned home to LB/Last.fm at launch and on every reconnect.
        if self._settings.offline_mode:
            return
        self._flush_listenbrainz_async()
        self._flush_lastfm_async()

    def _flush_listenbrainz_async(self):
        if self._lb_flush_in_flight:
            return
        if not self._lb_in_app_active():
            # In-app LB is suppressed (the server scrobbles directly), so any
            # queued in-app listens are redundant — clear them instead of
            # stranding the queue forever (re-submitting would double-scrobble).
            scrobble_queue.clear("listenbrainz")
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
        scanned = pending[: listenbrainz.MAX_LISTENS_PER_BATCH]
        listens: List[Dict[str, Any]] = []
        for entry in scanned:
            tm = entry.get("track_metadata")
            ts = entry.get("listened_at")
            if isinstance(tm, dict) and isinstance(ts, int):
                listens.append({"listened_at": ts, "track_metadata": tm})
        if not listens:
            # The whole scanned prefix is malformed (missing
            # track_metadata/listened_at) — it can never become valid, and
            # _done (which evicts) only fires on a send. Drop the poison
            # head here so it can't block the queue forever.
            if scanned:
                scrobble_queue.remove("listenbrainz", records=scanned)
            return
        self._lb_flush_in_flight = True

        def _done(ok, _recs=scanned):
            self._lb_flush_in_flight = False
            # Remove exactly the SCANNED records by identity — not the
            # oldest-N by count. Count-removal dropped never-sent entries
            # when two flushes overlapped; it also handles malformed
            # entries interleaved in the slice (they're in _recs, so they
            # get cleared with the sent ones).
            if ok:
                scrobble_queue.remove("listenbrainz", records=_recs)

        run_async(
            listenbrainz.send_listens_batch,
            token,
            listens,
            base,
            on_result=_done,
            on_error=lambda _e: setattr(self, "_lb_flush_in_flight", False),
        )

    def _flush_lastfm_async(self):
        if self._lf_flush_in_flight:
            return
        if not (self._settings.lastfm_enabled and lastfm.is_configured()):
            return
        sk = self._settings.lastfm_session_key
        if not sk:
            return
        pending = scrobble_queue.pending("lastfm")
        if not pending:
            return
        scanned = pending[: lastfm.MAX_LISTENS_PER_BATCH]
        batch: List[Dict[str, Any]] = []
        for entry in scanned:
            if entry.get("artist") and entry.get("track") and entry.get("timestamp"):
                batch.append(entry)
        if not batch:
            # All-malformed scanned head (see the ListenBrainz path) — evict
            # it so the queue can't stay stuck behind unsendable entries.
            if scanned:
                scrobble_queue.remove("lastfm", records=scanned)
            return
        self._lf_flush_in_flight = True

        def _done(res, _recs=scanned):
            self._lf_flush_in_flight = False
            ok = res[0] if isinstance(res, tuple) else bool(res)
            # Identity removal (see the ListenBrainz path) — drops exactly
            # the sent records, not the oldest-N by count.
            if ok:
                scrobble_queue.remove("lastfm", records=_recs)

        run_async(
            lastfm.scrobble_batch,
            sk,
            batch,
            on_result=_done,
            on_error=lambda _e: setattr(self, "_lf_flush_in_flight", False),
        )


# ── Module-level helpers ───────────────────────────────────────────────────


def _enqueue_lb(track_metadata: Dict[str, Any], listened_at: int):
    """Persist a failed/offline ListenBrainz submission for later replay."""
    if not isinstance(track_metadata, dict) or listened_at <= 0:
        return
    scrobble_queue.add(
        "listenbrainz",
        {
            "track_metadata": track_metadata,
            "listened_at": int(listened_at),
        },
    )


def _enqueue_lastfm(
    artist: str, track: str, album: str, duration_ms: int, mbid: str, listened_at: int
):
    if not artist or not track or listened_at <= 0:
        return
    scrobble_queue.add(
        "lastfm",
        {
            "artist": artist,
            "track": track,
            "album": album or "",
            "duration_ms": int(duration_ms or 0),
            "mbid": mbid or "",
            "timestamp": int(listened_at),
        },
    )


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
    # ONE path for both backends: Jellyfin sets ProviderIds natively
    # (recording-level ``MusicBrainzTrack`` on most installs,
    # ``MusicBrainzRecording`` on newer ones) and the Subsonic adapter
    # projects Navidrome's ``musicBrainzId`` into the same shape
    # (subsonic._adapt_song) — no provider-private stash peeking here.
    pids = raw.get("ProviderIds") or {}
    if isinstance(pids, dict):
        for key in ("MusicBrainzRecording", "MusicBrainzTrack"):
            v = pids.get(key)
            if isinstance(v, str) and v:
                return v
    # Legacy fallback: ``musicBrainzId`` straight on the dict (rare).
    v = raw.get("musicBrainzId")
    if isinstance(v, str) and v:
        return v
    return ""
