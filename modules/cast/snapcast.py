"""Snapcast control surface (A24 — Option B).

v1 ships the *control* half of the Snapcast integration: discover
snapservers on the LAN via mDNS, connect to one over JSON-RPC :1705,
expose the {groups, clients, streams} object graph, and let the user
set per-client volume / mute, regroup clients, and switch a group's
source stream.

Audio routing (Option A — mpv → snapserver pipe) is intentionally
deferred to v1.5 with august's eyes. Per the research doc, Option B
is competitively distinctive on its own and has zero downside for the
~95% of users without a snapserver: when ``snapcast_enabled`` is True
but no server is discovered, the controller stays idle.

Design notes:

- The upstream ``snapcast`` package is asyncio-native. We run a private
  asyncio event loop on a dedicated worker thread so the GUI thread
  never blocks on RPC; cross-thread fan-in is done via PlayerBus
  signals (Qt's queued connections marshal them onto the main thread).
  This mirrors the pattern in the DLNA backend research doc.
- Discovery uses the existing ``zeroconf`` dep (sync ``ServiceBrowser``
  on a daemon thread) for symmetry with ``modules/cast_manager.py``'s
  AirPlay scan path. Switching to ``AsyncZeroconfServiceBrowser`` is a
  v2 refinement; for v1 we want the discovery and controller threads
  decoupled.
- The PlayerBus signals are deliberately separate from ``cast_*``; see
  ``docs/research/casting_snapcast.md`` §10 for the rationale.

Caller surface — all coroutine-free; the controller handles the
asyncio bridging:

    ctl = SnapcastController()
    ctl.discover()                 # populates servers list
    ctl.connect("snapserver.local", 1705)
    ctl.set_client_volume(cid, 60)
    ctl.set_client_mute(cid, True)
    ctl.set_group_stream(gid, sid)
    ctl.set_group_clients(gid, [cid_a, cid_b])
    ctl.pair_group(gid)            # remembers active group
    ctl.disconnect()
"""

from __future__ import annotations

import asyncio
import socket
import threading
from dataclasses import asdict, dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional

# Lazy imports — the snapcast / zeroconf deps shouldn't crash on
# module import in tests or on installs without them. The controller
# refuses to ``connect()`` if the snapcast package is missing.
_snapcast: Any = None
_snapcast_import_error: Optional[Exception] = None


def _ensure_snapcast() -> bool:
    global _snapcast, _snapcast_import_error
    if _snapcast is None and _snapcast_import_error is None:
        try:
            import snapcast.control as _sc  # type: ignore[import-not-found]

            _snapcast = _sc
        except Exception as e:  # noqa: BLE001  — any import error counts
            _snapcast_import_error = e
            return False
    return _snapcast is not None


_zeroconf: Any = None
_ServiceBrowser: Any = None
_zeroconf_import_error: Optional[Exception] = None


def _ensure_zeroconf() -> bool:
    global _zeroconf, _ServiceBrowser, _zeroconf_import_error
    if _zeroconf is None and _zeroconf_import_error is None:
        try:
            from zeroconf import ServiceBrowser as _Sb
            from zeroconf import Zeroconf as _Zc

            _zeroconf = _Zc
            _ServiceBrowser = _Sb
        except Exception as e:  # noqa: BLE001
            _zeroconf_import_error = e
            return False
    return _zeroconf is not None


# RFC 6763 service type. One byte over the 15-byte limit — the snapcast
# upstream issue #243 — but zeroconf>=0.80 accepts it just fine.
SNAPCAST_SERVICE_TYPE = "_snapcast-jsonrpc._tcp.local."
DEFAULT_CONTROL_PORT = 1705
DISCOVERY_TIMEOUT_S = 3.0


# ── Dataclasses ─────────────────────────────────────────────────────────────


