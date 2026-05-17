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

    def _stub_get_chromecasts(*args, **kwargs):
        calls["chromecast"] += 1
        return ([], None)

    # The chromecast branch goes through ``pychromecast.get_chromecasts``.
    # Patch ``modules.cast_manager.pychromecast`` to a stub object so we
    # don't depend on the real lib being installed.
    import modules.cast_manager as _cm_mod

    class _PCStub:
        get_chromecasts = staticmethod(_stub_get_chromecasts)

    monkeypatch.setattr(_cm_mod, "pychromecast", _PCStub, raising=False)
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
