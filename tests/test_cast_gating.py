"""Cast type toggles + discovery-timing gates.

A25: covers two gates that ride on QSettings keys under ``cast/``:

* Per-protocol enable flags (``cast/{kind}_enabled``) make a discovery
  call a no-op when the user has disabled that cast type.
* ``cast/discovery_timing`` decides whether the app's boot-time
  pre-warm scan runs (``startup``) or skips entirely so the first
  scan only fires when the cast menu opens (``on_demand``, default).

Tests poke QSettings (via the ``isolated_settings`` route through
conftest.py's test-mode redirect) and assert that the discovery entry
points either dispatch or skip, without bringing pychromecast / pyatv
into the picture.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from PySide6.QtCore import QSettings

from modules.cast_manager import CastManager


@pytest.fixture
def cm(monkeypatch):
    """A fresh CastManager with each network-touching path replaced by
    a counter so a test can assert "the gate skipped the scan" without
    actually hitting mDNS / pychromecast."""
    m = CastManager()

    # Force-disable the lazy dep imports so even if they're installed on
    # the dev box, ``discover_chromecasts`` / ``discover_airplay`` use the
    # cheap no-network code path. The toggle gate runs BEFORE the
    # ``_ensure_*`` check; the real test is that with the toggle off the
    # ``_ensure_*`` probe is never reached. We track that via the call
    # counter on the underlying scanner stub.
    calls = {"chromecast": 0, "airplay_pyatv": 0, "airplay_zc": 0}

    # The chromecast branch went from ``pychromecast.get_chromecasts``
    # to ``CastBrowser`` + ``SimpleCastListener`` + ``get_chromecast_from_cast_info``.
    # Stub all three on the package namespace and tick the
    # ``calls["chromecast"]`` counter when discovery actually starts —
    # the per-protocol gate test asserts a disabled type never reaches
    # the start.
    import modules.cast_manager as _cm_mod

    class _CastBrowserStub:
        def __init__(self, listener, zconf, known_hosts=None):
            self.listener = listener
            self.devices: dict = {}

        def start_discovery(self):
            calls["chromecast"] += 1

        def stop_discovery(self):
            pass

    class _SimpleCastListenerStub:
        def __init__(self, add_callback=None, remove_callback=None, update_callback=None):
            self.add_callback = add_callback

    def _stub_get_from_info(info, zconf):
        return None

    def _stub_get_from_host(host_tuple):
        return None

    class _PCStub:
        # Kept so anything that still resolves through
        # ``_pkg.pychromecast`` (e.g. controllers.multizone import in
        # group-member code) finds the namespace, even though the
        # discovery entry point no longer goes through it.
        get_chromecast_from_cast_info = staticmethod(_stub_get_from_info)
        get_chromecast_from_host = staticmethod(_stub_get_from_host)

    monkeypatch.setattr(_cm_mod, "pychromecast", _PCStub, raising=False)
    monkeypatch.setattr(_cm_mod, "CastBrowser", _CastBrowserStub, raising=False)
    monkeypatch.setattr(
        _cm_mod, "SimpleCastListener", _SimpleCastListenerStub, raising=False
    )
    monkeypatch.setattr(
        _cm_mod, "get_chromecast_from_cast_info", _stub_get_from_info, raising=False
    )
    monkeypatch.setattr(
        _cm_mod, "get_chromecast_from_host", _stub_get_from_host, raising=False
    )
    monkeypatch.setattr(_cm_mod, "DISCOVERY_WINDOW_S", 0.0, raising=False)
    monkeypatch.setattr(_cm_mod, "CHROMECAST_AVAILABLE", True, raising=False)
    # Keep the LAN-interface zeroconf out of the gating tests: None means
    # CastBrowser uses its own (stubbed) default instance, exactly as
    # before this factory existed.
    monkeypatch.setattr(_cm_mod, "_make_discovery_zeroconf", lambda: None, raising=False)

    # ``discover_chromecasts`` dispatches the scan onto ``run_async``.
    # Stub it to run the worker inline so the test sees the side-effect
    # immediately, no event-loop juggling.
    def _run_async_inline(fn, on_result=None, on_error=None):
        try:
            v = fn()
        except Exception as e:
            if on_error:
                on_error(e)
            return
        if on_result:
            on_result(v)

    monkeypatch.setattr(_cm_mod, "run_async", _run_async_inline)

    # ── AirPlay paths ──────────────────────────────────────────────────
    # Stub the pyatv module so ``_discover_airplay_pyatv`` doesn't try
    # to actually scan. is_available() decides whether pyatv or the
    # zeroconf fallback runs; we let the test toggle that.
    class _AP2Stub:
        @staticmethod
        def is_available():
            return False  # default: skip pyatv branch

        @staticmethod
        def scan_sync(timeout=3.0):
            calls["airplay_pyatv"] += 1
            return []

    monkeypatch.setitem(
        __import__("sys").modules,
        "modules.airplay2",
        _AP2Stub,
    )
    # Belt-and-braces: ``_discover_airplay_pyatv`` does ``from modules
    # import airplay2`` at call time, which resolves the ``airplay2``
    # ATTRIBUTE on the already-imported ``modules`` package — that
    # attribute shadows the ``sys.modules`` setitem above once anything
    # (a prior test, a top-level import) has imported the real module.
    # So also patch the real module's ``is_available`` / ``scan_sync``
    # directly (same robust setattr the DLNA/Sonos stubs below use), or
    # the real pyatv path runs whenever pyatv is installed and the
    # zeroconf-fallback counter never increments.
    try:
        import modules.airplay2 as _real_ap2

        monkeypatch.setattr(_real_ap2, "is_available", _AP2Stub.is_available, raising=False)
        monkeypatch.setattr(_real_ap2, "scan_sync", _AP2Stub.scan_sync, raising=False)
    except Exception:
        pass

    # Zeroconf fallback. The boolean gate is ``ZEROCONF_AVAILABLE``;
    # set it True and stub the Zeroconf/ServiceBrowser ctors so the
    # network probe is a no-op.
    monkeypatch.setattr(_cm_mod, "ZEROCONF_AVAILABLE", True, raising=False)

    class _ZcStub:
        def __init__(self):
            calls["airplay_zc"] += 1

        def close(self):
            pass

    class _BrowserStub:
        def __init__(self, *args, **kwargs):
            pass

    monkeypatch.setattr(_cm_mod, "Zeroconf", _ZcStub, raising=False)
    monkeypatch.setattr(
        _cm_mod,
        "ServiceBrowser",
        _BrowserStub,
        raising=False,
    )

    # DLNA / Sonos / Snapcast discovery each kick off a real network sweep
    # (SSDP / mDNS) on a background thread that OUTLIVES the test — the
    # DLNA path in particular starts the long-lived ``jellytoast-dlna``
    # asyncio loop thread, which aborts the process when a later test's
    # event loop tears it down under random order. These gating tests only
    # assert the Chromecast/AirPlay counters, so stub the other three
    # protocols unavailable: ``discover_dlna``/``_sonos``/``_snapcast``
    # then no-op before spawning anything.
    for _modname, _attr in (
        ("modules.cast.dlna", "is_available"),
        ("modules.cast.sonos", "is_available"),
        ("modules.cast.snapcast", "_ensure_snapcast"),
    ):
        try:
            _m = __import__(_modname, fromlist=[_attr])
            monkeypatch.setattr(_m, _attr, lambda: False, raising=False)
        except Exception:
            pass

    return m, calls


def _qs():
    return QSettings("jellytoast", "jellytoast")


def _clear_cast_settings():
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
    _clear_cast_settings()
    yield
    _clear_cast_settings()


# ── Per-protocol gates ─────────────────────────────────────────────────


def test_chromecast_enabled_by_default(cm):
    m, calls = cm
    m.discover_chromecasts()
    assert calls["chromecast"] == 1


def test_chromecast_skipped_when_disabled(cm):
    m, calls = cm
    _qs().setValue("cast/chromecast_enabled", False)
    _qs().sync()
    m.discover_chromecasts()
    assert calls["chromecast"] == 0


def test_airplay_enabled_by_default_runs_zeroconf_fallback(cm):
    m, calls = cm
    m.discover_airplay()
    assert calls["airplay_zc"] == 1


def test_airplay_skipped_when_disabled(cm):
    m, calls = cm
    _qs().setValue("cast/airplay_enabled", False)
    _qs().sync()
    m.discover_airplay()
    assert calls["airplay_zc"] == 0
    assert calls["airplay_pyatv"] == 0


def test_discover_all_skips_both_when_both_disabled(cm):
    m, calls = cm
    qs = _qs()
    qs.setValue("cast/chromecast_enabled", False)
    qs.setValue("cast/airplay_enabled", False)
    qs.sync()
    m.discover_all()
    assert calls["chromecast"] == 0
    assert calls["airplay_zc"] == 0


def test_discover_all_runs_only_enabled_protocol(cm):
    m, calls = cm
    qs = _qs()
    qs.setValue("cast/chromecast_enabled", True)
    qs.setValue("cast/airplay_enabled", False)
    qs.sync()
    m.discover_all()
    assert calls["chromecast"] == 1
    assert calls["airplay_zc"] == 0


# ── Discovery timing gate ──────────────────────────────────────────────


def test_boot_scan_skipped_by_default(cm):
    # Default is ``on_demand``: the boot pre-warm must NOT scan.
    m, calls = cm
    m.discover_all_at_boot()
    assert calls["chromecast"] == 0
    assert calls["airplay_zc"] == 0


def test_boot_scan_skipped_when_on_demand(cm):
    m, calls = cm
    _qs().setValue("cast/discovery_timing", "on_demand")
    _qs().sync()
    m.discover_all_at_boot()
    assert calls["chromecast"] == 0
    assert calls["airplay_zc"] == 0


def test_boot_scan_runs_when_startup(cm):
    m, calls = cm
    _qs().setValue("cast/discovery_timing", "startup")
    _qs().sync()
    m.discover_all_at_boot()
    assert calls["chromecast"] == 1
    assert calls["airplay_zc"] == 1


def test_boot_scan_runs_when_startup_but_honors_type_gates(cm):
    # Timing says scan, but the user has Chromecast disabled — the
    # boot scan must still skip Chromecast while running AirPlay.
    m, calls = cm
    qs = _qs()
    qs.setValue("cast/discovery_timing", "startup")
    qs.setValue("cast/chromecast_enabled", False)
    qs.sync()
    m.discover_all_at_boot()
    assert calls["chromecast"] == 0
    assert calls["airplay_zc"] == 1


def test_unknown_timing_value_defaults_to_on_demand(cm):
    # Defensive: a typo'd value in the conf file shouldn't accidentally
    # opt the user into boot scans.
    m, calls = cm
    _qs().setValue("cast/discovery_timing", "whenever")
    _qs().sync()
    m.discover_all_at_boot()
    assert calls["chromecast"] == 0
    assert calls["airplay_zc"] == 0


# ── Settings round-trip ───────────────────────────────────────────────
# Light sanity that the new keys persist + read back; the real value
# wiring tests above already prove the gates work, these just guard
# the property accessors.


def test_settings_cast_type_defaults_true():
    from modules.settings import Settings

    s = Settings()
    assert s.cast_chromecast_enabled is True
    assert s.cast_airplay_enabled is True
    assert s.cast_dlna_enabled is True
    assert s.cast_sonos_enabled is True
    assert s.cast_snapcast_enabled is True


def test_settings_cast_timing_default_on_demand():
    from modules.settings import Settings

    s = Settings()
    assert s.cast_discovery_timing == "on_demand"


def test_settings_cast_timing_validates_value():
    from modules.settings import Settings

    s = Settings()
    s.cast_discovery_timing = "bogus"
    assert s.cast_discovery_timing == "on_demand"
    s.cast_discovery_timing = "startup"
    assert s.cast_discovery_timing == "startup"
    s.cast_discovery_timing = "on_demand"
    assert s.cast_discovery_timing == "on_demand"


def test_settings_cast_toggle_round_trip():
    from modules.settings import Settings

    s = Settings()
    s.cast_chromecast_enabled = False
    s.cast_sonos_enabled = False
    assert s.cast_chromecast_enabled is False
    assert s.cast_sonos_enabled is False
    # Untouched flags stay at their default
    assert s.cast_airplay_enabled is True


# ── CastBrowser migration ─────────────────────────────────────────────


def test_chromecast_discovery_materialises_devices_via_host(monkeypatch):
    """End-to-end on the CastBrowser path: when the listener buffers one
    CastInfo, ``discover_chromecasts`` materialises it through
    ``get_chromecast_from_host`` (host-based — no zeroconf needed at
    connect time) and the resulting CastDevice carries the
    listener-reported friendly_name / host / port / uuid / cast_type.
    The host tuple passed is (host, port, uuid, model_name, name)."""
    from uuid import UUID

    import modules.cast_manager as _cm_mod
    from modules.cast_manager import CastManager

    captured: dict = {}

    fake_uuid = UUID("00000000-0000-0000-0000-000000000001")

    class _FakeInfo:
        uuid = fake_uuid
        friendly_name = "Kitchen Speaker"
        host = "192.168.1.50"
        port = 8009
        cast_type = "audio"
        model_name = "Google Home"

    class _BrowserStub:
        def __init__(self, listener, zconf, known_hosts=None):
            self.listener = listener
            self.devices = {fake_uuid: _FakeInfo()}

        def start_discovery(self):
            # Simulate the mDNS callback firing during the discovery
            # window — the listener queues this uuid for snapshot.
            self.listener.add_callback(fake_uuid, "_googlecast._tcp.local.")

        def stop_discovery(self):
            captured["stopped"] = True

    class _ListenerStub:
        def __init__(self, add_callback=None, **kw):
            self.add_callback = add_callback

    def _fake_get_from_host(host_tuple):
        captured["host_tuple"] = host_tuple
        return object()  # opaque handle, just needs to be non-None

    def _run_async_inline(fn, on_result=None, on_error=None):
        v = fn()
        if on_result:
            on_result(v)

    class _PCStub:
        get_chromecast_from_host = staticmethod(_fake_get_from_host)

    monkeypatch.setattr(_cm_mod, "pychromecast", _PCStub, raising=False)
    monkeypatch.setattr(_cm_mod, "CastBrowser", _BrowserStub, raising=False)
    monkeypatch.setattr(_cm_mod, "SimpleCastListener", _ListenerStub, raising=False)
    monkeypatch.setattr(
        _cm_mod, "get_chromecast_from_host", _fake_get_from_host, raising=False
    )
    monkeypatch.setattr(_cm_mod, "DISCOVERY_WINDOW_S", 0.0, raising=False)
    monkeypatch.setattr(_cm_mod, "CHROMECAST_AVAILABLE", True, raising=False)
    monkeypatch.setattr(_cm_mod, "_make_discovery_zeroconf", lambda: None, raising=False)
    monkeypatch.setattr(_cm_mod, "run_async", _run_async_inline)

    m = CastManager()
    m.discover_chromecasts()

    assert captured.get("stopped") is True
    # Host tuple is (host, port, uuid, model_name, friendly_name).
    assert captured.get("host_tuple") == (
        "192.168.1.50",
        8009,
        fake_uuid,
        "Google Home",
        "Kitchen Speaker",
    )
    assert len(m.chromecast_devices) == 1
    dev = m.chromecast_devices[0]
    assert dev.name == "Kitchen Speaker"
    assert dev.host == "192.168.1.50"
    assert dev.port == 8009
    assert dev.uuid == str(fake_uuid)
    assert dev.cast_type == "audio"
    assert dev.device_type == "chromecast"
    assert dev.cast_object is not None


def test_chromecast_discovery_tolerates_materialise_failure(monkeypatch):
    """If ``get_chromecast_from_host`` raises for one uuid, the other
    devices in the same sweep still materialise. Defends against a single
    offline / mis-resolving Chromecast nuking the whole discovery
    snapshot."""
    from uuid import UUID

    import modules.cast_manager as _cm_mod
    from modules.cast_manager import CastManager

    bad = UUID("00000000-0000-0000-0000-00000000aaaa")
    good = UUID("00000000-0000-0000-0000-00000000bbbb")

    class _Info:
        def __init__(self, uuid_, name):
            self.uuid = uuid_
            self.friendly_name = name
            self.host = "10.0.0.1"
            self.port = 8009
            self.cast_type = "cast"
            self.model_name = "Chromecast"

    class _BrowserStub:
        def __init__(self, listener, zconf, known_hosts=None):
            self.listener = listener
            self.devices = {bad: _Info(bad, "Broken"), good: _Info(good, "Healthy")}

        def start_discovery(self):
            self.listener.add_callback(bad, "_googlecast._tcp.local.")
            self.listener.add_callback(good, "_googlecast._tcp.local.")

        def stop_discovery(self):
            pass

    class _ListenerStub:
        def __init__(self, add_callback=None, **kw):
            self.add_callback = add_callback

    def _flaky_get_from_host(host_tuple):
        # host_tuple = (host, port, uuid, model_name, name)
        if host_tuple[2] == bad:
            raise RuntimeError("simulated socket failure")
        return object()

    def _run_async_inline(fn, on_result=None, on_error=None):
        v = fn()
        if on_result:
            on_result(v)

    class _PCStub:
        get_chromecast_from_host = staticmethod(_flaky_get_from_host)

    monkeypatch.setattr(_cm_mod, "pychromecast", _PCStub, raising=False)
    monkeypatch.setattr(_cm_mod, "CastBrowser", _BrowserStub, raising=False)
    monkeypatch.setattr(_cm_mod, "SimpleCastListener", _ListenerStub, raising=False)
    monkeypatch.setattr(
        _cm_mod, "get_chromecast_from_host", _flaky_get_from_host, raising=False
    )
    monkeypatch.setattr(_cm_mod, "DISCOVERY_WINDOW_S", 0.0, raising=False)
    monkeypatch.setattr(_cm_mod, "CHROMECAST_AVAILABLE", True, raising=False)
    monkeypatch.setattr(_cm_mod, "_make_discovery_zeroconf", lambda: None, raising=False)
    monkeypatch.setattr(_cm_mod, "run_async", _run_async_inline)

    m = CastManager()
    m.discover_chromecasts()

    assert len(m.chromecast_devices) == 1
    assert m.chromecast_devices[0].name == "Healthy"


def test_pychromecast_discovery_logger_pinned_to_warning():
    """The deprecation-noise mute that bundles with the CastBrowser
    migration: ``_ensure_chromecast`` lifts the
    ``pychromecast.discovery`` sub-logger to WARNING so any future
    library codepath that re-emits the "discover_chromecasts is
    deprecated" INFO line stays quiet. Requires the real pychromecast
    to be installed (it's a runtime dep), but doesn't touch the
    network — only checks the logger level after the lazy import."""
    import logging

    import modules.cast_manager as _cm_mod

    # Force a fresh probe — clear the cached flag so _ensure_chromecast
    # actually re-enters the import branch.
    monkeypatched_flag = _cm_mod.CHROMECAST_AVAILABLE
    _cm_mod.CHROMECAST_AVAILABLE = None
    try:
        ok = _cm_mod._ensure_chromecast()
    finally:
        _cm_mod.CHROMECAST_AVAILABLE = monkeypatched_flag

    if not ok:
        pytest.skip("pychromecast not installed — mute is a no-op here")

    level = logging.getLogger("pychromecast.discovery").getEffectiveLevel()
    assert level >= logging.WARNING, (
        f"pychromecast.discovery logger should be muted at WARNING+, "
        f"got level={level}"
    )


# ── LAN-interface binding (Tailscale exclusion) ───────────────────────
# A default Zeroconf() binds across all interfaces; with a Tailscale
# tunnel up the _googlecast._tcp sweep leaves via the overlay and finds
# nothing. _discovery_interfaces excludes the 100.64.0.0/10 overlay so
# the bind stays on the LAN, and discover_chromecasts passes + closes the
# resulting instance.


def _fake_ifaddr(addrs):
    """A stub ``ifaddr`` module exposing one adapter with ``addrs``
    (list of ``(ip_str_or_tuple, is_IPv4_bool)``)."""
    import types

    ips = [types.SimpleNamespace(ip=a, is_IPv4=v4) for (a, v4) in addrs]
    adapter = types.SimpleNamespace(name="eth", nice_name="eth", ips=ips)
    mod = types.ModuleType("ifaddr")
    mod.get_adapters = lambda: [adapter]
    return mod


class TestDiscoveryInterfaces:
    def test_excludes_tailscale_cgnat(self, monkeypatch):
        import sys

        import modules.cast_manager as _cm_mod

        monkeypatch.setitem(
            sys.modules,
            "ifaddr",
            _fake_ifaddr([("192.168.50.20", True), ("100.94.220.31", True), ("127.0.0.1", True)]),
        )
        assert _cm_mod._discovery_interfaces() == ["192.168.50.20"]

    def test_excludes_loopback_and_link_local(self, monkeypatch):
        import sys

        import modules.cast_manager as _cm_mod

        monkeypatch.setitem(
            sys.modules,
            "ifaddr",
            _fake_ifaddr([("127.0.0.1", True), ("169.254.1.2", True), ("10.0.0.5", True)]),
        )
        assert _cm_mod._discovery_interfaces() == ["10.0.0.5"]

    def test_ignores_ipv6(self, monkeypatch):
        import sys

        import modules.cast_manager as _cm_mod

        monkeypatch.setitem(
            sys.modules,
            "ifaddr",
            _fake_ifaddr([("192.168.1.4", True), (("fe80::1", 64, 0), False)]),
        )
        assert _cm_mod._discovery_interfaces() == ["192.168.1.4"]

    def test_cgnat_only_host_falls_back_to_cgnat(self, monkeypatch):
        # A pure-Tailscale box (no LAN) still gets *something* to bind to
        # rather than None disabling the LAN-preference entirely.
        import sys

        import modules.cast_manager as _cm_mod

        monkeypatch.setitem(
            sys.modules,
            "ifaddr",
            _fake_ifaddr([("100.64.5.5", True), ("127.0.0.1", True)]),
        )
        assert _cm_mod._discovery_interfaces() == ["100.64.5.5"]

    def test_no_usable_interface_returns_none(self, monkeypatch):
        import sys

        import modules.cast_manager as _cm_mod

        monkeypatch.setitem(
            sys.modules,
            "ifaddr",
            _fake_ifaddr([("127.0.0.1", True), ("169.254.9.9", True)]),
        )
        assert _cm_mod._discovery_interfaces() is None

    def test_ifaddr_missing_returns_none(self, monkeypatch):
        import sys

        import modules.cast_manager as _cm_mod

        # None in sys.modules makes `import ifaddr` raise ImportError.
        monkeypatch.setitem(sys.modules, "ifaddr", None)
        assert _cm_mod._discovery_interfaces() is None


def test_discover_passes_lan_zeroconf_to_browser_and_closes_it(monkeypatch):
    """The LAN-bound discovery zeroconf is handed to CastBrowser and
    closed after the sweep — host-based materialisation means the
    instance is only needed for discovery itself, not for connecting, so
    there's nothing to keep alive (CastBrowser only auto-closes the
    instance it creates for zconf=None)."""
    import modules.cast_manager as _cm_mod
    from modules.cast_manager import CastManager

    events = {"passed_zc": "unset", "closed": False}

    class _Zc:
        def close(self):
            events["closed"] = True

    fake_zc = _Zc()

    class _BrowserStub:
        def __init__(self, listener, zconf, known_hosts=None):
            events["passed_zc"] = zconf
            self.devices = {}

        def start_discovery(self):
            pass

        def stop_discovery(self):
            pass

    class _ListenerStub:
        def __init__(self, add_callback=None, **kw):
            self.add_callback = add_callback

    def _run_async_inline(fn, on_result=None, on_error=None):
        v = fn()
        if on_result:
            on_result(v)

    monkeypatch.setattr(_cm_mod, "CastBrowser", _BrowserStub, raising=False)
    monkeypatch.setattr(_cm_mod, "SimpleCastListener", _ListenerStub, raising=False)
    monkeypatch.setattr(_cm_mod, "DISCOVERY_WINDOW_S", 0.0, raising=False)
    monkeypatch.setattr(_cm_mod, "CHROMECAST_AVAILABLE", True, raising=False)
    monkeypatch.setattr(_cm_mod, "_make_discovery_zeroconf", lambda: fake_zc, raising=False)
    monkeypatch.setattr(_cm_mod, "run_async", _run_async_inline)

    CastManager().discover_chromecasts()
    assert events["passed_zc"] is fake_zc  # handed to CastBrowser
    assert events["closed"] is True  # closed after the sweep


def test_set_member_volume_uses_bounded_wait(monkeypatch):
    # An unreachable group-member speaker must not block the worker forever:
    # cc.wait() needs a finite timeout (matching the member-read site) so it
    # can't permanently leak a slot from the bounded async pool.
    import modules.cast_manager as _cm_mod

    m = CastManager()
    wait = MagicMock()
    m.chromecast_devices = [
        SimpleNamespace(
            uuid="tv",
            cast_object=SimpleNamespace(wait=wait, set_volume=lambda v: None),
        )
    ]

    def _run_async_inline(fn, on_result=None, on_error=None):
        try:
            v = fn()
        except Exception as e:  # pragma: no cover - not expected in this test
            if on_error:
                on_error(e)
            return
        if on_result:
            on_result(v)

    monkeypatch.setattr(_cm_mod, "run_async", _run_async_inline)
    m.set_member_volume_async("tv", 50)

    assert wait.called
    call = wait.call_args
    t = call.kwargs.get("timeout", call.args[0] if call.args else None)
    assert t is not None and t > 0  # fails on the unbounded cc.wait()
