"""Discovery fan-out across the DLNA / Sonos / Snapcast backends.

Covers the wiring added in ``auto/cast-manager-discovery-fanout``:
``CastManager`` now drives all five protocol discoveries (not just
Chromecast + AirPlay). Each ``discover_<type>`` must:

* no-op when its ``cast/<type>_enabled`` toggle is off — before any
  backend call is attempted;
* no-op when the backend's optional dependency is unavailable;
* adapt discovered devices into ``CastDevice`` records with the right
  ``device_type`` and surface them through the shared devices-callback;
* be invoked by ``discover_all`` (each still gated by its own toggle).

Backends are fully mocked — no real network, no real ``async-upnp-client``
/ ``soco`` / ``snapcast`` import. The ``run_async`` shim runs workers
inline so side-effects are observable without a Qt event loop.
"""

import sys
import types

import pytest
from PySide6.QtCore import QSettings

from modules.cast_manager import CastDevice, CastManager

# ── Backend fakes ──────────────────────────────────────────────────────


class _FakeDlnaDevice:
    def __init__(self, udn, name, host="192.168.1.5", port=8200):
        self.udn = udn
        self.name = name
        self.host = host
        self.port = port


class _FakeDlnaController:
    def __init__(self, devices):
        self._devices = devices
        self.discover_calls = 0
        self.stopped = False
        self.polling_stopped = False

    def discover(self, timeout=5, validate=False):
        self.discover_calls += 1
        return list(self._devices)

    def stop_polling(self):
        self.polling_stopped = True

    def start_polling(self, on_state=None):
        pass

    def stop_renderer(self):
        self.stopped = True
        return True


class _FakeSonosZone:
    def __init__(self, uuid, label, ip="192.168.1.9", is_group=False):
        self.uuid = uuid
        self.label = label
        self.coordinator_ip = ip
        self.is_group = is_group


class _FakeSnapServer:
    def __init__(self, host, port=1705, hostname=""):
        self.host = host
        self.port = port
        self.hostname = hostname


def _make_dlna_module(available, devices, calls):
    mod = types.ModuleType("modules.cast.dlna")
    controller = _FakeDlnaController(devices)
    mod._async_upnp_imported = available

    def is_available():
        return available

    def get_dlna_controller():
        return controller

    mod.is_available = is_available
    mod.get_dlna_controller = get_dlna_controller
    mod._controller = controller
    mod._calls = calls
    return mod


def _make_sonos_module(available, zones, calls):
    mod = types.ModuleType("modules.cast.sonos")

    def is_available():
        return available

    def discover_sonos(timeout=1.0):
        calls["sonos"] += 1
        return list(zones)

    def stop_sonos(_obj):
        return True

    mod.is_available = is_available
    mod.discover_sonos = discover_sonos
    mod.stop_sonos = stop_sonos
    return mod


def _make_snapcast_module(available, servers, calls):
    mod = types.ModuleType("modules.cast.snapcast")

    def _ensure_snapcast():
        return available

    def discover_servers(on_result, timeout=3.0):
        calls["snapcast"] += 1
        # The real backend fires on_result on a worker thread once the
        # browse window closes; the fake fires synchronously so the
        # test sees the side-effect immediately.
        on_result(list(servers))

    mod._ensure_snapcast = _ensure_snapcast
    mod._snapcast = object() if available else None
    mod.discover_servers = discover_servers
    return mod


# ── Fixtures ───────────────────────────────────────────────────────────


@pytest.fixture
def cm(monkeypatch):
    """A CastManager with the three new backends mocked, and
    ``run_async`` patched to run workers inline. Returns ``(manager,
    calls, install)`` — ``install`` lets a test register the backend
    modules with chosen availability / device payloads."""
    import modules.cast_manager as _cm_mod

    m = CastManager()
    calls = {"dlna": 0, "sonos": 0, "snapcast": 0}

    def _run_async_inline(fn, on_result=None, on_error=None):
        try:
            v = fn()
        except Exception as e:  # noqa: BLE001
            if on_error:
                on_error(e)
            return
        if on_result:
            on_result(v)

    monkeypatch.setattr(_cm_mod, "run_async", _run_async_inline)

    # ``discover_*`` does ``from modules.cast import dlna`` — that binds
    # the *attribute* off the already-imported ``modules.cast`` package,
    # so a bare ``sys.modules`` swap isn't enough when an earlier test in
    # the suite already imported the real submodule. Patch both.
    import importlib

    cast_pkg = importlib.import_module("modules.cast")

    def _swap(name, mod):
        monkeypatch.setitem(sys.modules, f"modules.cast.{name}", mod)
        monkeypatch.setattr(cast_pkg, name, mod, raising=False)

    def install(dlna=None, sonos=None, snapcast=None):
        if dlna is not None:
            available, devices = dlna
            mod = _make_dlna_module(available, devices, calls)
            # Wrap discover() so the call counter ticks.
            orig = mod.get_dlna_controller().discover

            def _counted(timeout=5, validate=False, _o=orig):
                calls["dlna"] += 1
                return _o(timeout=timeout, validate=validate)

            mod._controller.discover = _counted
            _swap("dlna", mod)
        if sonos is not None:
            available, zones = sonos
            _swap("sonos", _make_sonos_module(available, zones, calls))
        if snapcast is not None:
            available, servers = snapcast
            _swap("snapcast", _make_snapcast_module(available, servers, calls))

    return m, calls, install


