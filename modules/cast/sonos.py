"""Native Sonos cast backend.

Implements the discovery, transport, grouping, and event-bridge slice
described in ``docs/research/casting_sonos.md``. UI integration and
``cast_manager`` wiring land separately — this module exposes a small,
self-contained set of helpers any caller can drive from a worker
thread via ``modules.async_io.run_async``.

Design notes (see the research doc for the long form):

- **Sync soco only.** We deliberately avoid ``soco.events_asyncio``;
  the sync events module is paired with a Qt ``QTimer`` watchdog so
  the bug-prone long-running asyncio renewal path stays out of our
  code path.
- **Coordinator-aware.** Every transport push resolves the target
  to its group coordinator; ``play_uri`` against a member silently
  no-ops on the speaker side, so we resolve up-front and log it.
- **Cast-proxy first.** Every URL we push to a speaker is routed
  through ``modules.cast_proxy.resolve_cast_url`` so a Tailscale /
  remote-host server and ``file://`` downloaded blobs both work
  identically — same contract as Chromecast / AirPlay 2.
- **Untested against real hardware.** This file ships "should-work,
  untested" — soco's API is stable and well-mocked, but it has not
  been validated against a live speaker as of this commit. No
  device-specific quirks are pre-baked in; the doc enumerates a few
  candidates (S1 retry on cold start, etc.) that get added only
  after a real-world report.

The package layout mirrors ``modules/scrobble/``: this is the public
entry; ``_sonos_events.py`` holds the Qt-friendly event subscription
wrapper.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Iterable, List, Optional, Set, Tuple
from xml.sax.saxutils import escape as _xml_escape

from PySide6.QtCore import QObject, QTimer, Signal

# Top-level lazy-import pattern (per the project's
# feedback_local_reimport_scoping.md memory): set the soco symbols to
# None at module load, populate on first use. soco at import drags in
# ``requests`` plus a chunk of XML / xmltodict — paying that only when
# discovery actually runs keeps Settings imports cheap.

soco = None  # type: ignore[assignment]
SoCoException = Exception  # type: ignore[assignment,misc]
_SOCO_AVAILABLE: Optional[bool] = None


def _ensure_soco() -> bool:
    """Lazy-import soco. Returns True on success, False if the dep is
    missing (Sonos discovery silently no-ops in that case)."""
    global soco, SoCoException, _SOCO_AVAILABLE
    if _SOCO_AVAILABLE is None:
        try:
            import soco as _soco
            from soco.exceptions import SoCoException as _SoCoException

            soco = _soco
            SoCoException = _SoCoException  # type: ignore[assignment,misc]
            _SOCO_AVAILABLE = True
        except ImportError:
            _SOCO_AVAILABLE = False
    return bool(_SOCO_AVAILABLE)


def is_available() -> bool:
    """True when the ``soco`` dependency is importable on this host."""
    return _ensure_soco()


# ── Public dataclasses ─────────────────────────────────────────────────────


@dataclass
class SonosZone:
    """One Sonos group's worth of state, as the cast picker wants it.

    Mirrors the ``CastDevice`` shape in ``cast_manager`` but carries
    enough zone topology that a Sonos-specific tree view can render
    "Kitchen + Patio" with per-member volume sliders without a second
    SOAP round-trip.

    ``coordinator`` and ``members`` hold the raw ``soco.SoCo`` objects
    for the lifetime of one cast session. They get dropped on
    ``stop_sonos`` so the next discovery starts clean.
    """

    uuid: str
    label: str
    coordinator_ip: str
    coordinator_uuid: str
    member_uuids: List[str] = field(default_factory=list)
    member_names: List[str] = field(default_factory=list)
    is_group: bool = False
    is_visible: bool = True
    # Loose-typed payload so we don't force soco onto every caller's
    # type-checker. The cast manager casts back to soco.SoCo when it
    # needs to act on the zone.
    coordinator: object = field(default=None, repr=False)
    members: List[object] = field(default_factory=list, repr=False)


# ── DIDL-Lite metadata ─────────────────────────────────────────────────────

_DIDL_TEMPLATE = (
    "<DIDL-Lite "
    'xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
    'xmlns:dc="http://purl.org/dc/elements/1.1/" '
    'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">'
    '<item id="jellytoast-1" parentID="-1" restricted="true">'
    "<dc:title>{title}</dc:title>"
    "<dc:creator>{creator}</dc:creator>"
    "<upnp:album>{album}</upnp:album>"
    "{art}"
    "<upnp:class>object.item.audioItem.musicTrack</upnp:class>"
    "</item>"
    "</DIDL-Lite>"
)


def build_didl(title: str, artist: str = "", album: str = "", art_url: str = "") -> str:
    """Render a DIDL-Lite envelope for a single track push.

    Sonos accepts the empty / non-XML / wrong-namespace cases by
    silently refusing to play. We always emit the required namespaces
    and HTML-escape the user-supplied fields so an apostrophe in a
    title doesn't blow up the SOAP envelope.
    """
    art = ""
    if art_url:
        art = f"<upnp:albumArtURI>{_xml_escape(art_url)}</upnp:albumArtURI>"
    return _DIDL_TEMPLATE.format(
        title=_xml_escape(title or ""),
        creator=_xml_escape(artist or ""),
        album=_xml_escape(album or ""),
        art=art,
    )


# ── Discovery ──────────────────────────────────────────────────────────────


def discover_sonos(timeout: float = 1.0) -> List[SonosZone]:
    """Run an SSDP M-SEARCH for ``urn:schemas-upnp-org:device:ZonePlayer:1``
    and return the resulting zones, one per group coordinator plus any
    standalone players.

    Blocking — call from a worker thread. Returns ``[]`` on any failure
    (no LAN multicast, no Sonos on the network, soco missing).
    Honors ``Settings.sonos_enabled``: returns ``[]`` immediately when
    the user has disabled Sonos discovery.
    """
    from modules.settings import get_settings

    if not get_settings().sonos_enabled:
        return []
    if not _ensure_soco():
        return []
    try:
        zones = soco.discover(timeout=timeout) or set()
    except Exception:
        return []
    return expand_topology(zones)


def expand_topology(zones: Iterable[object]) -> List[SonosZone]:
    """Given any one or more ``SoCo`` instances on the LAN, query
    topology and build a stable list of ``SonosZone`` entries.

    One responding speaker is enough — soco reports the whole
    household. We dedupe across multiple seeds, drop invisible
    players (bonded subs / surrounds), and resolve each group's
    coordinator UUID so the cast picker can pre-select the user's
    last-used zone.
    """
    seeds = [z for z in zones if z is not None]
    if not seeds:
        return []
    seed = seeds[0]
    try:
        groups = list(getattr(seed, "all_groups", []) or [])
    except Exception:
        return []

    out: List[SonosZone] = []
    seen: Set[str] = set()
    for g in groups:
        try:
            coord = getattr(g, "coordinator", None)
            if coord is None:
                continue
            coord_uuid = str(getattr(coord, "uid", "")) or ""
            if not coord_uuid or coord_uuid in seen:
                continue
            seen.add(coord_uuid)
            members = list(getattr(g, "members", []) or [])
            visible_members = [m for m in members if getattr(m, "is_visible", True)]
            member_uuids = [str(getattr(m, "uid", "")) for m in visible_members]
            member_names = [str(getattr(m, "player_name", "") or "") for m in visible_members]
            label = str(getattr(g, "label", "")) or (member_names[0] if member_names else "")
            zone = SonosZone(
                uuid=coord_uuid,
                label=label or "Sonos",
                coordinator_ip=str(getattr(coord, "ip_address", "") or ""),
                coordinator_uuid=coord_uuid,
                member_uuids=member_uuids,
                member_names=member_names,
                is_group=len(visible_members) > 1,
                is_visible=bool(getattr(coord, "is_visible", True)),
                coordinator=coord,
                members=visible_members,
            )
            out.append(zone)
        except Exception:
            continue
    out.sort(key=lambda z: z.label.lower())
    return out


# ── Coordinator resolution ─────────────────────────────────────────────────


def resolve_coordinator(zone_or_player: object) -> Optional[object]:
    """Return the SoCo coordinator for whatever we were handed.

    Accepts a ``SonosZone``, a raw ``SoCo`` instance, or anything with
    a ``group.coordinator`` attribute. ``play_uri`` on a non-coordinator
    is the #1 silent-failure mode on Sonos — every transport entry
    point funnels through here.
    """
    if zone_or_player is None:
        return None
    if isinstance(zone_or_player, SonosZone):
        return zone_or_player.coordinator
    try:
        group = getattr(zone_or_player, "group", None)
        if group is not None:
            coord = getattr(group, "coordinator", None)
            if coord is not None:
                return coord
    except Exception:
        pass
    return zone_or_player


# ── Transport ──────────────────────────────────────────────────────────────


def cast_to_sonos(
    zone_or_player: object,
    url: str,
    title: str = "",
    artist: str = "",
    album: str = "",
    art_url: str = "",
    apply_volume_floor: bool = True,
) -> bool:
    """Push ``url`` to the zone's coordinator with DIDL metadata.

    Returns True when the SOAP call returns without error. False on
    coordinator resolution failure, soco missing, or any SoCoException
    bubbling out of the underlying ``play_uri`` call. Blocking — call
    from a worker thread.

    ``url`` is routed through the cast-proxy first so a Tailscale /
    self-signed / ``file://`` upstream still resolves to something the
    speaker can fetch.
    """
    if not _ensure_soco():
        return False
    coord = resolve_coordinator(zone_or_player)
    if coord is None:
        return False
    try:
        from modules.cast_proxy import resolve_cast_url
    except Exception:
        # Cast proxy missing should never happen — but the doc says
        # degrade gracefully to the raw URL rather than refusing.
        def resolve_cast_url(u: str) -> str:  # type: ignore[no-redef]
            return u

    proxy_url = resolve_cast_url(url) if url else url
    proxy_art = resolve_cast_url(art_url) if art_url else ""
    didl = build_didl(title, artist=artist, album=album, art_url=proxy_art)
    try:
        if apply_volume_floor:
            _apply_volume_floor(coord)
        coord.play_uri(proxy_url, meta=didl, title=title or "")
        return True
    except SoCoException:
        return False
    except Exception:
        return False


def stop_sonos(zone_or_player: object) -> bool:
    """Stop playback on the zone's coordinator. Returns False on any
    failure but never raises."""
    if not _ensure_soco():
        return False
    coord = resolve_coordinator(zone_or_player)
    if coord is None:
        return False
    try:
        coord.stop()
        return True
    except SoCoException:
        return False
    except Exception:
        return False


def pause_sonos(zone_or_player: object) -> bool:
    """Pause playback on the coordinator. False on any failure."""
    coord = resolve_coordinator(zone_or_player)
    if coord is None:
        return False
    try:
        coord.pause()
        return True
    except Exception:
        return False


def play_sonos(zone_or_player: object) -> bool:
    """Resume playback on the coordinator. False on any failure."""
    coord = resolve_coordinator(zone_or_player)
    if coord is None:
        return False
    try:
        coord.play()
        return True
    except Exception:
        return False


def seek_sonos(zone_or_player: object, seconds: float) -> bool:
    """Seek to ``seconds`` on the coordinator (soco expects HH:MM:SS)."""
    coord = resolve_coordinator(zone_or_player)
    if coord is None:
        return False
    try:
        sec = max(0, int(round(seconds)))
        h, rem = divmod(sec, 3600)
        m, s = divmod(rem, 60)
        coord.seek(f"{h}:{m:02d}:{s:02d}")
        return True
    except Exception:
        return False


def _parse_hms(value: object) -> float:
    """Parse soco's ``H:MM:SS`` (or ``M:SS``) position string to seconds.
    Returns 0.0 for empty / ``NOT_IMPLEMENTED`` / malformed values."""
    try:
        parts = [int(p) for p in str(value).split(":")]
    except (ValueError, TypeError):
        return 0.0
    sec = 0.0
    for p in parts:
        sec = sec * 60 + p
    return sec


def seek_relative_sonos(zone_or_player: object, delta_sec: float) -> bool:
    """Skip ±: read the coordinator's current track position and
    absolute-seek to ``pos + delta_sec`` (clamped at 0). False on any
    failure. UNVERIFIED against real hardware."""
    coord = resolve_coordinator(zone_or_player)
    if coord is None:
        return False
    try:
        info = coord.get_current_track_info() or {}
        pos = _parse_hms(info.get("position", "0:00:00"))
        return seek_sonos(coord, max(0.0, pos + float(delta_sec)))
    except Exception:
        return False


# ── Volume ─────────────────────────────────────────────────────────────────


def set_volume(player_or_zone: object, level_pct: int) -> bool:
    """Set group-master volume (when given a ``SonosZone``) or
    individual-speaker volume (when given a raw ``SoCo``). The Sonos
    protocol distinction is "is this the coordinator I'm setting on
    or a specific member" — we mirror it.

    ``level_pct`` is clamped to 0–100.
    """
    level = max(0, min(100, int(level_pct)))
    try:
        if isinstance(player_or_zone, SonosZone):
            coord = player_or_zone.coordinator
            if coord is None:
                return False
            # group.volume sets the master fader proportionally across
            # all members; if the zone is a single speaker this is
            # equivalent to coord.volume.
            group = getattr(coord, "group", None)
            if group is not None:
                group.volume = level
            else:
                coord.volume = level
            return True
        # raw SoCo instance — per-member volume
        player_or_zone.volume = level  # type: ignore[attr-defined]
        return True
    except Exception:
        return False


def _apply_volume_floor(coord: object) -> None:
    """Bump volume to ``max(current, sonos_volume_floor)`` on the first
    push to a zone. Survives a Sonos Connect:Amp set to 100% from the
    Sonos app — a "blasted by 3 AM defaults" footgun the doc calls out.
    """
    try:
        from modules.settings import get_settings

        floor = int(get_settings().sonos_volume_floor)
        if floor <= 0:
            return
        group = getattr(coord, "group", None)
        target = group if group is not None else coord
        current = int(getattr(target, "volume", 0) or 0)
        if current < floor:
            target.volume = floor
    except Exception:
        pass


# ── Grouping ───────────────────────────────────────────────────────────────


def join_group(member: object, coordinator: object) -> bool:
    """Make ``member`` join ``coordinator``'s group. Additive — doesn't
    touch any zone the coordinator wasn't already managing. Returns
    False on any failure."""
    coord = resolve_coordinator(coordinator)
    if coord is None or member is None:
        return False
    try:
        member.join(coord)  # type: ignore[attr-defined]
        return True
    except Exception:
        return False


def unjoin(member: object) -> bool:
    """Remove ``member`` from any group it's part of (becoming its own
    one-zone group). Returns False on any failure."""
    if member is None:
        return False
    try:
        member.unjoin()  # type: ignore[attr-defined]
        return True
    except Exception:
        return False


def members_for_zone(zone: SonosZone) -> List[Tuple[str, str, int]]:
    """Return ``[(uuid, name, volume), ...]`` for the visible members
    of ``zone``. Volume reads happen here so the caller can populate
    per-member sliders without driving SOAP themselves. Best-effort:
    a member whose volume read fails returns 50."""
    out: List[Tuple[str, str, int]] = []
    for m in zone.members:
        uuid = str(getattr(m, "uid", ""))
        name = str(getattr(m, "player_name", "") or "")
        try:
            vol = int(getattr(m, "volume", 50) or 50)
        except Exception:
            vol = 50
        out.append((uuid, name, max(0, min(100, vol))))
    return out


# ── Event bridge ───────────────────────────────────────────────────────────

# Two SOAP services jellytoast cares about on the coordinator:
#   avTransport — transport state (PLAYING/PAUSED/STOPPED), current
#                 track URI, position, duration.
#   renderingControl — group + per-member volume, mute, balance.
# These are subscribed independently because Sonos publishes them on
# separate UPnP services with separate auto-renew clocks.

_EVENT_SERVICES = ("avTransport", "renderingControl")


class SonosEventBridge(QObject):
    """Qt-thread-safe wrapper around soco's sync event subscriptions.

    STATUS (2026-06-01): shipped + unit-tested but **not yet wired** into
    the cast flow — no production caller constructs it. It's kept ready for
    Sonos push-event support (live volume/transport updates from the zone)
    once that's hardware-verified; until then the Sonos backend polls.
    Don't delete — wire it when the Sonos hardware-validation item
    (docs/TODO.md, P4 casting) gets hardware to validate against.

    Spawns a subscription per service on the supplied coordinator,
    polls each subscription's ``events`` queue from a worker thread,
    and re-emits via Qt signals so slot dispatch lands on the GUI
    thread automatically.

    Renewal watchdog: a ``QTimer`` re-checks every 5 minutes that
    subscriptions are still alive (`is_subscribed`); on failure the
    bridge force-unsubscribes and re-subscribes. This is the soco
    SoCo issue #822 mitigation — sync events + Qt timer beats
    ``events_asyncio`` for our workload.

    Lifecycle:
      bridge = SonosEventBridge(coord)
      bridge.transport_event.connect(handler)
      bridge.start()
      ...
      bridge.stop()        # idempotent
    """

    # str(service_name), dict(variable -> value)
    event_received = Signal(str, dict)
    # convenience: a parsed avTransport state ("PLAYING" / "PAUSED" / …)
    transport_state = Signal(str)
    # subscription dropped + couldn't recover
    subscription_failed = Signal(str)  # service name

    # Defaults — overridable for tests. Times in milliseconds.
    POLL_INTERVAL_MS = 500
    WATCHDOG_INTERVAL_MS = 5 * 60 * 1000  # 5 minutes
    # 30 min subscription * 1.5 = silence window before we force renew.
    SILENCE_LIMIT_S = 45 * 60

    def __init__(self, coordinator: object, parent: Optional[QObject] = None, event_port: int = 0):
        super().__init__(parent)
        self._coord = coordinator
        self._event_port = int(event_port or 0)
        self._subs: dict = {}  # service_name -> Subscription
        self._last_event_ts: dict = {}  # service_name -> monotonic float
        self._stopped = False
        self._poll_timer: Optional[QTimer] = None
        self._watchdog_timer: Optional[QTimer] = None

    # ── Subscription lifecycle ─────────────────────────────────────────

    def start(self) -> bool:
        """Open subscriptions on all watched services. Returns False
        if soco is unavailable or the initial subscribe fails on
        every service — we don't half-start. Idempotent: calling
        twice is a no-op."""
        if self._stopped:
            return False
        if self._subs:
            return True
        if not _ensure_soco():
            return False
        opened = self._open_subscriptions()
        if not opened:
            return False
        self._start_timers()
        return True

    def _open_subscriptions(self) -> int:
        """Subscribe to each service in ``_EVENT_SERVICES``. Returns
        the number of services that subscribed cleanly."""
        opened = 0
        for service_name in _EVENT_SERVICES:
            if self._subscribe(service_name):
                opened += 1
        return opened

    def _subscribe(self, service_name: str) -> bool:
        """Open one subscription. Records the timestamp so the
        watchdog has a reference point even before the first event
        arrives."""
        coord = self._coord
        service = getattr(coord, service_name, None)
        if service is None:
            return False
        try:
            sub = service.subscribe(auto_renew=True)
            self._subs[service_name] = sub
            self._last_event_ts[service_name] = time.monotonic()
            return True
        except Exception:
            return False

    def _start_timers(self) -> None:
        # Poll the event queues frequently so transport-state updates
        # surface to the UI within ~500 ms of the speaker emitting.
        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(self.POLL_INTERVAL_MS)
        self._poll_timer.timeout.connect(self._drain_event_queues)
        self._poll_timer.start()
        # Watchdog renews stuck subscriptions on a much longer beat.
        self._watchdog_timer = QTimer(self)
        self._watchdog_timer.setInterval(self.WATCHDOG_INTERVAL_MS)
        self._watchdog_timer.timeout.connect(self._renew_if_stale)
        self._watchdog_timer.start()

    def stop(self) -> None:
        """Tear down subscriptions + timers. Idempotent. Safe to call
        from the destructor of whatever owns the bridge."""
        self._stopped = True
        for t in (self._poll_timer, self._watchdog_timer):
            if t is not None:
                try:
                    t.stop()
                except Exception:
                    pass
        self._poll_timer = None
        self._watchdog_timer = None
        for service_name, sub in list(self._subs.items()):
            try:
                sub.unsubscribe()
            except Exception:
                pass
            self._subs.pop(service_name, None)

    # ── Event-queue draining ──────────────────────────────────────────

    def _drain_event_queues(self) -> None:
        """Pull every queued event off every subscription, emit Qt
        signals, refresh the last-event timestamp. Cheap when idle —
        ``events.get(timeout=0)`` returns immediately."""
        if self._stopped:
            return
        for service_name, sub in list(self._subs.items()):
            self._drain_one(service_name, sub)

    def _drain_one(self, service_name: str, sub: object) -> None:
        events_q = getattr(sub, "events", None)
        if events_q is None:
            return
        # Bound the per-tick drain so a busy speaker can't starve
        # the GUI thread. 16 events / 500 ms is way more headroom
        # than Sonos ever actually emits.
        for _ in range(16):
            try:
                evt = events_q.get(timeout=0)
            except Exception:
                # Queue empty (the soco ``Empty`` exception) — fall
                # through to the next service.
                break
            variables = dict(getattr(evt, "variables", {}) or {})
            self._last_event_ts[service_name] = time.monotonic()
            self.event_received.emit(service_name, variables)
            if service_name == "avTransport":
                state = variables.get("transport_state")
                if state:
                    self.transport_state.emit(str(state))

    # ── Watchdog: detect + recover from a dead subscription ───────────

    def _renew_if_stale(self) -> None:
        """For each service: if no event in ``SILENCE_LIMIT_S`` seconds
        AND the subscription claims it's not subscribed, force a
        re-subscribe. The doubled condition avoids needless churn on
        a genuinely-idle speaker."""
        if self._stopped:
            return
        now = time.monotonic()
        for service_name, sub in list(self._subs.items()):
            last = self._last_event_ts.get(service_name, now)
            silent_for = now - last
            is_subscribed = bool(getattr(sub, "is_subscribed", True))
            # Force-renew if either the SOAP subscription says it's
            # dead OR we've been silent past the auto-renew + grace
            # window.
            if (not is_subscribed) or silent_for > self.SILENCE_LIMIT_S:
                self._force_renew(service_name)

    def _force_renew(self, service_name: str) -> None:
        sub = self._subs.pop(service_name, None)
        if sub is not None:
            try:
                sub.unsubscribe()
            except Exception:
                pass
        if not self._subscribe(service_name):
            self.subscription_failed.emit(service_name)


# ── Convenience wrappers expected by the autonomous-task spec ──────────────


def discover_async(
    timeout: float = 1.0,
    on_result: Optional[Callable[[List[SonosZone]], None]] = None,
    on_error: Optional[Callable[[Exception], None]] = None,
) -> None:
    """Non-blocking ``discover_sonos``. Fires ``on_result(list)`` on the
    GUI thread. Mirrors the ``run_async`` convention every other cast
    backend uses."""
    from modules.async_io import run_async

    def _go() -> List[SonosZone]:
        return discover_sonos(timeout=timeout)

    run_async(_go, on_result=on_result, on_error=on_error)


def cast_async(
    zone_or_player: object,
    url: str,
    title: str = "",
    artist: str = "",
    album: str = "",
    art_url: str = "",
    on_done: Optional[Callable[[bool], None]] = None,
) -> None:
    """Non-blocking ``cast_to_sonos``."""
    from modules.async_io import run_async

    def _go() -> bool:
        return cast_to_sonos(
            zone_or_player,
            url,
            title=title,
            artist=artist,
            album=album,
            art_url=art_url,
        )

    def _ok(ok: bool) -> None:
        if on_done:
            on_done(bool(ok))

    def _err(_e: Exception) -> None:
        if on_done:
            on_done(False)

    run_async(_go, on_result=_ok, on_error=_err)


__all__ = [
    "SonosZone",
    "SonosEventBridge",
    "build_didl",
    "cast_async",
    "cast_to_sonos",
    "discover_async",
    "discover_sonos",
    "expand_topology",
    "is_available",
    "join_group",
    "members_for_zone",
    "pause_sonos",
    "play_sonos",
    "resolve_coordinator",
    "seek_sonos",
    "set_volume",
    "stop_sonos",
    "unjoin",
]