@dataclass
class SnapcastServerInfo:
    """A snapserver discovered on the LAN (or manually added).

    Not the live connection — ``SnapcastController`` opens that on
    demand. Kept as a plain dataclass so it can round-trip through
    QSettings + the PlayerBus signal payload.
    """

    host: str
    port: int = DEFAULT_CONTROL_PORT
    hostname: str = ""
    version: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class SnapcastClientState:
    """Snapshot of a Snapcast client at the time of the last sync."""

    id: str
    name: str
    connected: bool = False
    volume: int = 0  # 0-100
    muted: bool = False
    group_id: str = ""
    latency: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class SnapcastGroupState:
    id: str
    name: str
    stream_id: str = ""
    muted: bool = False
    client_ids: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class SnapcastStreamState:
    id: str
    name: str
    status: str = ""  # "playing" | "idle" | "disabled"
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ── Discovery ───────────────────────────────────────────────────────────────


class _SnapcastDiscoveryListener:
    """zeroconf ServiceBrowser listener that collates discovered
    snapservers into a host-keyed dict. Owns no state besides the
    dict + the callback; the controller holds it for the lifetime of
    a browse session."""

    def __init__(self, callback: Callable[[List[SnapcastServerInfo]], None]):
        self._callback = callback
        self._servers: Dict[str, SnapcastServerInfo] = {}

    @property
    def servers(self) -> List[SnapcastServerInfo]:
        return list(self._servers.values())

    def _resolve(self, zc, type_, name) -> Optional[SnapcastServerInfo]:
        info = zc.get_service_info(type_, name)
        if info is None or not info.addresses:
            return None
        try:
            host = socket.inet_ntoa(info.addresses[0])
        except (OSError, ValueError):
            return None
        hostname = info.server.rstrip(".") if getattr(info, "server", None) else ""
        # TXT records optionally carry a version; the snapserver tree
        # doesn't standardize this so we read defensively.
        version = ""
        props = getattr(info, "properties", None) or {}
        for k in (b"version", "version"):
            v = props.get(k)
            if v:
                version = v.decode() if isinstance(v, bytes) else str(v)
                break
        return SnapcastServerInfo(
            host=host,
            port=info.port or DEFAULT_CONTROL_PORT,
            hostname=hostname,
            version=version,
        )

    def add_service(self, zc, type_, name):
        srv = self._resolve(zc, type_, name)
        if srv is not None:
            self._servers[name] = srv
            self._callback(self.servers)

    def remove_service(self, zc, type_, name):
        if self._servers.pop(name, None) is not None:
            self._callback(self.servers)

    def update_service(self, zc, type_, name):
        self.add_service(zc, type_, name)


def discover_servers(
    on_result: Callable[[List[SnapcastServerInfo]], None],
    timeout: float = DISCOVERY_TIMEOUT_S,
) -> None:
    """Kick off an mDNS browse for snapservers. ``on_result`` fires on
    the GUI thread once per change (add / remove / update). The browse
    runs for ``timeout`` seconds then tears down; callers re-arm if
    they want continuous discovery."""
    if not _ensure_zeroconf():
        on_result([])
        return

    def _go() -> None:
        zc = _zeroconf()
        listener = _SnapcastDiscoveryListener(lambda _: None)
        try:
            _ServiceBrowser(zc, SNAPCAST_SERVICE_TYPE, listener)
            # zeroconf's add_service callbacks land on its own thread —
            # we sleep on the worker so they have a chance to run. The
            # browse is one-shot per-call; the listener's dict is the
            # full result.
            import time as _t

            _t.sleep(max(0.0, float(timeout)))
        finally:
            try:
                zc.close()
            except Exception:
                pass
        try:
            on_result(listener.servers)
        except Exception:
            pass

    # Documented exception to [[feedback-async-io-pattern]] (the "use
    # modules.async_io, not raw threads" rule). The general pattern routes
    # blocking work via `run_async`, which marshals results back to the GUI
    # thread through Qt queued signals — that requires a running
    # QApplication event loop. The snapcast discovery tests run without
    # one (the bus fixture is a fake QObject with manual `.emit()` capture,
    # not a real event loop), so queued signal dispatch would deadlock
    # the test. Discovery fires ~once per cast-menu open and runs for
    # at most `timeout` seconds; a single daemon thread is the
    # lowest-friction way to keep both the production path and the unit
    # tests green. The `_AsyncLoopThread` below is the *other* documented
    # exception in this file (an asyncio loop hosting snapcast's JSON-RPC).
    t = threading.Thread(target=_go, name="snapcast-discovery", daemon=True)
    t.start()


