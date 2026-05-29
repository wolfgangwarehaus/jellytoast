"""Tests for the native Sonos cast backend (``modules.cast.sonos``).

Everything in here mocks the soco SOAP transport — no live network,
no QApplication. Tests cover:

- DIDL-Lite metadata builder (escaping + namespace correctness)
- Discovery + topology expansion (single zone, group, S1+S2, invisible
  members, multiple seeds, error paths)
- Coordinator resolution (the play_uri-on-a-member silent-failure
  guard)
- ``cast_to_sonos`` push (cast_proxy hop, DIDL passed through,
  failure modes, volume-floor side-effect)
- Transport helpers (stop/pause/play/seek)
- Volume + per-member volume
- Group join / unjoin
- Settings honoring (sonos_enabled gate, volume floor)
- ``SonosEventBridge`` (subscribe + drain + watchdog + signal emit)

The bridge tests deliberately don't construct a real QEventLoop; the
poll + watchdog are driven by hand by calling the private slots.
PySide6 signals dispatch synchronously when connected directly to a
plain Python callable, which is all we need to verify emission.

These tests have never run against a real Sonos speaker — every
externalised behavior is contract-verified through the mock.
"""

from __future__ import annotations

import importlib
import sys
import types
from typing import List
from unittest.mock import MagicMock

import pytest

from modules.cast import sonos as sonos_mod
from modules.cast.sonos import (
    SonosEventBridge,
    SonosZone,
    build_didl,
    cast_to_sonos,
    discover_sonos,
    expand_topology,
    join_group,
    members_for_zone,
    pause_sonos,
    play_sonos,
    resolve_coordinator,
    seek_sonos,
    set_volume,
    stop_sonos,
    unjoin,
)

# ── Helpers ────────────────────────────────────────────────────────────────


def _make_player(
    uid="RINCON_AAA", ip="192.168.1.10", name="Living Room", is_visible=True, volume=30
):
    """One MagicMock pretending to be a soco.SoCo instance.

    Sets only the attributes the production code reads. Volume is a
    real attribute so the setter exercise actually mutates state.

    ``group`` is explicitly set to None so ``resolve_coordinator``
    treats this as a standalone player and returns the mock itself.
    Tests that exercise the group-resolution path build their own
    ``.group`` attribute.
    """
    p = MagicMock(name=name)
    p.uid = uid
    p.ip_address = ip
    p.player_name = name
    p.is_visible = is_visible
    p.volume = volume
    p.group = None
    # ``play_uri`` is what we usually assert on; default to no-op
    p.play_uri = MagicMock()
    p.pause = MagicMock()
    p.play = MagicMock()
    p.stop = MagicMock()
    p.seek = MagicMock()
    p.join = MagicMock()
    p.unjoin = MagicMock()
    return p


def _make_group(coordinator, members=None, label=None):
    g = MagicMock(name=f"group-{coordinator.uid}")
    g.coordinator = coordinator
    g.members = members or [coordinator]
    g.label = label or coordinator.player_name
    g.volume = 35
    return g


def _seed_with_groups(*groups):
    """A seed SoCo whose ``all_groups`` returns ``groups``. The seed
    itself is *also* part of one of them; soco's contract is that any
    one player can answer for the entire household."""
    seed = MagicMock(name="seed-player")
    seed.all_groups = list(groups)
    seed.uid = groups[0].coordinator.uid if groups else "RINCON_SEED"
    return seed


@pytest.fixture(autouse=True)
def _reset_soco_module_state(monkeypatch):
    """Each test starts with soco "available" — the import has already
    happened at module load, but resetting to a known state keeps the
    ``_ensure_soco`` lazy path's expected behavior stable when a test
    patches ``soco`` directly to a stub."""
    monkeypatch.setattr(sonos_mod, "_SOCO_AVAILABLE", True)
    yield


# ── DIDL builder ───────────────────────────────────────────────────────────


def test_didl_includes_required_namespaces():
    didl = build_didl("Title", artist="Artist", album="Album")
    assert 'xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/"' in didl
    assert 'xmlns:dc="http://purl.org/dc/elements/1.1/"' in didl
    assert 'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/"' in didl


def test_didl_includes_track_class():
    didl = build_didl("T", artist="A", album="Al")
    assert "<upnp:class>object.item.audioItem.musicTrack</upnp:class>" in didl