def _qs():
    return QSettings("jellytoast", "jellytoast")


def _clear():
    qs = _qs()
    for k in (
        "cast/chromecast_enabled",
        "cast/airplay_enabled",
        "cast/dlna_enabled",
        "cast/sonos_enabled",
        "cast/snapcast_enabled",
        "cast/discovery_timing",
    ):
        qs.remove(k)
    qs.sync()


@pytest.fixture(autouse=True)
def _reset_settings():
    _clear()
    yield
    _clear()


# ── DLNA ───────────────────────────────────────────────────────────────


def test_dlna_skipped_when_disabled(cm):
    m, calls, install = cm
    install(dlna=(True, [_FakeDlnaDevice("uuid:a", "Living Room TV")]))
    _qs().setValue("cast/dlna_enabled", False)
    _qs().sync()
    m.discover_dlna()
    assert calls["dlna"] == 0
    assert m.dlna_devices == []


def test_dlna_skipped_when_dep_unavailable(cm):
    m, calls, install = cm
    install(dlna=(False, [_FakeDlnaDevice("uuid:a", "Living Room TV")]))
    m.discover_dlna()
    assert calls["dlna"] == 0
    assert m.dlna_devices == []


def test_dlna_discovers_and_adapts(cm):
    m, calls, install = cm
    install(dlna=(True, [_FakeDlnaDevice("uuid:a", "Living Room TV")]))
    seen = []
    m.set_devices_callback(seen.append)
    m.discover_dlna()
    assert calls["dlna"] == 1
    assert len(m.dlna_devices) == 1
    dev = m.dlna_devices[0]
    assert isinstance(dev, CastDevice)
    assert dev.device_type == "dlna"
    assert dev.name == "Living Room TV"
    assert dev.uuid == "uuid:a"
    # The discovered device reached the devices-callback.
    assert seen and any(d.device_type == "dlna" for d in seen[-1])


# ── Sonos ──────────────────────────────────────────────────────────────


def test_sonos_skipped_when_disabled(cm):
    m, calls, install = cm
    install(sonos=(True, [_FakeSonosZone("rincon:1", "Kitchen")]))
    _qs().setValue("cast/sonos_enabled", False)
    _qs().sync()
    m.discover_sonos()
    assert calls["sonos"] == 0
    assert m.sonos_devices == []


def test_sonos_skipped_when_dep_unavailable(cm):
    m, calls, install = cm
    install(sonos=(False, [_FakeSonosZone("rincon:1", "Kitchen")]))
    m.discover_sonos()
    assert calls["sonos"] == 0
    assert m.sonos_devices == []


def test_sonos_discovers_and_adapts(cm):
    m, calls, install = cm
    install(
        sonos=(
            True,
            [_FakeSonosZone("rincon:1", "Kitchen + Patio", is_group=True)],
        )
    )
    seen = []
    m.set_devices_callback(seen.append)
    m.discover_sonos()
    assert calls["sonos"] == 1
    assert len(m.sonos_devices) == 1
    dev = m.sonos_devices[0]
    assert dev.device_type == "sonos"
    assert dev.name == "Kitchen + Patio"
    assert dev.uuid == "rincon:1"
    assert dev.cast_type == "group"  # is_group → group
    assert seen and any(d.device_type == "sonos" for d in seen[-1])


# ── Snapcast ───────────────────────────────────────────────────────────


def test_snapcast_skipped_when_disabled(cm):
    m, calls, install = cm
    install(snapcast=(True, [_FakeSnapServer("192.168.1.20", hostname="snap")]))
    _qs().setValue("cast/snapcast_enabled", False)
    _qs().sync()
    m.discover_snapcast()
    assert calls["snapcast"] == 0
    assert m.snapcast_devices == []