# ── Async loop on a worker thread ───────────────────────────────────────────


class _AsyncLoopThread:
    """A dedicated asyncio loop running on a daemon thread.

    Exists so the snapcast package (asyncio-only) and the Qt GUI thread
    (qt event loop) can coexist without rewriting the whole app under
    qasync. ``submit(coro)`` schedules a coroutine onto the loop and
    returns a ``concurrent.futures.Future`` the caller can wait on (or
    ignore). Loop is started lazily on first ``submit``.
    """

    def __init__(self) -> None:
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._started = threading.Event()
        self._lock = threading.Lock()

    def _run(self):
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        self._started.set()
        try:
            loop.run_forever()
        finally:
            try:
                loop.close()
            except Exception:
                pass

    def _ensure_started(self) -> None:
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._started.clear()
                self._thread = threading.Thread(
                    target=self._run,
                    name="snapcast-asyncio",
                    daemon=True,
                )
                self._thread.start()
                # Block briefly so callers don't race a not-yet-ready
                # loop on first use; the loop creates in microseconds.
                self._started.wait(timeout=2.0)

    def submit(self, coro: Awaitable[Any]):
        self._ensure_started()
        if self._loop is None:
            raise RuntimeError("event loop did not start")
        return asyncio.run_coroutine_threadsafe(coro, self._loop)

    def call_soon(self, fn: Callable[[], Any]) -> bool:
        """Schedule a plain (non-coroutine) callable on the loop thread.
        Returns ``False`` if the loop isn't running so the caller can
        pick a fallback. Used to close asyncio transports from their
        owning thread instead of the GUI thread."""
        loop = self._loop
        if loop is None or loop.is_closed():
            return False
        try:
            loop.call_soon_threadsafe(fn)
            return True
        except RuntimeError:
            return False

    def stop(self) -> None:
        loop = self._loop
        if loop is None:
            return
        try:
            loop.call_soon_threadsafe(loop.stop)
        except Exception:
            pass
        t = self._thread
        if t is not None:
            t.join(timeout=2.0)
        self._loop = None
        self._thread = None


# ── Controller ──────────────────────────────────────────────────────────────


