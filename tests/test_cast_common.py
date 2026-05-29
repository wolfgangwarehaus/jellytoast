"""Coverage for ``modules.cast_manager._common``.

Two primitives live here:

* ``_AirPlayListener`` — a zeroconf ServiceBrowser listener that
  maintains a per-name ``CastDevice`` dict and notifies a callback on
  add / update / remove.
* ``_type_enabled`` — the per-protocol QSettings gate that lets a user
  disable a cast type they don't own so discovery skips it entirely.

Both are pure-Python (the listener doesn't touch real mDNS — we feed
it a stubbed ``zc`` with the methods it actually calls). The gate
reads QSettings directly, so conftest.py's test-mode redirect keeps
the user's real config untouched.
"""

from __future__ import annotations

import socket
from typing import List

import pytest
from PySide6.QtCore import QSettings

from modules.cast_manager._common import CastDevice, _AirPlayListener, _type_enabled


# ── _AirPlayListener ───────────────────────────────────────────────────


class _FakeServiceInfo:
    """Stand-in for the zeroconf ``ServiceInfo`` shape the listener
    pokes — only ``addresses`` (raw 4-byte IPv4) and ``port`` matter."""

    def __init__(self, ip: str = "192.168.1.50", port: int = 7000):
        self.addresses = [socket.inet_aton(ip)] if ip else []
        self.port = port


class _FakeZc:
    """Minimal zeroconf stand-in: holds a dict and returns the value
    for the requested name (or None for absent / suppressed entries)."""

    def __init__(self, services: dict | None = None):
        self._services = services or {}

    def get_service_info(self, type_, name):
        return self._services.get(name)


@pytest.fixture
def captured_devices():
    """Callback recorder: each ``add/remove/update`` push lands here so
    a test can assert on the *latest* device list the listener emitted."""
    captured: List[List[CastDevice]] = []
    return captured


@pytest.fixture
def listener(captured_devices):
    return _AirPlayListener(callback=captured_devices.append)


def test_add_service_records_and_emits(listener, captured_devices):
    zc = _FakeZc(
        {
            "Living Room._airplay._tcp.local.": _FakeServiceInfo(
                "192.168.1.40", 7000
            )
        }
    )
    listener.add_service(zc, "_airplay._tcp.local.", "Living Room._airplay._tcp.local.")
    assert len(captured_devices) == 1
    devices = captured_devices[-1]
    assert len(devices) == 1
    dev = devices[0]
    # The mDNS-suffix strip should leave only the user-visible name.
    assert dev.name == "Living Room"
    assert dev.host == "192.168.1.40"
    assert dev.port == 7000
    assert dev.device_type == "airplay"
    # uuid carries the full mDNS name so duplicate-add deduplicates.
    assert dev.uuid == "Living Room._airplay._tcp.local."


def test_add_service_ignored_when_info_missing(listener, captured_devices):
    """A None ``get_service_info`` (e.g. browser raced ahead of cache)
    must not record anything or fire the callback."""
    zc = _FakeZc()  # no entries
    listener.add_service(zc, "_airplay._tcp.local.", "Ghost._airplay._tcp.local.")
    assert captured_devices == []
    assert listener.devices == {}


def test_add_service_ignored_when_no_addresses(listener, captured_devices):
    """Entries lacking ``addresses`` (IPv6-only services or empty
    cache hits) are dropped — host would be unset otherwise."""
    zc = _FakeZc(
        {"NoIP._airplay._tcp.local.": _FakeServiceInfo(ip="", port=7000)}
    )
    listener.add_service(zc, "_airplay._tcp.local.", "NoIP._airplay._tcp.local.")
    assert captured_devices == []
    assert listener.devices == {}