def test_snapcast_skipped_when_dep_unavailable(cm):
    m, calls, install = cm
    install(snapcast=(False, [_FakeSnapServer("192.168.1.20")]))
    m.discover_snapcast()
    assert calls["snapcast"] == 0
    assert m.snapcast_devices == []


def test_snapcast_discovers_and_adapts(cm):
    m, calls, install = cm
    install(
        snapcast=(True, [_FakeSnapServer("192.168.1.20", 1705, hostname="snap")])
    )
    seen = []
    m.set_devices_callback(seen.append)
    m.discover_snapcast()
    assert calls["snapcast"] == 1
    assert len(m.snapcast_devices) == 1
    dev = m.snapcast_devices[0]
    assert dev.device_type == "snapcast"
    assert dev.name == "snap"
    assert dev.host == "192.168.1.20"
    assert dev.uuid == "192.168.1.20:1705"
    assert seen and any(d.device_type == "snapcast" for d in seen[-1])


# ── discover_all fan-out ───────────────────────────────────────────────


def _install_all(install):
    install(
        dlna=(True, [_FakeDlnaDevice("uuid:a", "TV")]),
        sonos=(True, [_FakeSonosZone("rincon:1", "Kitchen")]),
        snapcast=(True, [_FakeSnapServer("192.168.1.20", hostname="snap")]),
    )


def _disable_chromecast_airplay():
    """``discover_all`` also drives Chromecast + AirPlay; those backends
    aren't mocked in this file, so disable them to keep the fan-out
    tests focused on (and fast for) the three new protocols."""
    qs = _qs()
    qs.setValue("cast/chromecast_enabled", False)
    qs.setValue("cast/airplay_enabled", False)
    qs.sync()


def test_discover_all_invokes_all_three_new_protocols(cm):
    m, calls, install = cm
    _install_all(install)
    _disable_chromecast_airplay()
    m.discover_all()
    assert calls["dlna"] == 1
    assert calls["sonos"] == 1
    assert calls["snapcast"] == 1


def test_discover_all_honors_per_type_gates(cm):
    m, calls, install = cm
    _install_all(install)
    _disable_chromecast_airplay()
    qs = _qs()
    qs.setValue("cast/dlna_enabled", True)
    qs.setValue("cast/sonos_enabled", False)
    qs.setValue("cast/snapcast_enabled", False)
    qs.sync()
    m.discover_all()
    assert calls["dlna"] == 1
    assert calls["sonos"] == 0
    assert calls["snapcast"] == 0


def test_get_all_devices_merges_every_protocol(cm):
    m, _calls, install = cm
    _install_all(install)
    m.discover_dlna()
    m.discover_sonos()
    m.discover_snapcast()
    types_seen = {d.device_type for d in m.get_all_devices()}
    assert types_seen == {"dlna", "sonos", "snapcast"}


# ── Transport routing ──────────────────────────────────────────────────


def test_stop_cast_routes_dlna(cm, monkeypatch):
    m, _calls, install = cm
    install(dlna=(True, [_FakeDlnaDevice("uuid:a", "TV")]))
    stopped = {"dlna": False}

    class _Ctl:
        def stop_polling(self):
            pass

        def stop_renderer(self):
            stopped["dlna"] = True

    monkeypatch.setattr(
        sys.modules["modules.cast.dlna"], "get_dlna_controller", lambda: _Ctl()
    )
    m.active_cast = CastDevice("TV", "h", 0, "dlna", uuid="uuid:a")
    m.stop_cast()
    assert stopped["dlna"] is True
    assert m.active_cast is None


def test_stop_cast_routes_sonos(cm, monkeypatch):
    m, _calls, install = cm
    install(sonos=(True, [_FakeSonosZone("rincon:1", "Kitchen")]))
    stopped = {"sonos": None}

    def _stop_sonos(obj):
        stopped["sonos"] = obj
        return True

    monkeypatch.setattr(sys.modules["modules.cast.sonos"], "stop_sonos", _stop_sonos)
    zone = _FakeSonosZone("rincon:1", "Kitchen")
    m.active_cast = CastDevice("Kitchen", "h", 0, "sonos", cast_object=zone)
    m.stop_cast()
    assert stopped["sonos"] is zone
    assert m.active_cast is None


def test_stop_cast_routes_snapcast(cm):
    m, _calls, _install = cm
    m.active_cast = CastDevice("snap", "h", 1705, "snapcast")
    m.stop_cast()
    # Snapcast has no stop verb — stop_cast just clears active_cast.
    assert m.active_cast is None