class SnapcastController:
    """High-level Snapcast control surface.

    The controller wraps a single connection to one snapserver. Switch
    servers via ``disconnect()`` + ``connect(new_host)``. Discovery is
    a static helper (``discover_servers``) — the controller doesn't
    auto-browse, since UIs vary in how they want to surface that.

    Signal fan-out goes through ``PlayerBus`` so consumers don't have to
    register direct callbacks; tests can subscribe to the signals and
    assert on payloads. Constructor takes an optional ``bus`` param so
    tests can hand in a fake without coupling to the singleton.
    """

    def __init__(self, bus: Any = None) -> None:
        from modules.player_state import PlayerBus

        self._bus = bus if bus is not None else PlayerBus.get()
        self._loop = _AsyncLoopThread()
        self._server: Any = None  # snapcast.control.Snapserver
        self._info: Optional[SnapcastServerInfo] = None
        self._active_group_id: str = ""
        self._connected: bool = False
        # Discovery scratch — exposed so the cast popup can re-fire the
        # list without re-browsing on every paint.
        self._servers: List[SnapcastServerInfo] = []

    # ── Discovery ──────────────────────────────────────────────────────────

    @property
    def servers(self) -> List[SnapcastServerInfo]:
        return list(self._servers)

    def discover(self, timeout: float = DISCOVERY_TIMEOUT_S) -> None:
        """Async mDNS browse. Emits ``snapcast_servers_changed`` with the
        final list (cumulative, since add/remove during a single browse
        is rare in practice). Failure (zeroconf missing, no
        snapservers) emits an empty list — same shape as success."""

        def _on(srvs: List[SnapcastServerInfo]) -> None:
            self._servers = list(srvs)
            self._emit_servers_changed()

        discover_servers(_on, timeout=timeout)

    def add_manual_server(
        self, host: str, port: int = DEFAULT_CONTROL_PORT, hostname: str = ""
    ) -> SnapcastServerInfo:
        """Register a snapserver by host:port for mDNS-firewalled
        networks. Appended to ``servers`` and the ``servers_changed``
        signal fires. Doesn't connect — the caller still has to
        ``connect()`` to it explicitly."""
        host = (host or "").strip()
        if not host:
            raise ValueError("host is required")
        info = SnapcastServerInfo(host=host, port=int(port), hostname=hostname or host)
        # De-dupe by (host, port) so a re-add doesn't double the list.
        self._servers = [
            s for s in self._servers if not (s.host == info.host and s.port == info.port)
        ]
        self._servers.append(info)
        self._emit_servers_changed()
        return info

    # ── Connection ─────────────────────────────────────────────────────────

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def server_info(self) -> Optional[SnapcastServerInfo]:
        return self._info

    @property
    def active_group_id(self) -> str:
        return self._active_group_id

    def connect(
        self,
        host: str,
        port: int = DEFAULT_CONTROL_PORT,
        reconnect: bool = True,
        on_done: Optional[Callable[[bool], None]] = None,
    ) -> None:
        """Open a JSON-RPC session against ``host:port``. Idempotent —
        a second call disconnects the previous server first.
        ``on_done(ok)`` is fired on the GUI thread once the initial
        ``Server.GetStatus`` arrives (or the connect fails)."""
        if not _ensure_snapcast():
            self._emit_error(f"snapcast package not installed: {_snapcast_import_error!r}")
            if on_done:
                on_done(False)
            return

        async def _go() -> bool:
            # Tear down any existing connection first.
            if self._server is not None:
                try:
                    self._server.stop()
                except Exception:
                    pass
                self._server = None

            loop = asyncio.get_running_loop()
            server = _snapcast.Snapserver(
                loop,
                host,
                port,
                reconnect=reconnect,
            )
            server.set_on_update_callback(self._on_server_update)
            server.set_on_connect_callback(self._on_server_connect)
            server.set_on_disconnect_callback(self._on_server_disconnect)
            server.set_new_client_callback(self._on_new_client)
            try:
                await server.start()
            except Exception as e:  # noqa: BLE001
                self._emit_error(f"snapcast connect failed: {e!r}")
                return False
            self._server = server
            self._info = SnapcastServerInfo(
                host=host,
                port=port,
                hostname=host,
                version=getattr(server, "version", "") or "",
            )
            self._connected = True
            self._emit_connection(True)
            self._wire_object_callbacks()
            self._emit_state_changed()
            return True

        fut = self._loop.submit(_go())

        def _resolve(f) -> None:
            try:
                ok = bool(f.result())
            except Exception as e:  # noqa: BLE001
                self._emit_error(f"snapcast connect crashed: {e!r}")
                ok = False
            if on_done:
                # add_done_callback fires on the asyncio loop thread;
                # marshal on_done onto the GUI thread so the caller can
                # safely touch widgets (the cast dialog rebuilds in it).
                from modules.async_io import call_on_gui

                call_on_gui(lambda: on_done(ok))

        fut.add_done_callback(_resolve)

    def disconnect(self) -> None:
        """Tear down the current Snapcast session. Idempotent.

        ``Snapserver.stop()`` closes the asyncio transport, which is
        owned by the worker event loop. Closing a transport from a
        foreign thread races the loop's selector callbacks, so we
        schedule ``stop()`` ON the loop thread via ``call_soon`` rather
        than calling it directly from the GUI thread. If the loop is
        already stopped we fall back to a direct call as a best effort."""
        srv = self._server
        if srv is not None:
            if not self._loop.call_soon(srv.stop):
                try:
                    srv.stop()
                except Exception:
                    pass
        self._server = None
        self._info = None
        self._connected = False
        self._active_group_id = ""
        self._emit_connection(False)

    # ── Reads ──────────────────────────────────────────────────────────────

    def list_clients(self) -> List[SnapcastClientState]:
        if self._server is None:
            return []
        return [_client_snapshot(c) for c in self._server.clients]

    def list_groups(self) -> List[SnapcastGroupState]:
        if self._server is None:
            return []
        return [_group_snapshot(g) for g in self._server.groups]

    def list_streams(self) -> List[SnapcastStreamState]:
        if self._server is None:
            return []
        return [_stream_snapshot(s) for s in self._server.streams]

    def snapshot(self) -> Dict[str, Any]:
        """Full {groups, clients, streams} snapshot — the payload of
        ``snapcast_state_changed``. Empty dicts when disconnected."""
        return {
            "connected": self._connected,
            "server": self._info.to_dict() if self._info else None,
            "active_group_id": self._active_group_id,
            "clients": [c.to_dict() for c in self.list_clients()],
            "groups": [g.to_dict() for g in self.list_groups()],
            "streams": [s.to_dict() for s in self.list_streams()],
        }

    # ── Writes ─────────────────────────────────────────────────────────────

    def set_client_volume(
        self, client_id: str, percent: int, on_done: Optional[Callable[[bool], None]] = None
    ) -> None:
        """Set per-client volume (0-100). Clamps out-of-range. Mute
        state is preserved server-side."""
        if self._server is None:
            if on_done:
                on_done(False)
            return
        pct = max(0, min(100, int(percent)))
        try:
            client = self._server.client(client_id)
        except Exception:
            self._emit_error(f"snapcast: unknown client {client_id!r}")
            if on_done:
                on_done(False)
            return

        async def _go() -> bool:
            try:
                await client.set_volume(pct)
                return True
            except Exception as e:  # noqa: BLE001
                self._emit_error(f"snapcast set_client_volume failed: {e!r}")
                return False

        self._dispatch(_go(), on_done)

    def set_client_mute(
        self, client_id: str, muted: bool, on_done: Optional[Callable[[bool], None]] = None
    ) -> None:
        if self._server is None:
            if on_done:
                on_done(False)
            return
        try:
            client = self._server.client(client_id)
        except Exception:
            self._emit_error(f"snapcast: unknown client {client_id!r}")
            if on_done:
                on_done(False)
            return

        async def _go() -> bool:
            try:
                await client.set_muted(bool(muted))
                return True
            except Exception as e:  # noqa: BLE001
                self._emit_error(f"snapcast set_client_mute failed: {e!r}")
                return False

        self._dispatch(_go(), on_done)

    def set_client_name(
        self, client_id: str, name: str, on_done: Optional[Callable[[bool], None]] = None
    ) -> None:
        if self._server is None:
            if on_done:
                on_done(False)
            return
        try:
            client = self._server.client(client_id)
        except Exception:
            self._emit_error(f"snapcast: unknown client {client_id!r}")
            if on_done:
                on_done(False)
            return

        async def _go() -> bool:
            try:
                await client.set_name(name or "")
                return True
            except Exception as e:  # noqa: BLE001
                self._emit_error(f"snapcast set_client_name failed: {e!r}")
                return False

        self._dispatch(_go(), on_done)

    def set_group_stream(
        self, group_id: str, stream_id: str, on_done: Optional[Callable[[bool], None]] = None
    ) -> None:
        """Switch a group's source stream. Snapserver's buffer means
        the audio lag is ~1 s; the bus signal fires immediately."""
        if self._server is None:
            if on_done:
                on_done(False)
            return
        try:
            group = self._server.group(group_id)
        except Exception:
            self._emit_error(f"snapcast: unknown group {group_id!r}")
            if on_done:
                on_done(False)
            return

        async def _go() -> bool:
            try:
                await group.set_stream(stream_id)
                return True
            except Exception as e:  # noqa: BLE001
                self._emit_error(f"snapcast set_group_stream failed: {e!r}")
                return False

        self._dispatch(_go(), on_done)

    def set_group_mute(
        self, group_id: str, muted: bool, on_done: Optional[Callable[[bool], None]] = None
    ) -> None:
        if self._server is None:
            if on_done:
                on_done(False)
            return
        try:
            group = self._server.group(group_id)
        except Exception:
            self._emit_error(f"snapcast: unknown group {group_id!r}")
            if on_done:
                on_done(False)
            return

        async def _go() -> bool:
            try:
                await group.set_muted(bool(muted))
                return True
            except Exception as e:  # noqa: BLE001
                self._emit_error(f"snapcast set_group_mute failed: {e!r}")
                return False

        self._dispatch(_go(), on_done)

    def set_group_name(
        self, group_id: str, name: str, on_done: Optional[Callable[[bool], None]] = None
    ) -> None:
        if self._server is None:
            if on_done:
                on_done(False)
            return
        try:
            group = self._server.group(group_id)
        except Exception:
            self._emit_error(f"snapcast: unknown group {group_id!r}")
            if on_done:
                on_done(False)
            return

        async def _go() -> bool:
            try:
                await group.set_name(name or "")
                return True
            except Exception as e:  # noqa: BLE001
                self._emit_error(f"snapcast set_group_name failed: {e!r}")
                return False

        self._dispatch(_go(), on_done)

    def set_group_clients(
        self, group_id: str, client_ids: List[str], on_done: Optional[Callable[[bool], None]] = None
    ) -> None:
        """Replace the set of clients in a group. The snapserver
        re-shuffles other groups as needed (clients can only belong to
        one group at a time)."""
        if self._server is None:
            if on_done:
                on_done(False)
            return

        async def _go() -> bool:
            try:
                # Calling Server.group_clients directly avoids
                # depending on the per-Group helper, which calls
                # status() internally and broadens the failure surface.
                await self._server.group_clients(group_id, list(client_ids or []))
                # Refresh after a regroup so the snapshot matches
                # snapserver's view.
                status, _err = await self._server.status()
                if isinstance(status, dict):
                    self._server.synchronize(status)
                return True
            except Exception as e:  # noqa: BLE001
                self._emit_error(f"snapcast set_group_clients failed: {e!r}")
                return False

        def _after(ok: bool) -> None:
            if ok:
                self._emit_state_changed()
            if on_done:
                on_done(ok)

        self._dispatch(_go(), _after)

    def add_client_to_group(
        self, group_id: str, client_id: str, on_done: Optional[Callable[[bool], None]] = None
    ) -> None:
        try:
            current = list(self._server.group(group_id).clients)
        except Exception:
            current = []
        if client_id and client_id not in current:
            current.append(client_id)
        self.set_group_clients(group_id, current, on_done)

    def remove_client_from_group(
        self,
        group_id: str,
        client_id: str,
        on_done: Optional[Callable[[bool], None]] = None,
    ) -> None:
        try:
            current = list(self._server.group(group_id).clients)
        except Exception:
            current = []
        current = [c for c in current if c != client_id]
        self.set_group_clients(group_id, current, on_done)

    def pair_group(self, group_id: str) -> None:
        """Mark a group as the user's "casting target". Doesn't change
        snapserver state — just persists locally + emits the signal so
        the popup can render the new selection."""
        if group_id == self._active_group_id:
            return
        self._active_group_id = group_id or ""
        self._bus.snapcast_active_group_changed.emit(self._active_group_id)

    def unpair_group(self) -> None:
        self.pair_group("")

    def refresh(self, on_done: Optional[Callable[[bool], None]] = None) -> None:
        """Force a ``Server.GetStatus`` resync. Useful after a network
        blip when we don't yet know the snapserver caught back up."""
        if self._server is None:
            if on_done:
                on_done(False)
            return

        async def _go() -> bool:
            try:
                status, _err = await self._server.status()
                if isinstance(status, dict):
                    self._server.synchronize(status)
                    return True
                return False
            except Exception as e:  # noqa: BLE001
                self._emit_error(f"snapcast refresh failed: {e!r}")
                return False

        def _after(ok: bool) -> None:
            if ok:
                self._emit_state_changed()
            if on_done:
                on_done(ok)

        self._dispatch(_go(), _after)

    # ── Internals ──────────────────────────────────────────────────────────

    def _dispatch(self, coro: Awaitable[bool], on_done: Optional[Callable[[bool], None]]) -> None:
        """Submit ``coro`` to the asyncio loop and fire ``on_done(bool)``
        when it resolves. Used by all write methods so they share one
        error-handling spine.

        Cross-thread story: the asyncio future resolves on the loop
        thread; the ``done_callback`` marshals ``on_done`` onto the GUI
        thread via ``call_on_gui`` so callers may safely touch widgets.
        """
        try:
            fut = self._loop.submit(coro)
        except Exception as e:  # noqa: BLE001
            self._emit_error(f"snapcast dispatch failed: {e!r}")
            if on_done:
                on_done(False)
            return

        def _resolve(f) -> None:
            try:
                ok = bool(f.result())
            except Exception as e:  # noqa: BLE001
                self._emit_error(f"snapcast op crashed: {e!r}")
                ok = False
            if on_done is not None:
                from modules.async_io import call_on_gui

                call_on_gui(lambda: on_done(ok))

        fut.add_done_callback(_resolve)

    def _wire_object_callbacks(self) -> None:
        """Hook per-client / per-group / per-stream callbacks so we
        can emit fine-grained signals as the snapserver reports
        changes. Snapcast fires these from its asyncio loop; the Qt
        signals trampoline onto the GUI thread via queued connection."""
        if self._server is None:
            return
        for c in self._server.clients:
            c.set_callback(self._on_client_callback)
        for g in self._server.groups:
            g.set_callback(self._on_group_callback)
        for s in self._server.streams:
            s.set_callback(self._on_stream_callback)

    # ── Snapcast lib callbacks ─────────────────────────────────────────────

    def _on_server_connect(self) -> None:
        self._connected = True
        self._emit_connection(True)

    def _on_server_disconnect(self, exception: Any = None) -> None:
        self._connected = False
        self._emit_connection(False)
        if exception is not None:
            self._emit_error(f"snapcast disconnected: {exception!r}")

    def _on_server_update(self) -> None:
        # Full resync — rewire callbacks because synchronize() can
        # replace per-object instances when new clients/groups appear.
        self._wire_object_callbacks()
        self._emit_state_changed()

    def _on_new_client(self, client: Any) -> None:
        try:
            client.set_callback(self._on_client_callback)
        except Exception:
            pass
        self._emit_state_changed()

    def _on_client_callback(self, client: Any) -> None:
        try:
            cid = client.identifier
            pct = int(client.volume or 0)
            muted = bool(client.muted)
            self._bus.snapcast_client_volume_changed.emit(cid, pct, muted)
        except Exception:
            pass
        self._emit_clients_changed()

    def _on_group_callback(self, group: Any) -> None:
        try:
            gid = group.identifier
            self._bus.snapcast_group_changed.emit(gid)
        except Exception:
            pass
        self._emit_groups_changed()

    def _on_stream_callback(self, _stream: Any) -> None:
        self._emit_streams_changed()

    # ── Bus emission helpers ───────────────────────────────────────────────

    def _emit_servers_changed(self) -> None:
        self._bus.snapcast_servers_changed.emit([s.to_dict() for s in self._servers])

    def _emit_state_changed(self) -> None:
        snap = self.snapshot()
        self._bus.snapcast_state_changed.emit(snap)
        self._bus.snapcast_clients_changed.emit(snap["clients"])
        self._bus.snapcast_groups_changed.emit(snap["groups"])
        self._bus.snapcast_streams_changed.emit(snap["streams"])

    def _emit_clients_changed(self) -> None:
        self._bus.snapcast_clients_changed.emit([c.to_dict() for c in self.list_clients()])

    def _emit_groups_changed(self) -> None:
        self._bus.snapcast_groups_changed.emit([g.to_dict() for g in self.list_groups()])

    def _emit_streams_changed(self) -> None:
        self._bus.snapcast_streams_changed.emit([s.to_dict() for s in self.list_streams()])

    def _emit_connection(self, connected: bool) -> None:
        self._bus.snapcast_connection_changed.emit(bool(connected))

    def _emit_error(self, msg: str) -> None:
        self._bus.snapcast_error.emit(str(msg))

    # ── Shutdown ───────────────────────────────────────────────────────────

    def shutdown(self) -> None:
        """Tear down the controller — disconnects the snapserver, stops
        the asyncio loop thread. Safe to call multiple times."""
        try:
            self.disconnect()
        except Exception:
            pass
        try:
            self._loop.stop()
        except Exception:
            pass