def test_remove_service_drops_entry_and_emits(listener, captured_devices):
    zc = _FakeZc(
        {"A._airplay._tcp.local.": _FakeServiceInfo("10.0.0.1", 7000)}
    )
    listener.add_service(zc, "_airplay._tcp.local.", "A._airplay._tcp.local.")
    captured_devices.clear()

    listener.remove_service(zc, "_airplay._tcp.local.", "A._airplay._tcp.local.")
    assert listener.devices == {}
    assert captured_devices == [[]]


def test_remove_service_unknown_name_is_idempotent(listener, captured_devices):
    """remove() on an unknown name silently emits the current list —
    never raises."""
    listener.remove_service(None, "_airplay._tcp.local.", "Unknown")
    assert listener.devices == {}
    assert captured_devices == [[]]


def test_update_service_refreshes_via_add(listener, captured_devices):
    """``update_service`` is just an ``add_service`` re-dispatch so a
    port / IP change replays through the same record path."""
    name = "Kitchen._airplay._tcp.local."
    services = {name: _FakeServiceInfo("192.168.1.41", 7000)}
    zc = _FakeZc(services)
    listener.add_service(zc, "_airplay._tcp.local.", name)
    # Port change: same uuid, new port.
    services[name] = _FakeServiceInfo("192.168.1.41", 7001)
    listener.update_service(zc, "_airplay._tcp.local.", name)
    assert listener.devices[name].port == 7001
    # Two callback fires: initial add + update.
    assert len(captured_devices) == 2


def test_multiple_devices_accumulate(listener, captured_devices):
    services = {
        "A._airplay._tcp.local.": _FakeServiceInfo("10.0.0.1", 7000),
        "B._airplay._tcp.local.": _FakeServiceInfo("10.0.0.2", 7000),
    }
    zc = _FakeZc(services)
    listener.add_service(zc, "_airplay._tcp.local.", "A._airplay._tcp.local.")
    listener.add_service(zc, "_airplay._tcp.local.", "B._airplay._tcp.local.")
    assert {d.name for d in captured_devices[-1]} == {"A", "B"}


# ── _type_enabled ──────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _clean_cast_settings():
    """Wipe the cast/* QSettings keys before and after each gate test
    so cross-test order can't leak a False through."""
    qs = QSettings("jellytoast", "jellytoast")
    for k in (
        "cast/chromecast_enabled",
        "cast/airplay_enabled",
        "cast/dlna_enabled",
        "cast/sonos_enabled",
        "cast/snapcast_enabled",
    ):
        qs.remove(k)
    qs.sync()
    yield
    for k in (
        "cast/chromecast_enabled",
        "cast/airplay_enabled",
        "cast/dlna_enabled",
        "cast/sonos_enabled",
        "cast/snapcast_enabled",
    ):
        qs.remove(k)
    qs.sync()


def test_type_enabled_default_true_when_unset():
    """Default is True (opt-in disable model — every cast type is on
    until the user toggles it off in Settings)."""
    assert _type_enabled("chromecast") is True
    assert _type_enabled("airplay") is True
    assert _type_enabled("dlna") is True
    assert _type_enabled("sonos") is True
    assert _type_enabled("snapcast") is True


def test_type_enabled_returns_false_when_disabled():
    qs = QSettings("jellytoast", "jellytoast")
    qs.setValue("cast/chromecast_enabled", False)
    qs.sync()
    assert _type_enabled("chromecast") is False
    # Other types remain default-True (per-kind keys are independent).
    assert _type_enabled("airplay") is True


def test_type_enabled_reads_kind_specific_key():
    """Each kind gates on its own ``cast/<kind>_enabled`` key — a
    disable on one type must not leak to siblings."""
    qs = QSettings("jellytoast", "jellytoast")
    qs.setValue("cast/dlna_enabled", False)
    qs.sync()
    assert _type_enabled("dlna") is False
    assert _type_enabled("chromecast") is True
    assert _type_enabled("airplay") is True
    assert _type_enabled("sonos") is True
    assert _type_enabled("snapcast") is True
