"""Tests for ``jellytoast.cast.snapcast``.

Strategy:

- The upstream ``snapcast`` package is asyncio-only and talks raw TCP
  JSON-RPC. Rather than spin a real snapserver, we stub
  ``snapcast.control.Snapserver`` (and its child ``Snapclient`` /
  ``Snapgroup`` / ``Snapstream`` types) with in-memory fakes so the
  controller's wiring + signal fan-out is the unit under test.
- The controller hosts a private asyncio loop on a worker thread; we
  rely on ``asyncio.run_coroutine_threadsafe`` returning a real Future
  so write methods can run in the test process unmodified. The fake
  Snapserver's coroutines actually ``await`` so the loop has work.
- Discovery is patched via ``zeroconf.ServiceBrowser`` so we never
  open a real socket.

PlayerBus signals are observed via a tiny ``_SigCapture`` helper that
patches the bus instance with plain Python callables (PySide6 signals
take some setup to construct standalone; for test purposes a list-
appending callable is functionally identical).
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional

import pytest

from jellytoast.cast import snapcast as sc
from jellytoast.player_state import PlayerBus

# ── Fakes ──────────────────────────────────────────────────────────────────


class _FakeStream:
    def __init__(self, sid: str, name: str = "", status: str = "playing"):
        self.identifier = sid
        self.name = name or sid
        self.friendly_name = name or sid
        self.status = status
        self.metadata: Dict[str, Any] = {"title": f"track-{sid}"}
        self._cb: Optional[Callable] = None

    def set_callback(self, fn):
        self._cb = fn

    def fire(self):
        if self._cb:
            self._cb(self)


class _FakeClient:
    def __init__(
        self,
        cid: str,
        name: str = "",
        volume: int = 50,
        muted: bool = False,
        connected: bool = True,
        latency: int = 0,
        group: Any = None,
    ):
        self.identifier = cid
        self.name = name or cid
        self.friendly_name = name or cid
        self.volume = volume
        self.muted = muted
        self.connected = connected
        self.latency = latency
        self.group = group
        self._cb: Optional[Callable] = None

    def set_callback(self, fn):
        self._cb = fn

    async def set_volume(self, percent):
        await asyncio.sleep(0)
        self.volume = int(percent)
        if self._cb:
            self._cb(self)

    async def set_muted(self, status):
        await asyncio.sleep(0)
        self.muted = bool(status)
        if self._cb:
            self._cb(self)

    async def set_name(self, name):
        await asyncio.sleep(0)
        self.name = self.friendly_name = name
        if self._cb:
            self._cb(self)


class _FakeGroup:
    def __init__(
        self,
        gid: str,
        name: str = "",
        clients: List[str] = None,
        stream: str = "",
        muted: bool = False,
        server: Any = None,
    ):
        self.identifier = gid
        self.name = name or gid
        self.friendly_name = name or gid
        self.clients = list(clients or [])
        self.stream = stream
        self.muted = muted
        self._server = server
        self._cb: Optional[Callable] = None

    def set_callback(self, fn):
        self._cb = fn

    async def set_stream(self, stream_id):
        await asyncio.sleep(0)
        self.stream = stream_id
        if self._cb:
            self._cb(self)

    async def set_muted(self, status):
        await asyncio.sleep(0)
        self.muted = bool(status)
        if self._cb:
            self._cb(self)

    async def set_name(self, name):
        await asyncio.sleep(0)
        self.name = self.friendly_name = name
        if self._cb:
            self._cb(self)


class _FakeSnapserver:
    """Minimal in-memory snapcast.control.Snapserver replacement.

    Tracks one server's groups / clients / streams as plain fakes;
    coroutines actually await so the controller's loop drives.
    """

    def __init__(self, loop, host, port=1705, reconnect=False):
        self._loop = loop
        self._host = host
        self._port = port
        self._reconnect = reconnect
        self.version = "0.27.0"
        self._clients: Dict[str, _FakeClient] = {}
        self._groups: Dict[str, _FakeGroup] = {}
        self._streams: Dict[str, _FakeStream] = {}
        self._on_update_cb = None
        self._on_connect_cb = None
        self._on_disconnect_cb = None
        self._on_new_client_cb = None
        # Default population — one stream, one group, two clients.
        self._streams["default"] = _FakeStream("default", "Default")
        self._streams["jellytoast"] = _FakeStream("jellytoast", "jellytoast")
        c1 = _FakeClient("clientA", "Couch Pi", volume=40)
        c2 = _FakeClient("clientB", "Kitchen Pi", volume=70)
        self._clients["clientA"] = c1
        self._clients["clientB"] = c2
        g = _FakeGroup(
            "groupX", "Living Room", clients=["clientA", "clientB"], stream="default", server=self
        )
        c1.group = g
        c2.group = g
        self._groups["groupX"] = g
        # ``start`` failure simulation.
        self.should_fail_start = False

    # ── Lifecycle ──────────────────────────────────────────────────────
    async def start(self):
        await asyncio.sleep(0)
        if self.should_fail_start:
            raise OSError("simulated connect failure")
        if self._on_connect_cb:
            self._on_connect_cb()

    def stop(self):
        if self._on_disconnect_cb:
            self._on_disconnect_cb(None)

    # ── Status ─────────────────────────────────────────────────────────
    async def status(self):
        await asyncio.sleep(0)
        return ({"server": {"server": {"snapserver": {"version": self.version}}}}, None)

    # ── Object accessors ──────────────────────────────────────────────
    def client(self, cid):
        return self._clients[cid]

    def group(self, gid):
        return self._groups[gid]

    def stream(self, sid):
        return self._streams[sid]

    @property
    def clients(self):
        return list(self._clients.values())

    @property
    def groups(self):
        return list(self._groups.values())

    @property
    def streams(self):
        return list(self._streams.values())

    # ── Server-level writes the controller calls directly ──────────────
    async def group_clients(self, gid, client_ids):
        await asyncio.sleep(0)
        g = self._groups[gid]
        g.clients = list(client_ids)
        if g._cb:
            g._cb(g)
        return client_ids

    def synchronize(self, _status):
        # Test fake — already mutated state in place. Real impl would
        # rebuild dicts from the status payload.
        pass

    # ── Callback setters ──────────────────────────────────────────────
    def set_on_update_callback(self, fn):
        self._on_update_cb = fn

    def set_on_connect_callback(self, fn):
        self._on_connect_cb = fn

    def set_on_disconnect_callback(self, fn):
        self._on_disconnect_cb = fn

    def set_new_client_callback(self, fn):
        self._on_new_client_cb = fn

    # ── Test conveniences ─────────────────────────────────────────────
    def trigger_update(self):
        if self._on_update_cb:
            self._on_update_cb()


# ── Fake PlayerBus ──────────────────────────────────────────────────────────


class _SignalSink:
    """Replace a Qt Signal with a plain Python object exposing
    ``emit(...)`` + a captured-args list. Avoids needing a QApplication
    just to observe controller side effects."""

    def __init__(self) -> None:
        self.calls: List[tuple] = []

    def emit(self, *args):
        self.calls.append(args)

    def connect(self, fn):
        # Allow tests to optionally route emits straight to a callable.
        prev = self.emit

        def _emit_and_call(*args):
            prev(*args)
            try:
                fn(*args)
            except Exception:
                pass

        self.emit = _emit_and_call  # type: ignore[assignment]


class _FakeBus:
    def __init__(self) -> None:
        for name in (
            "snapcast_servers_changed",
            "snapcast_state_changed",
            "snapcast_clients_changed",
            "snapcast_groups_changed",
            "snapcast_streams_changed",
            "snapcast_client_volume_changed",
            "snapcast_group_changed",
            "snapcast_active_group_changed",
            "snapcast_connection_changed",
            "snapcast_error",
        ):
            setattr(self, name, _SignalSink())


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def fake_server_cls(monkeypatch):
    """Patch ``snapcast.control.Snapserver`` → ``_FakeSnapserver``."""
    # Make sure the lazy import has run, then swap the symbol.
    sc._ensure_snapcast()
    if sc._snapcast is None:  # pragma: no cover — only if dep missing
        pytest.skip("snapcast package not importable")
    monkeypatch.setattr(sc._snapcast, "Snapserver", _FakeSnapserver)
    return _FakeSnapserver


@pytest.fixture
def bus():
    return _FakeBus()


@pytest.fixture
def controller(bus, fake_server_cls):
    # Reap any stale Qt widgets left collectable by earlier tests BEFORE
    # spinning up the asyncio loop thread. Otherwise Python GC can dealloc
    # a leftover QAbstractItemView on the main thread *while* this loop
    # thread is live, and that cross-thread C++ teardown SIGSEGVs the
    # interpreter under some random test orders (the long-open Bug 2).
    import gc

    gc.collect()
    ctl = sc.SnapcastController(bus=bus)
    yield ctl
    ctl.shutdown()
    gc.collect()


def _wait_until(predicate, timeout=3.0, interval=0.02):
    """Poll until ``predicate()`` is truthy or ``timeout`` elapses.
    Used to await cross-thread side effects without sprinkling sleeps
    through individual tests.

    Pumps the Qt event loop when a QApplication exists so callbacks
    marshalled onto the GUI thread (``async_io.call_on_gui`` — how the
    controller now delivers ``on_done``) actually get dispatched. With
    no QApplication (this file run in isolation) ``call_on_gui`` runs
    inline, so ``processEvents`` is a harmless no-op."""
    from PySide6.QtWidgets import QApplication

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app = QApplication.instance()
        if app is not None:
            app.processEvents()
        if predicate():
            return True
        time.sleep(interval)
    return False


def _connect(controller) -> bool:
    done = {"ok": None}
    controller.connect("snapserver.test", 1705, on_done=lambda ok: done.__setitem__("ok", ok))
    assert _wait_until(lambda: done["ok"] is not None), "connect timed out"
    return bool(done["ok"])


# ── Dataclass tests ─────────────────────────────────────────────────────────


class TestDataclasses:
    def test_server_info_round_trip(self):
        info = sc.SnapcastServerInfo(
            host="10.0.0.5",
            port=1705,
            hostname="snap.lan",
            version="0.27.0",
        )
        d = info.to_dict()
        assert d == {"host": "10.0.0.5", "port": 1705, "hostname": "snap.lan", "version": "0.27.0"}

    def test_server_info_defaults(self):
        info = sc.SnapcastServerInfo(host="x")
        assert info.port == sc.DEFAULT_CONTROL_PORT
        assert info.hostname == ""
        assert info.version == ""

    def test_client_state_round_trip(self):
        c = sc.SnapcastClientState(
            id="cid", name="Pi", volume=42, muted=True, group_id="g", latency=10, connected=True
        )
        d = c.to_dict()
        assert d["volume"] == 42 and d["muted"] is True

    def test_group_state_defaults(self):
        g = sc.SnapcastGroupState(id="g", name="Living")
        assert g.client_ids == []
        assert g.muted is False


# ── Snapshot helpers (translation Snapcast → dataclass) ─────────────────────


class TestSnapshotHelpers:
    def test_client_snapshot(self):
        g = SimpleNamespace(identifier="g1")
        c = SimpleNamespace(
            identifier="c1",
            friendly_name="Couch",
            name="Couch",
            connected=True,
            volume=55,
            muted=False,
            latency=3,
            group=g,
        )
        s = sc._client_snapshot(c)
        assert s.id == "c1"
        assert s.name == "Couch"
        assert s.group_id == "g1"
        assert s.volume == 55

    def test_client_snapshot_tolerates_missing(self):
        c = SimpleNamespace()
        s = sc._client_snapshot(c)
        assert s.id == ""
        assert s.volume == 0
        assert s.group_id == ""

    def test_group_snapshot(self):
        g = SimpleNamespace(
            identifier="g1",
            friendly_name="Living",
            name="Living",
            stream="s1",
            muted=False,
            clients=["a", "b"],
        )
        snap = sc._group_snapshot(g)
        assert snap.id == "g1"
        assert snap.client_ids == ["a", "b"]
        assert snap.stream_id == "s1"

    def test_stream_snapshot(self):
        s = SimpleNamespace(
            identifier="default",
            friendly_name="Default",
            name="Default",
            status="playing",
            metadata={"title": "Hello"},
        )
        snap = sc._stream_snapshot(s)
        assert snap.id == "default"
        assert snap.status == "playing"
        assert snap.metadata == {"title": "Hello"}


# ── Discovery ───────────────────────────────────────────────────────────────


class TestDiscovery:
    def test_discovery_emits_empty_when_zeroconf_missing(self, monkeypatch, bus):
        # Force the lazy zeroconf import path to fail.
        monkeypatch.setattr(sc, "_zeroconf", None)
        monkeypatch.setattr(sc, "_zeroconf_import_error", ImportError("nope"))
        ctl = sc.SnapcastController(bus=bus)
        results = []
        ctl.discover()
        # ``run_async`` is called inside discover_servers; without a
        # QApplication the callback executes synchronously on the same
        # thread (the QThreadPool blocks waiting for emit, but since
        # we early-return in discover_servers when zeroconf is missing
        # the callback is fired immediately).
        # Wait for the servers_changed emit either way.
        if not _wait_until(lambda: bus.snapcast_servers_changed.calls, timeout=1.0):
            # zeroconf missing case fires on_result([]) synchronously
            # in main thread → bus already has []
            pass
        ctl.shutdown()
        # Either no emit (synchronous fall-through) or one with [].
        if bus.snapcast_servers_changed.calls:
            assert bus.snapcast_servers_changed.calls[-1] == ([],)
        del results

    def test_discover_servers_with_patched_browser(self, monkeypatch, bus):
        """Patch zeroconf.ServiceBrowser to immediately call
        ``add_service`` for two fake snapservers."""
        sc._ensure_zeroconf()
        if sc._zeroconf is None:
            pytest.skip("zeroconf not importable")

        class _FakeServiceInfo:
            def __init__(self, host, port, server_name):
                import socket as _s

                self.addresses = [_s.inet_aton(host)]
                self.port = port
                self.server = server_name + "."
                self.properties = {b"version": b"0.27.0"}

        class _FakeZeroconf:
            def __init__(self):
                self.closed = False

            def get_service_info(self, type_, name):
                is_a = name.startswith("snap-a")
                return _FakeServiceInfo(
                    host=("10.0.0.5" if is_a else "10.0.0.6"),
                    port=1705,
                    server_name=("snap-a.local" if is_a else "snap-b.local"),
                )

            def close(self):
                self.closed = True

        class _FakeBrowser:
            def __init__(self, zc, type_, listener):
                listener.add_service(zc, type_, "snap-a._snapcast-jsonrpc._tcp.local.")
                listener.add_service(zc, type_, "snap-b._snapcast-jsonrpc._tcp.local.")

        monkeypatch.setattr(sc, "_zeroconf", _FakeZeroconf)
        monkeypatch.setattr(sc, "_ServiceBrowser", _FakeBrowser)

        ctl = sc.SnapcastController(bus=bus)
        ctl.discover(timeout=0.05)
        assert _wait_until(lambda: bus.snapcast_servers_changed.calls, timeout=3.0)
        last = bus.snapcast_servers_changed.calls[-1][0]
        hosts = sorted(s["host"] for s in last)
        assert hosts == ["10.0.0.5", "10.0.0.6"]
        ctl.shutdown()

    def test_add_manual_server(self, controller, bus):
        info = controller.add_manual_server("snap.example", 9999, "Snap LAN")
        assert info.host == "snap.example"
        assert info.port == 9999
        assert info.hostname == "Snap LAN"
        assert bus.snapcast_servers_changed.calls
        last = bus.snapcast_servers_changed.calls[-1][0]
        assert any(s["host"] == "snap.example" for s in last)

    def test_add_manual_server_rejects_empty_host(self, controller):
        with pytest.raises(ValueError):
            controller.add_manual_server("", 1705)

    def test_add_manual_server_dedupes(self, controller):
        controller.add_manual_server("snap.example", 1705)
        controller.add_manual_server("snap.example", 1705)
        assert len([s for s in controller.servers if s.host == "snap.example"]) == 1

    def test_listener_resolve_returns_none_when_no_addresses(self):
        listener = sc._SnapcastDiscoveryListener(lambda _: None)

        class _NoAddrZC:
            def get_service_info(self, type_, name):
                return SimpleNamespace(addresses=[], port=1705, server="x.")

        assert listener._resolve(_NoAddrZC(), "x", "y") is None

    def test_listener_remove_service(self):
        events = []
        listener = sc._SnapcastDiscoveryListener(lambda servers: events.append(len(servers)))
        listener._servers["foo"] = sc.SnapcastServerInfo("10.0.0.1")
        listener.remove_service(None, "x", "foo")
        assert events == [0]


# ── Connect / disconnect ────────────────────────────────────────────────────


class TestConnect:
    def test_connect_success_fires_signals(self, controller, bus):
        assert _connect(controller) is True
        assert controller.connected is True
        assert controller.server_info is not None
        assert controller.server_info.host == "snapserver.test"
        # Connection signal fires both from _on_server_connect and from
        # the connect coroutine itself; assert at least one True emit.
        assert any(c == (True,) for c in bus.snapcast_connection_changed.calls)
        # And the initial state snapshot lands on the wire.
        assert bus.snapcast_state_changed.calls

    def test_connect_failure_emits_error(self, controller, bus, fake_server_cls):
        # Configure the fake server to fail .start().
        orig_init = fake_server_cls.__init__

        def _init(self, loop, host, port=1705, reconnect=False):
            orig_init(self, loop, host, port, reconnect)
            self.should_fail_start = True

        fake_server_cls.__init__ = _init

        done = {"ok": None}
        controller.connect("snap.test", on_done=lambda ok: done.__setitem__("ok", ok))
        assert _wait_until(lambda: done["ok"] is not None, timeout=5.0)
        assert done["ok"] is False
        assert bus.snapcast_error.calls
        assert controller.connected is False
        # Restore.
        fake_server_cls.__init__ = orig_init

    def test_disconnect_when_not_connected_is_noop(self, controller):
        controller.disconnect()
        assert controller.connected is False

    def test_disconnect_resets_state(self, controller):
        assert _connect(controller)
        controller.disconnect()
        assert controller.connected is False
        assert controller.server_info is None
        assert controller.active_group_id == ""

    def test_connect_without_snapcast_dep_emits_error(self, monkeypatch, bus):
        monkeypatch.setattr(sc, "_snapcast", None)
        monkeypatch.setattr(sc, "_snapcast_import_error", ImportError("absent"))
        ctl = sc.SnapcastController(bus=bus)
        done = {"ok": None}
        ctl.connect("snap.test", on_done=lambda ok: done.__setitem__("ok", ok))
        assert done["ok"] is False
        assert any("snapcast package not installed" in c[0] for c in bus.snapcast_error.calls)
        ctl.shutdown()


# ── Reads ───────────────────────────────────────────────────────────────────


class TestReads:
    def test_list_clients_when_disconnected_is_empty(self, controller):
        assert controller.list_clients() == []

    def test_list_groups_when_disconnected_is_empty(self, controller):
        assert controller.list_groups() == []

    def test_list_streams_when_disconnected_is_empty(self, controller):
        assert controller.list_streams() == []

    def test_list_clients_after_connect(self, controller):
        assert _connect(controller)
        cs = controller.list_clients()
        assert {c.id for c in cs} == {"clientA", "clientB"}
        couch = next(c for c in cs if c.id == "clientA")
        assert couch.name == "Couch Pi"
        assert couch.volume == 40

    def test_list_groups_after_connect(self, controller):
        assert _connect(controller)
        gs = controller.list_groups()
        assert [g.id for g in gs] == ["groupX"]
        assert sorted(gs[0].client_ids) == ["clientA", "clientB"]
        assert gs[0].stream_id == "default"

    def test_list_streams_after_connect(self, controller):
        assert _connect(controller)
        ss = {s.id for s in controller.list_streams()}
        assert ss == {"default", "jellytoast"}

    def test_snapshot_payload_shape(self, controller):
        assert _connect(controller)
        snap = controller.snapshot()
        assert snap["connected"] is True
        assert snap["server"]["host"] == "snapserver.test"
        assert isinstance(snap["clients"], list)
        assert isinstance(snap["groups"], list)
        assert isinstance(snap["streams"], list)

    def test_snapshot_when_disconnected(self, controller):
        snap = controller.snapshot()
        assert snap["connected"] is False
        assert snap["server"] is None
        assert snap["clients"] == []


# ── Writes ──────────────────────────────────────────────────────────────────


class TestWrites:
    def test_set_client_volume(self, controller, bus, fake_server_cls):
        assert _connect(controller)
        bus.snapcast_client_volume_changed.calls.clear()
        done = {"ok": None}
        controller.set_client_volume(
            "clientA",
            75,
            on_done=lambda ok: done.__setitem__("ok", ok),
        )
        assert _wait_until(lambda: done["ok"] is not None)
        assert done["ok"] is True
        assert controller._server.client("clientA").volume == 75
        # The per-client callback fires through to the bus signal.
        assert _wait_until(lambda: bus.snapcast_client_volume_changed.calls)
        last = bus.snapcast_client_volume_changed.calls[-1]
        assert last[0] == "clientA"
        assert last[1] == 75

    def test_set_client_volume_clamps_high(self, controller):
        assert _connect(controller)
        done = {"ok": None}
        controller.set_client_volume(
            "clientA",
            9999,
            on_done=lambda ok: done.__setitem__("ok", ok),
        )
        assert _wait_until(lambda: done["ok"] is not None)
        assert controller._server.client("clientA").volume == 100

    def test_set_client_volume_clamps_negative(self, controller):
        assert _connect(controller)
        done = {"ok": None}
        controller.set_client_volume(
            "clientA",
            -50,
            on_done=lambda ok: done.__setitem__("ok", ok),
        )
        assert _wait_until(lambda: done["ok"] is not None)
        assert controller._server.client("clientA").volume == 0

    def test_set_client_volume_unknown_client(self, controller, bus):
        assert _connect(controller)
        done = {"ok": None}
        controller.set_client_volume(
            "ghost",
            50,
            on_done=lambda ok: done.__setitem__("ok", ok),
        )
        # No async work — fires synchronously.
        assert done["ok"] is False
        assert any("unknown client" in c[0] for c in bus.snapcast_error.calls)

    def test_set_client_volume_when_disconnected(self, controller):
        done = {"ok": None}
        controller.set_client_volume(
            "clientA",
            50,
            on_done=lambda ok: done.__setitem__("ok", ok),
        )
        assert done["ok"] is False

    def test_set_client_mute(self, controller):
        assert _connect(controller)
        done = {"ok": None}
        controller.set_client_mute(
            "clientA",
            True,
            on_done=lambda ok: done.__setitem__("ok", ok),
        )
        assert _wait_until(lambda: done["ok"] is not None)
        assert done["ok"] is True
        assert controller._server.client("clientA").muted is True

    def test_set_client_mute_unknown(self, controller, bus):
        assert _connect(controller)
        done = {"ok": None}
        controller.set_client_mute(
            "ghost",
            True,
            on_done=lambda ok: done.__setitem__("ok", ok),
        )
        assert done["ok"] is False
        assert any("unknown client" in c[0] for c in bus.snapcast_error.calls)

    def test_set_client_name(self, controller):
        assert _connect(controller)
        done = {"ok": None}
        controller.set_client_name(
            "clientA",
            "New Name",
            on_done=lambda ok: done.__setitem__("ok", ok),
        )
        assert _wait_until(lambda: done["ok"] is not None)
        assert controller._server.client("clientA").name == "New Name"

    def test_set_group_stream(self, controller, bus):
        assert _connect(controller)
        done = {"ok": None}
        controller.set_group_stream(
            "groupX",
            "jellytoast",
            on_done=lambda ok: done.__setitem__("ok", ok),
        )
        assert _wait_until(lambda: done["ok"] is not None)
        assert controller._server.group("groupX").stream == "jellytoast"
        # The per-group callback fires through to the bus signal.
        assert _wait_until(lambda: bus.snapcast_group_changed.calls)

    def test_set_group_stream_unknown_group(self, controller, bus):
        assert _connect(controller)
        done = {"ok": None}
        controller.set_group_stream(
            "ghost",
            "default",
            on_done=lambda ok: done.__setitem__("ok", ok),
        )
        assert done["ok"] is False
        assert any("unknown group" in c[0] for c in bus.snapcast_error.calls)

    def test_set_group_mute(self, controller):
        assert _connect(controller)
        done = {"ok": None}
        controller.set_group_mute(
            "groupX",
            True,
            on_done=lambda ok: done.__setitem__("ok", ok),
        )
        assert _wait_until(lambda: done["ok"] is not None)
        assert controller._server.group("groupX").muted is True

    def test_set_group_name(self, controller):
        assert _connect(controller)
        done = {"ok": None}
        controller.set_group_name(
            "groupX",
            "Den",
            on_done=lambda ok: done.__setitem__("ok", ok),
        )
        assert _wait_until(lambda: done["ok"] is not None)
        assert controller._server.group("groupX").name == "Den"

    def test_set_group_clients(self, controller, bus):
        assert _connect(controller)
        bus.snapcast_state_changed.calls.clear()
        done = {"ok": None}
        controller.set_group_clients(
            "groupX",
            ["clientA"],
            on_done=lambda ok: done.__setitem__("ok", ok),
        )
        assert _wait_until(lambda: done["ok"] is not None)
        assert done["ok"] is True
        assert controller._server.group("groupX").clients == ["clientA"]
        # State snapshot fires on regroup so the popup repaints.
        assert _wait_until(lambda: bus.snapcast_state_changed.calls)

    def test_add_client_to_group(self, controller):
        assert _connect(controller)
        # Move both already in groupX; add a fictitious extra.
        done = {"ok": None}
        controller.add_client_to_group(
            "groupX",
            "clientC",
            on_done=lambda ok: done.__setitem__("ok", ok),
        )
        assert _wait_until(lambda: done["ok"] is not None)
        assert "clientC" in controller._server.group("groupX").clients

    def test_remove_client_from_group(self, controller):
        assert _connect(controller)
        done = {"ok": None}
        controller.remove_client_from_group(
            "groupX",
            "clientA",
            on_done=lambda ok: done.__setitem__("ok", ok),
        )
        assert _wait_until(lambda: done["ok"] is not None)
        assert "clientA" not in controller._server.group("groupX").clients


# ── Pairing ─────────────────────────────────────────────────────────────────


class TestPairing:
    def test_pair_group_emits_signal(self, controller, bus):
        controller.pair_group("groupX")
        assert controller.active_group_id == "groupX"
        assert bus.snapcast_active_group_changed.calls[-1] == ("groupX",)

    def test_pair_group_idempotent(self, controller, bus):
        controller.pair_group("groupX")
        n = len(bus.snapcast_active_group_changed.calls)
        controller.pair_group("groupX")  # second call — no second emit
        assert len(bus.snapcast_active_group_changed.calls) == n

    def test_unpair_group(self, controller, bus):
        controller.pair_group("groupX")
        controller.unpair_group()
        assert controller.active_group_id == ""
        assert bus.snapcast_active_group_changed.calls[-1] == ("",)

    def test_pair_group_empty_normalizes_to_unpaired(self, controller):
        controller.pair_group("groupX")
        controller.pair_group("")
        assert controller.active_group_id == ""


# ── Refresh ────────────────────────────────────────────────────────────────


class TestRefresh:
    def test_refresh_when_disconnected(self, controller):
        done = {"ok": None}
        controller.refresh(on_done=lambda ok: done.__setitem__("ok", ok))
        assert done["ok"] is False

    def test_refresh_round_trip(self, controller, bus):
        assert _connect(controller)
        bus.snapcast_state_changed.calls.clear()
        done = {"ok": None}
        controller.refresh(on_done=lambda ok: done.__setitem__("ok", ok))
        assert _wait_until(lambda: done["ok"] is not None)
        assert done["ok"] is True
        assert _wait_until(lambda: bus.snapcast_state_changed.calls)


# ── Notification fan-out ────────────────────────────────────────────────────


class TestNotificationFanout:
    def test_server_update_fires_state_changed(self, controller, bus):
        assert _connect(controller)
        bus.snapcast_state_changed.calls.clear()
        controller._server.trigger_update()
        # Synchronous; the fake's trigger_update calls the registered
        # callback on the calling thread.
        assert bus.snapcast_state_changed.calls

    def test_per_client_callback_fires_client_volume_changed(self, controller, bus):
        assert _connect(controller)
        bus.snapcast_client_volume_changed.calls.clear()
        c = controller._server.client("clientA")
        # Simulate a snapserver-side volume notification by invoking
        # the wired callback directly.
        c._cb(c)
        assert bus.snapcast_client_volume_changed.calls[-1][0] == "clientA"

    def test_per_group_callback_fires_group_changed(self, controller, bus):
        assert _connect(controller)
        bus.snapcast_group_changed.calls.clear()
        g = controller._server.group("groupX")
        g._cb(g)
        assert bus.snapcast_group_changed.calls[-1] == ("groupX",)

    def test_per_stream_callback_fires_streams_changed(self, controller, bus):
        assert _connect(controller)
        bus.snapcast_streams_changed.calls.clear()
        s = controller._server.stream("default")
        s._cb(s)
        assert bus.snapcast_streams_changed.calls

    def test_new_client_callback_fires_state_changed(self, controller, bus):
        assert _connect(controller)
        bus.snapcast_state_changed.calls.clear()
        fake_client = _FakeClient("clientD", "Bath Pi", volume=30)
        cb = controller._server._on_new_client_cb
        assert cb is not None
        cb(fake_client)
        assert bus.snapcast_state_changed.calls

    def test_server_disconnect_callback_emits_connection_false(self, controller, bus):
        assert _connect(controller)
        bus.snapcast_connection_changed.calls.clear()
        controller._on_server_disconnect(RuntimeError("dropped"))
        assert (False,) in bus.snapcast_connection_changed.calls
        assert any("dropped" in c[0] for c in bus.snapcast_error.calls)


# ── PlayerBus integration ──────────────────────────────────────────────────


class TestPlayerBusIntegration:
    def test_default_bus_is_player_bus_singleton(self, fake_server_cls):
        # Smoke test — controller without a bus arg pulls the singleton.
        ctl = sc.SnapcastController()
        assert ctl._bus is PlayerBus.get()
        ctl.shutdown()

    def test_player_bus_has_snapcast_signals(self):
        bus = PlayerBus.get()
        # Each declared signal is a real Qt Signal attribute.
        for name in (
            "snapcast_servers_changed",
            "snapcast_state_changed",
            "snapcast_clients_changed",
            "snapcast_groups_changed",
            "snapcast_streams_changed",
            "snapcast_client_volume_changed",
            "snapcast_group_changed",
            "snapcast_active_group_changed",
            "snapcast_connection_changed",
            "snapcast_error",
        ):
            assert hasattr(bus, name)


# ── Thread affinity (Bug 2 regression) ──────────────────────────────────────


class TestOnDoneThreadAffinity:
    """connect()/dispatch deliver on_done from the asyncio loop thread via
    Future.add_done_callback. They must marshal it onto the GUI thread
    (async_io.call_on_gui) so callers can build widgets — doing so off the
    GUI thread is the SIGSEGV the Snapcast dialog hit (Bug 2)."""

    def test_connect_on_done_runs_on_gui_thread(self, controller, qapp):
        import threading

        main_id = threading.get_ident()
        seen: dict = {}
        controller.connect(
            "snapserver.test",
            1705,
            on_done=lambda ok: seen.update(thread=threading.get_ident(), ok=ok),
        )
        assert _wait_until(lambda: "ok" in seen), "on_done never fired"
        assert seen["ok"] is True
        assert seen["thread"] == main_id, "on_done ran off the GUI thread"

    def test_dispatch_on_done_runs_on_gui_thread(self, controller, qapp):
        import threading

        assert _connect(controller)
        main_id = threading.get_ident()
        seen: dict = {}
        controller.set_client_volume(
            "clientA",
            55,
            on_done=lambda ok: seen.update(thread=threading.get_ident(), ok=ok),
        )
        assert _wait_until(lambda: "ok" in seen), "dispatch on_done never fired"
        assert seen["thread"] == main_id, "dispatch on_done ran off the GUI thread"