def test_didl_escapes_xml_specials_in_title():
    didl = build_didl('She said "yes" & <run>', artist="Foo")
    # Greater-than is *not* escaped by saxutils.escape by default,
    # but ampersand, less-than, and double-quote (when relevant) are.
    assert "She said" in didl
    assert "&amp;" in didl
    assert "&lt;run&gt;" in didl
    # Original unescaped ampersand should not be present.
    assert " & " not in didl.replace("&amp;", "")


def test_didl_omits_album_art_when_empty():
    didl = build_didl("T", artist="A", album="Al", art_url="")
    assert "albumArtURI" not in didl


def test_didl_includes_album_art_when_provided():
    didl = build_didl("T", album="Al", art_url="http://host/img.jpg")
    assert "<upnp:albumArtURI>http://host/img.jpg</upnp:albumArtURI>" in didl


def test_didl_escapes_art_url():
    didl = build_didl("T", art_url="http://h/?a=1&b=2")
    assert "<upnp:albumArtURI>http://h/?a=1&amp;b=2</upnp:albumArtURI>" in didl


def test_didl_blank_fields_dont_crash():
    didl = build_didl("", artist="", album="", art_url="")
    assert "<dc:title></dc:title>" in didl
    assert "<dc:creator></dc:creator>" in didl


# ── Discovery + topology ──────────────────────────────────────────────────


def test_discover_honors_sonos_enabled_off(monkeypatch):
    stub_settings = MagicMock()
    stub_settings.sonos_enabled = False
    monkeypatch.setattr("modules.settings.get_settings", lambda: stub_settings)
    # Even if soco.discover would return something, the gate short-
    # circuits to []. Patch discover so a failure here would be loud.
    stub_soco = MagicMock()
    stub_soco.discover = MagicMock(return_value={_make_player()})
    monkeypatch.setattr(sonos_mod, "soco", stub_soco)
    assert discover_sonos(timeout=0.1) == []
    stub_soco.discover.assert_not_called()


def test_discover_returns_empty_when_soco_missing(monkeypatch):
    stub_settings = MagicMock()
    stub_settings.sonos_enabled = True
    monkeypatch.setattr("modules.settings.get_settings", lambda: stub_settings)
    monkeypatch.setattr(sonos_mod, "_SOCO_AVAILABLE", False)
    monkeypatch.setattr(sonos_mod, "soco", None)
    assert discover_sonos(timeout=0.1) == []


def test_discover_returns_empty_when_no_zones_on_lan(monkeypatch):
    stub_settings = MagicMock()
    stub_settings.sonos_enabled = True
    monkeypatch.setattr("modules.settings.get_settings", lambda: stub_settings)
    stub_soco = MagicMock()
    stub_soco.discover = MagicMock(return_value=set())
    monkeypatch.setattr(sonos_mod, "soco", stub_soco)
    assert discover_sonos(timeout=0.1) == []


def test_discover_swallows_socket_errors(monkeypatch):
    stub_settings = MagicMock()
    stub_settings.sonos_enabled = True
    monkeypatch.setattr("modules.settings.get_settings", lambda: stub_settings)
    stub_soco = MagicMock()
    stub_soco.discover = MagicMock(side_effect=OSError("no multicast"))
    monkeypatch.setattr(sonos_mod, "soco", stub_soco)
    assert discover_sonos(timeout=0.1) == []


def test_expand_topology_single_player_single_group():
    p = _make_player()
    g = _make_group(p)
    zones = expand_topology([_seed_with_groups(g)])
    assert len(zones) == 1
    z = zones[0]
    assert z.uuid == p.uid
    assert z.coordinator is p
    assert z.member_uuids == [p.uid]
    assert not z.is_group


def test_expand_topology_multi_player_group():
    a = _make_player(uid="RINCON_A", name="Kitchen")
    b = _make_player(uid="RINCON_B", name="Patio")
    g = _make_group(a, members=[a, b], label="Kitchen + Patio")
    zones = expand_topology([_seed_with_groups(g)])
    assert len(zones) == 1
    z = zones[0]
    assert z.is_group is True
    assert set(z.member_uuids) == {"RINCON_A", "RINCON_B"}
    assert z.label == "Kitchen + Patio"


def test_expand_topology_skips_invisible_members():
    # A soundbar with a bonded sub: sub is invisible to the picker.
    bar = _make_player(uid="RINCON_BAR", name="Beam")
    sub = _make_player(uid="RINCON_SUB", name="Sub", is_visible=False)
    g = _make_group(bar, members=[bar, sub])
    zones = expand_topology([_seed_with_groups(g)])
    assert len(zones) == 1
    assert zones[0].member_uuids == ["RINCON_BAR"]