# ── Snapshot helpers (also handy in tests) ──────────────────────────────────


def _client_snapshot(c: Any) -> SnapcastClientState:
    """Translate a ``snapcast.control.Snapclient`` to a plain
    dataclass. Tolerates partial objects (raised attribute access just
    drops to defaults) so a half-synced state can't crash a snapshot."""

    def _safe(getter, default):
        try:
            v = getter()
            return v if v is not None else default
        except Exception:
            return default

    group = _safe(lambda: c.group, None)
    return SnapcastClientState(
        id=_safe(lambda: c.identifier, ""),
        name=_safe(lambda: c.friendly_name, "") or _safe(lambda: c.name, ""),
        connected=bool(_safe(lambda: c.connected, False)),
        volume=int(_safe(lambda: c.volume, 0) or 0),
        muted=bool(_safe(lambda: c.muted, False)),
        group_id=_safe(lambda: group.identifier if group else "", ""),
        latency=int(_safe(lambda: c.latency, 0) or 0),
    )


def _group_snapshot(g: Any) -> SnapcastGroupState:
    def _safe(getter, default):
        try:
            v = getter()
            return v if v is not None else default
        except Exception:
            return default

    return SnapcastGroupState(
        id=_safe(lambda: g.identifier, ""),
        name=_safe(lambda: g.friendly_name, "") or _safe(lambda: g.name, ""),
        stream_id=_safe(lambda: g.stream, "") or "",
        muted=bool(_safe(lambda: g.muted, False)),
        client_ids=list(_safe(lambda: g.clients, []) or []),
    )


def _stream_snapshot(s: Any) -> SnapcastStreamState:
    def _safe(getter, default):
        try:
            v = getter()
            return v if v is not None else default
        except Exception:
            return default

    return SnapcastStreamState(
        id=_safe(lambda: s.identifier, ""),
        name=_safe(lambda: s.friendly_name, "") or _safe(lambda: s.name, ""),
        status=_safe(lambda: s.status, "") or "",
        metadata=dict(_safe(lambda: s.metadata, {}) or {}),
    )


# ── Module singleton ─────────────────────────────────────────────────────────

_CONTROLLER: Optional[SnapcastController] = None


def get_snapcast_controller() -> SnapcastController:
    """Process-wide ``SnapcastController`` singleton — mirrors
    ``cast.dlna.get_dlna_controller``. The cast dialog's Snapcast pick and
    the control popup share one connection (one snapserver at a time)."""
    global _CONTROLLER
    if _CONTROLLER is None:
        _CONTROLLER = SnapcastController()
    return _CONTROLLER
