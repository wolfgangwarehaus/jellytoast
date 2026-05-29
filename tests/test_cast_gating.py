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

    class _PCStub:
        # Kept so anything that still resolves through
        # ``_pkg.pychromecast`` (e.g. controllers.multizone import in
        # group-member code) finds the namespace, even though the
        # discovery entry point no longer goes through it.
        get_chromecast_from_cast_info = staticmethod(_stub_get_from_info)

    monkeypatch.setattr(_cm_mod, "pychromecast", _PCStub, raising=False)
    monkeypatch.setattr(_cm_mod, "CastBrowser", _CastBrowserStub, raising=False)
    monkeypatch.setattr(
        _cm_mod, "SimpleCastListener", _SimpleCastListenerStub, raising=False
    )
    monkeypatch.setattr(
        _cm_mod, "get_chromecast_from_cast_info", _stub_get_from_info, raising=False
    )
    monkeypatch.setattr(_cm_mod, "DISCOVERY_WINDOW_S", 0.0, raising=False)
    monkeypatch.setattr(_cm_mod, "CHROMECAST_AVAILABLE", True, raising=False)

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


def test_chromecast_discovery_materialises_devices_via_castbrowser(monkeypatch):
    """End-to-end on the CastBrowser path: when the listener buffers
    one CastInfo, ``discover_chromecasts`` materialises it through
    ``get_chromecast_from_cast_info`` and the resulting CastDevice
    carries the listener-reported friendly_name / host / port / uuid /
    cast_type."""
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

    def _fake_get_from_info(info, zconf):
        captured["materialised_friendly"] = info.friendly_name
        return object()  # opaque handle, just needs to be non-None

    def _run_async_inline(fn, on_result=None, on_error=None):
        v = fn()
        if on_result:
            on_result(v)

    class _PCStub:
        get_chromecast_from_cast_info = staticmethod(_fake_get_from_info)

    monkeypatch.setattr(_cm_mod, "pychromecast", _PCStub, raising=False)
    monkeypatch.setattr(_cm_mod, "CastBrowser", _BrowserStub, raising=False)
    monkeypatch.setattr(_cm_mod, "SimpleCastListener", _ListenerStub, raising=False)
    monkeypatch.setattr(
        _cm_mod, "get_chromecast_from_cast_info", _fake_get_from_info, raising=False
    )
    monkeypatch.setattr(_cm_mod, "DISCOVERY_WINDOW_S", 0.0, raising=False)
    monkeypatch.setattr(_cm_mod, "CHROMECAST_AVAILABLE", True, raising=False)
    monkeypatch.setattr(_cm_mod, "run_async", _run_async_inline)

    m = CastManager()
    m.discover_chromecasts()

    assert captured.get("stopped") is True
    assert captured.get("materialised_friendly") == "Kitchen Speaker"
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
    """If ``get_chromecast_from_cast_info`` raises for one uuid, the
    other devices in the same sweep still materialise. Defends against
    a single offline / mis-resolving Chromecast nuking the whole
    discovery snapshot."""
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

    def _flaky_get_from_info(info, zconf):
        if info.uuid == bad:
            raise RuntimeError("simulated socket failure")
        return object()

    def _run_async_inline(fn, on_result=None, on_error=None):
        v = fn()
        if on_result:
            on_result(v)

    class _PCStub:
        get_chromecast_from_cast_info = staticmethod(_flaky_get_from_info)

    monkeypatch.setattr(_cm_mod, "pychromecast", _PCStub, raising=False)
    monkeypatch.setattr(_cm_mod, "CastBrowser", _BrowserStub, raising=False)
    monkeypatch.setattr(_cm_mod, "SimpleCastListener", _ListenerStub, raising=False)
    monkeypatch.setattr(
        _cm_mod, "get_chromecast_from_cast_info", _flaky_get_from_info, raising=False
    )
    monkeypatch.setattr(_cm_mod, "DISCOVERY_WINDOW_S", 0.0, raising=False)
    monkeypatch.setattr(_cm_mod, "CHROMECAST_AVAILABLE", True, raising=False)
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