def test_expand_topology_dedupes_across_seeds():
    p = _make_player()
    g = _make_group(p)
    # Two seeds both report the same group — should produce one zone.
    seeds = [_seed_with_groups(g), _seed_with_groups(g)]
    zones = expand_topology(seeds)
    assert len(zones) == 1


def test_expand_topology_sorts_alphabetically():
    a = _make_player(uid="RINCON_A", name="Zenith")
    b = _make_player(uid="RINCON_B", name="Aardvark")
    g_a = _make_group(a, label="Zenith")
    g_b = _make_group(b, label="Aardvark")
    # Seed reports both groups; expand should sort by label.
    seed = MagicMock(name="seed")
    seed.all_groups = [g_a, g_b]
    zones = expand_topology([seed])
    assert [z.label for z in zones] == ["Aardvark", "Zenith"]


def test_expand_topology_empty_seeds_returns_empty():
    assert expand_topology([]) == []
    assert expand_topology([None]) == []


def test_expand_topology_swallows_group_errors():
    p = _make_player()
    g_good = _make_group(p)
    g_bad = MagicMock()
    # ``coordinator`` access raises — we should skip and keep going.
    type(g_bad).coordinator = property(
        fget=lambda self: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    seed = MagicMock()
    seed.all_groups = [g_bad, g_good]
    zones = expand_topology([seed])
    assert len(zones) == 1
    assert zones[0].uuid == p.uid


def test_expand_topology_handles_all_groups_failure():
    seed = MagicMock()
    type(seed).all_groups = property(
        fget=lambda self: (_ for _ in ()).throw(RuntimeError("net down"))
    )
    assert expand_topology([seed]) == []


# ── Coordinator resolution ───────────────────────────────────────────────


def test_resolve_coordinator_from_zone_returns_coord():
    p = _make_player()
    zone = SonosZone(
        uuid=p.uid,
        label="L",
        coordinator_ip=p.ip_address,
        coordinator_uuid=p.uid,
        coordinator=p,
    )
    assert resolve_coordinator(zone) is p


def test_resolve_coordinator_from_member_returns_group_coord():
    coord = _make_player(uid="RINCON_C", name="Coord")
    member = _make_player(uid="RINCON_M", name="Member")
    member.group = _make_group(coord, members=[coord, member])
    assert resolve_coordinator(member) is coord


def test_resolve_coordinator_passthrough_for_standalone():
    p = _make_player()
    # No .group attribute (or coordinator falsy) — pass through.
    del p.group
    p.group = None
    assert resolve_coordinator(p) is p


def test_resolve_coordinator_handles_none():
    assert resolve_coordinator(None) is None


# ── cast_to_sonos ────────────────────────────────────────────────────────


def test_cast_to_sonos_pushes_through_proxy(monkeypatch):
    p = _make_player()
    zone = SonosZone(
        uuid=p.uid, label="L", coordinator_ip=p.ip_address, coordinator_uuid=p.uid, coordinator=p
    )
    monkeypatch.setattr("modules.cast_proxy.resolve_cast_url", lambda u: f"PROXY({u})" if u else u)
    # No volume floor to keep this test focused.
    ok = cast_to_sonos(
        zone,
        "http://server/track1.mp3",
        title="T",
        artist="A",
        album="Al",
        art_url="http://server/cover2.jpg",
        apply_volume_floor=False,
    )
    assert ok is True
    p.play_uri.assert_called_once()
    args, kwargs = p.play_uri.call_args
    # The URL handed to the speaker is the proxy URL, not the upstream.
    pushed_url = args[0] if args else kwargs.get("uri")
    assert pushed_url == "PROXY(http://server/track1.mp3)"
    # DIDL meta should be present and well-formed.
    didl = kwargs.get("meta", args[1] if len(args) > 1 else "")
    assert "<DIDL-Lite" in didl
    assert "<dc:title>T</dc:title>" in didl


def test_cast_to_sonos_resolves_member_to_coordinator(monkeypatch):
    coord = _make_player(uid="RINCON_C", name="Coord")
    member = _make_player(uid="RINCON_M", name="Member")
    member.group = _make_group(coord, members=[coord, member])
    monkeypatch.setattr("modules.cast_proxy.resolve_cast_url", lambda u: u)
    ok = cast_to_sonos(member, "http://server/x.mp3", title="X", apply_volume_floor=False)
    assert ok is True
    # play_uri must hit the coordinator, never the member.
    coord.play_uri.assert_called_once()
    member.play_uri.assert_not_called()


def test_cast_to_sonos_returns_false_on_no_coordinator():
    assert cast_to_sonos(None, "http://x") is False


def test_cast_to_sonos_returns_false_on_soco_unavailable(monkeypatch):
    monkeypatch.setattr(sonos_mod, "_SOCO_AVAILABLE", False)
    monkeypatch.setattr(sonos_mod, "soco", None)
    p = _make_player()
    assert cast_to_sonos(p, "http://x") is False


def test_cast_to_sonos_returns_false_on_soco_exception(monkeypatch):
    p = _make_player()
    p.play_uri = MagicMock(side_effect=sonos_mod.SoCoException("rejected"))
    monkeypatch.setattr("modules.cast_proxy.resolve_cast_url", lambda u: u)
    assert cast_to_sonos(p, "http://x", apply_volume_floor=False) is False


def test_cast_to_sonos_returns_false_on_unexpected_exception(monkeypatch):
    p = _make_player()
    p.play_uri = MagicMock(side_effect=RuntimeError("anything else"))
    monkeypatch.setattr("modules.cast_proxy.resolve_cast_url", lambda u: u)
    assert cast_to_sonos(p, "http://x", apply_volume_floor=False) is False


def test_cast_to_sonos_falls_back_when_proxy_import_fails(monkeypatch):
    p = _make_player()
    # Simulate cast_proxy import failure: temporarily evict it from
    # sys.modules and force ImportError on re-import. The production
    # code wraps the import in try/except and degrades to identity.
    real_proxy = sys.modules.pop("modules.cast_proxy", None)
    monkeypatch.setitem(sys.modules, "modules.cast_proxy", types.ModuleType("modules.cast_proxy"))
    # The stub module has no ``resolve_cast_url`` attribute → ImportError
    # path when we do ``from ... import resolve_cast_url``.
    try:
        ok = cast_to_sonos(p, "http://upstream/x", title="X", apply_volume_floor=False)
        assert ok is True
        # Without proxy: identity — speaker sees the upstream URL.
        args, _ = p.play_uri.call_args
        assert args[0] == "http://upstream/x"
    finally:
        if real_proxy is not None:
            sys.modules["modules.cast_proxy"] = real_proxy
        else:
            sys.modules.pop("modules.cast_proxy", None)


def test_cast_to_sonos_applies_volume_floor(monkeypatch):
    p = _make_player(volume=5)
    p.group = _make_group(p)
    p.group.volume = 5
    stub_settings = MagicMock()
    stub_settings.sonos_volume_floor = 20
    monkeypatch.setattr("modules.settings.get_settings", lambda: stub_settings)
    monkeypatch.setattr("modules.cast_proxy.resolve_cast_url", lambda u: u)
    ok = cast_to_sonos(p, "http://x", apply_volume_floor=True)
    assert ok is True
    # Floor raised volume from 5 -> 20 on the group.
    assert p.group.volume == 20


def test_cast_to_sonos_volume_floor_skipped_when_above(monkeypatch):
    p = _make_player(volume=40)
    p.group = _make_group(p)
    p.group.volume = 40
    stub_settings = MagicMock()
    stub_settings.sonos_volume_floor = 20
    monkeypatch.setattr("modules.settings.get_settings", lambda: stub_settings)
    monkeypatch.setattr("modules.cast_proxy.resolve_cast_url", lambda u: u)
    cast_to_sonos(p, "http://x", apply_volume_floor=True)
    assert p.group.volume == 40  # unchanged


def test_cast_to_sonos_volume_floor_zero_is_no_op(monkeypatch):
    p = _make_player(volume=3)
    p.group = _make_group(p)
    p.group.volume = 3
    stub_settings = MagicMock()
    stub_settings.sonos_volume_floor = 0
    monkeypatch.setattr("modules.settings.get_settings", lambda: stub_settings)
    monkeypatch.setattr("modules.cast_proxy.resolve_cast_url", lambda u: u)
    cast_to_sonos(p, "http://x", apply_volume_floor=True)
    assert p.group.volume == 3  # untouched


def test_cast_to_sonos_proxies_art_url_too(monkeypatch):
    p = _make_player()
    calls: List[str] = []

    def fake_resolve(u):
        calls.append(u)
        return f"PROXY({u})" if u else u

    monkeypatch.setattr("modules.cast_proxy.resolve_cast_url", fake_resolve)
    cast_to_sonos(
        p, "http://server/track.mp3", art_url="http://server/cover.jpg", apply_volume_floor=False
    )
    assert "http://server/track.mp3" in calls
    assert "http://server/cover.jpg" in calls
    args, kwargs = p.play_uri.call_args
    didl = kwargs.get("meta")
    assert "PROXY(http://server/cover.jpg)" in didl


# ── Transport helpers ─────────────────────────────────────────────────────


def test_stop_sonos_calls_coordinator_stop():
    p = _make_player()
    assert stop_sonos(p) is True
    p.stop.assert_called_once()


def test_stop_sonos_returns_false_on_exception():
    p = _make_player()
    p.stop = MagicMock(side_effect=sonos_mod.SoCoException("nope"))
    assert stop_sonos(p) is False


def test_stop_sonos_no_coordinator():
    assert stop_sonos(None) is False


def test_pause_play_seek_dispatch_correctly():
    p = _make_player()
    assert pause_sonos(p) is True
    p.pause.assert_called_once()
    assert play_sonos(p) is True
    p.play.assert_called_once()
    assert seek_sonos(p, 83) is True
    # 83 sec = 0:01:23
    p.seek.assert_called_once_with("0:01:23")


def test_seek_sonos_formats_hours():
    p = _make_player()
    assert seek_sonos(p, 3725) is True  # 1h 02m 05s
    p.seek.assert_called_once_with("1:02:05")


def test_seek_sonos_clamps_negative_to_zero():
    p = _make_player()
    assert seek_sonos(p, -10) is True
    p.seek.assert_called_once_with("0:00:00")


def test_transport_helpers_return_false_on_exceptions():
    p = _make_player()
    p.pause = MagicMock(side_effect=RuntimeError())
    p.play = MagicMock(side_effect=RuntimeError())
    p.seek = MagicMock(side_effect=RuntimeError())
    assert pause_sonos(p) is False
    assert play_sonos(p) is False
    assert seek_sonos(p, 10) is False


# ── Volume ───────────────────────────────────────────────────────────────


def test_set_volume_on_zone_sets_group_volume():
    p = _make_player()
    p.group = _make_group(p)
    zone = SonosZone(
        uuid=p.uid, label="L", coordinator_ip=p.ip_address, coordinator_uuid=p.uid, coordinator=p
    )
    assert set_volume(zone, 55) is True
    assert p.group.volume == 55


def test_set_volume_on_zone_without_group_falls_back_to_coord():
    p = _make_player()
    p.group = None
    zone = SonosZone(
        uuid=p.uid, label="L", coordinator_ip=p.ip_address, coordinator_uuid=p.uid, coordinator=p
    )
    assert set_volume(zone, 22) is True
    assert p.volume == 22


def test_set_volume_clamps_to_0_100():
    p = _make_player()
    assert set_volume(p, 999) is True
    assert p.volume == 100
    assert set_volume(p, -50) is True
    assert p.volume == 0


def test_set_volume_on_raw_player():
    p = _make_player(volume=0)
    assert set_volume(p, 75) is True
    assert p.volume == 75


def test_set_volume_returns_false_on_exception():
    bad = MagicMock()
    type(bad).volume = property(
        fget=lambda self: 0, fset=lambda self, v: (_ for _ in ()).throw(RuntimeError("no"))
    )
    assert set_volume(bad, 10) is False


def test_members_for_zone_reports_uuid_name_volume():
    a = _make_player(uid="RINCON_A", name="Kitchen", volume=30)
    b = _make_player(uid="RINCON_B", name="Patio", volume=50)
    zone = SonosZone(
        uuid="RINCON_A",
        label="K+P",
        coordinator_ip="ip",
        coordinator_uuid="RINCON_A",
        coordinator=a,
        member_uuids=["RINCON_A", "RINCON_B"],
        member_names=["Kitchen", "Patio"],
        members=[a, b],
    )
    rows = members_for_zone(zone)
    assert rows == [("RINCON_A", "Kitchen", 30), ("RINCON_B", "Patio", 50)]


def test_members_for_zone_swallows_bad_volume_read():
    a = _make_player(uid="RINCON_A", name="Kitchen")
    type(a).volume = property(fget=lambda self: (_ for _ in ()).throw(RuntimeError("offline")))
    zone = SonosZone(
        uuid="RINCON_A",
        label="K",
        coordinator_ip="ip",
        coordinator_uuid="RINCON_A",
        coordinator=a,
        members=[a],
    )
    rows = members_for_zone(zone)
    assert rows == [("RINCON_A", "Kitchen", 50)]  # fallback default


# ── Grouping ──────────────────────────────────────────────────────────────


def test_join_group_member_joins_coordinator():
    coord = _make_player(uid="RINCON_C")
    member = _make_player(uid="RINCON_M")
    assert join_group(member, coord) is True
    member.join.assert_called_once_with(coord)


def test_join_group_accepts_zone_as_coordinator():
    coord = _make_player(uid="RINCON_C")
    zone = SonosZone(
        uuid=coord.uid,
        label="L",
        coordinator_ip=coord.ip_address,
        coordinator_uuid=coord.uid,
        coordinator=coord,
    )
    member = _make_player(uid="RINCON_M")
    assert join_group(member, zone) is True
    member.join.assert_called_once_with(coord)


def test_join_group_returns_false_on_exception():
    coord = _make_player(uid="RINCON_C")
    member = _make_player(uid="RINCON_M")
    member.join = MagicMock(side_effect=sonos_mod.SoCoException())
    assert join_group(member, coord) is False


def test_join_group_returns_false_with_no_coord():
    member = _make_player(uid="RINCON_M")
    assert join_group(member, None) is False


def test_unjoin_calls_player_unjoin():
    member = _make_player()
    assert unjoin(member) is True
    member.unjoin.assert_called_once()


def test_unjoin_returns_false_on_none():
    assert unjoin(None) is False


def test_unjoin_swallows_exception():
    member = _make_player()
    member.unjoin = MagicMock(side_effect=RuntimeError())
    assert unjoin(member) is False


# ── SonosEventBridge ─────────────────────────────────────────────────────


class _FakeQueue:
    """Drop-in for ``soco.events.Subscription.events`` — a stdlib
    queue-like with the ``get(timeout)`` contract soco exposes. Raises
    ``Empty`` (RuntimeError stand-in) when drained."""

    def __init__(self, items=None):
        self._items = list(items or [])

    def get(self, timeout=None):
        if not self._items:
            raise RuntimeError("Empty")
        return self._items.pop(0)


class _FakeEvent:
    def __init__(self, variables):
        self.variables = variables


class _FakeSubscription:
    def __init__(self, events=None, is_subscribed=True):
        self.events = _FakeQueue(events)
        self.is_subscribed = is_subscribed
        self.unsubscribed = 0

    def unsubscribe(self):
        self.unsubscribed += 1
        self.is_subscribed = False


class _FakeService:
    def __init__(self, sub=None, fail=False):
        self._sub = sub
        self._fail = fail
        self.subscribe_calls = 0

    def subscribe(self, auto_renew=False):
        self.subscribe_calls += 1
        if self._fail:
            raise sonos_mod.SoCoException("subscribe failed")
        return self._sub


def _make_coord_with_services(av_sub=None, rc_sub=None, av_fail=False, rc_fail=False):
    coord = MagicMock()
    coord.avTransport = _FakeService(av_sub, fail=av_fail)
    coord.renderingControl = _FakeService(rc_sub, fail=rc_fail)
    return coord


def test_bridge_start_subscribes_both_services(qapp):
    av_sub = _FakeSubscription()
    rc_sub = _FakeSubscription()
    coord = _make_coord_with_services(av_sub, rc_sub)
    bridge = SonosEventBridge(coord)
    try:
        assert bridge.start() is True
        assert coord.avTransport.subscribe_calls == 1
        assert coord.renderingControl.subscribe_calls == 1
        assert "avTransport" in bridge._subs
        assert "renderingControl" in bridge._subs
    finally:
        bridge.stop()


def test_bridge_start_idempotent(qapp):
    av_sub = _FakeSubscription()
    rc_sub = _FakeSubscription()
    coord = _make_coord_with_services(av_sub, rc_sub)
    bridge = SonosEventBridge(coord)
    try:
        bridge.start()
        bridge.start()  # second start should not re-subscribe
        assert coord.avTransport.subscribe_calls == 1
    finally:
        bridge.stop()


def test_bridge_start_fails_when_all_services_fail(qapp):
    coord = _make_coord_with_services(av_fail=True, rc_fail=True)
    bridge = SonosEventBridge(coord)
    try:
        assert bridge.start() is False
    finally:
        bridge.stop()


def test_bridge_start_partial_failure_still_starts(qapp):
    av_sub = _FakeSubscription()
    coord = _make_coord_with_services(av_sub, rc_fail=True)
    bridge = SonosEventBridge(coord)
    try:
        assert bridge.start() is True
        assert "avTransport" in bridge._subs
        assert "renderingControl" not in bridge._subs
    finally:
        bridge.stop()


def test_bridge_drains_events_and_emits_signals(qapp):
    events_seen: List = []
    states_seen: List[str] = []
    av_sub = _FakeSubscription(
        events=[
            _FakeEvent({"transport_state": "PLAYING", "current_track_uri": "http://x/a.mp3"}),
            _FakeEvent({"transport_state": "PAUSED"}),
        ]
    )
    rc_sub = _FakeSubscription(
        events=[
            _FakeEvent({"volume": 35}),
        ]
    )
    coord = _make_coord_with_services(av_sub, rc_sub)
    bridge = SonosEventBridge(coord)
    bridge.event_received.connect(lambda svc, vars_: events_seen.append((svc, vars_)))
    bridge.transport_state.connect(states_seen.append)
    try:
        bridge.start()
        # Drive the drain manually rather than spinning a QEventLoop.
        bridge._drain_event_queues()
        # avTransport: two events, both emitted. renderingControl: one.
        assert len(events_seen) == 3
        # transport_state signal fires only for avTransport events
        # that carry a transport_state variable.
        assert states_seen == ["PLAYING", "PAUSED"]
    finally:
        bridge.stop()


def test_bridge_handles_empty_event_queue(qapp):
    coord = _make_coord_with_services(_FakeSubscription(), _FakeSubscription())
    bridge = SonosEventBridge(coord)
    seen: List = []
    bridge.event_received.connect(lambda *a: seen.append(a))
    try:
        bridge.start()
        bridge._drain_event_queues()
        assert seen == []
    finally:
        bridge.stop()


def test_bridge_stop_unsubscribes_all(qapp):
    av_sub = _FakeSubscription()
    rc_sub = _FakeSubscription()
    coord = _make_coord_with_services(av_sub, rc_sub)
    bridge = SonosEventBridge(coord)
    bridge.start()
    bridge.stop()
    assert av_sub.unsubscribed == 1
    assert rc_sub.unsubscribed == 1
    assert bridge._subs == {}


def test_bridge_stop_is_idempotent(qapp):
    av_sub = _FakeSubscription()
    rc_sub = _FakeSubscription()
    coord = _make_coord_with_services(av_sub, rc_sub)
    bridge = SonosEventBridge(coord)
    bridge.start()
    bridge.stop()
    bridge.stop()  # second stop should not blow up
    assert av_sub.unsubscribed == 1


def test_bridge_watchdog_renews_dropped_subscription(qapp):
    failed_signals: List[str] = []
    # First sub claims it's no longer subscribed; second sub is alive.
    dead_sub = _FakeSubscription(is_subscribed=False)
    fresh_sub = _FakeSubscription(is_subscribed=True)
    fresh_rc = _FakeSubscription()
    # Use a service that hands out dead_sub first, then fresh_sub.
    seq = [dead_sub, fresh_sub]

    class _SeqService:
        def __init__(self):
            self.calls = 0

        def subscribe(self, auto_renew=False):
            self.calls += 1
            return seq.pop(0)

    coord = MagicMock()
    coord.avTransport = _SeqService()
    coord.renderingControl = _FakeService(fresh_rc)
    bridge = SonosEventBridge(coord)
    bridge.subscription_failed.connect(failed_signals.append)
    try:
        assert bridge.start() is True
        # Drive the watchdog manually.
        bridge._renew_if_stale()
        # avTransport: dead -> renewed (no failure signal).
        # renderingControl: alive -> untouched.
        assert coord.avTransport.calls == 2
        assert failed_signals == []
        assert bridge._subs["avTransport"] is fresh_sub
    finally:
        bridge.stop()


def test_bridge_watchdog_emits_failure_when_renew_fails(qapp):
    failed_signals: List[str] = []
    dead_sub = _FakeSubscription(is_subscribed=False)

    class _OnceThenFail:
        def __init__(self):
            self.calls = 0

        def subscribe(self, auto_renew=False):
            self.calls += 1
            if self.calls == 1:
                return dead_sub
            raise sonos_mod.SoCoException("speaker is gone")

    coord = MagicMock()
    coord.avTransport = _OnceThenFail()
    coord.renderingControl = _FakeService(_FakeSubscription())
    bridge = SonosEventBridge(coord)
    bridge.subscription_failed.connect(failed_signals.append)
    try:
        bridge.start()
        bridge._renew_if_stale()
        assert failed_signals == ["avTransport"]
    finally:
        bridge.stop()


def test_bridge_drain_bounded_per_tick(qapp):
    # Hand the bridge a queue with 100 events. A single drain pass
    # should consume at most 16 (the production cap).
    events = [_FakeEvent({"transport_state": "PLAYING"}) for _ in range(100)]
    av_sub = _FakeSubscription(events=events)
    coord = _make_coord_with_services(av_sub, _FakeSubscription())
    bridge = SonosEventBridge(coord)
    seen: List = []
    bridge.event_received.connect(lambda *a: seen.append(a))
    try:
        bridge.start()
        bridge._drain_event_queues()
        assert len(seen) == 16
    finally:
        bridge.stop()


def test_bridge_silence_window_triggers_renew(monkeypatch, qapp):
    """Even when ``is_subscribed`` is True, a long silence should
    force a renew — the doc's SoCo #822 mitigation."""
    av_sub = _FakeSubscription(is_subscribed=True)
    fresh = _FakeSubscription(is_subscribed=True)
    seq = [av_sub, fresh]

    class _SeqService:
        def __init__(self):
            self.calls = 0

        def subscribe(self, auto_renew=False):
            self.calls += 1
            return seq.pop(0)

    coord = MagicMock()
    coord.avTransport = _SeqService()
    coord.renderingControl = _FakeService(_FakeSubscription())
    bridge = SonosEventBridge(coord)
    try:
        bridge.start()
        # Backdate the last-event timestamp to well before the silence
        # window so the watchdog believes events have stopped. We use
        # ``-2 * SILENCE_LIMIT_S`` (monotonic clocks can start at any
        # epoch — a tiny fresh process clock would otherwise leave us
        # under the threshold).
        import time as _t

        bridge._last_event_ts["avTransport"] = _t.monotonic() - 2 * bridge.SILENCE_LIMIT_S
        bridge._renew_if_stale()
        # avTransport renewed; rc untouched.
        assert coord.avTransport.calls == 2
    finally:
        bridge.stop()


# ── is_available + sundry ────────────────────────────────────────────────


def test_is_available_returns_true_when_soco_imported(monkeypatch):
    monkeypatch.setattr(sonos_mod, "_SOCO_AVAILABLE", True)
    assert sonos_mod.is_available() is True


def test_is_available_returns_false_when_missing(monkeypatch):
    monkeypatch.setattr(sonos_mod, "_SOCO_AVAILABLE", False)
    assert sonos_mod.is_available() is False


def test_ensure_soco_handles_import_error(monkeypatch):
    # Force the cached flag to None so _ensure_soco re-runs the
    # import — and mock the import to fail.
    monkeypatch.setattr(sonos_mod, "_SOCO_AVAILABLE", None)
    monkeypatch.setattr(sonos_mod, "soco", None)
    real_import = importlib.__import__

    def _fail_import(name, *a, **kw):
        if name == "soco" or name.startswith("soco."):
            raise ImportError("simulated missing dep")
        return real_import(name, *a, **kw)

    monkeypatch.setattr("builtins.__import__", _fail_import)
    assert sonos_mod._ensure_soco() is False
    assert sonos_mod._SOCO_AVAILABLE is False


# ── Settings integration ────────────────────────────────────────────────


def test_settings_defaults_for_sonos_keys():
    from modules.settings import Settings

    s = Settings()
    # Defaults per the research doc + the autonomous-task spec.
    assert s.sonos_enabled is True
    assert s.sonos_preferred_zone == ""
    assert s.sonos_group_with_master is False
    assert s.sonos_event_port == 0
    assert s.sonos_volume_floor == 15


def test_settings_round_trip_sonos_keys():
    from modules.settings import Settings

    s = Settings()
    s.sonos_enabled = False
    s.sonos_preferred_zone = "RINCON_ABC"
    s.sonos_group_with_master = True
    s.sonos_event_port = 8945
    s.sonos_volume_floor = 30
    s2 = Settings()
    assert s2.sonos_enabled is False
    assert s2.sonos_preferred_zone == "RINCON_ABC"
    assert s2.sonos_group_with_master is True
    assert s2.sonos_event_port == 8945
    assert s2.sonos_volume_floor == 30
    # Cleanup so cross-test ordering is stable.
    s2.sonos_enabled = True
    s2.sonos_preferred_zone = ""
    s2.sonos_group_with_master = False
    s2.sonos_event_port = 0
    s2.sonos_volume_floor = 15


def test_settings_event_port_clamped_to_range():
    from modules.settings import Settings

    s = Settings()
    s.sonos_event_port = -1
    assert s.sonos_event_port == 0
    s.sonos_event_port = 99999
    assert s.sonos_event_port == 65535
    s.sonos_event_port = 0  # reset


def test_settings_volume_floor_clamped():
    from modules.settings import Settings

    s = Settings()
    s.sonos_volume_floor = -5
    assert s.sonos_volume_floor == 0
    s.sonos_volume_floor = 999
    assert s.sonos_volume_floor == 50
    s.sonos_volume_floor = 15  # reset
