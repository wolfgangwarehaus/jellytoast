"""
Chromecast + AirPlay (legacy v1 mDNS + modern pyatv AirPlay 2) cast
manager. AirPlay 2 receivers route through ``jellytoast/airplay2.py``
(pyatv) when the library is installed; the v1 mDNS discovery path
remains as a fallback for older receivers. Newer cast backends
(DLNA, Sonos, Snapcast) live under ``jellytoast/cast/``; this package is
Chromecast + AirPlay only.

Package layout (split from the former 794-line ``cast_manager.py``):

    _common.py      CastDevice dataclass, _AirPlayListener, _type_enabled
    _chromecast.py  _ChromecastMixin — discovery + transport + groups
    _airplay.py     _AirplayMixin — v1 mDNS + pyatv discovery + transport
    _manager.py     CastManager — thin orchestrator composing both mixins

Monkeypatch contract — load-bearing. ``tests/test_cast_gating.py``
patches module-level names on *this package's* namespace
(``jellytoast.cast_manager``): ``pychromecast``, ``CHROMECAST_AVAILABLE``,
``ZEROCONF_AVAILABLE``, ``Zeroconf``, ``ServiceBrowser``, ``run_async``,
plus the CastBrowser trio (``CastBrowser``, ``SimpleCastListener``,
``get_chromecast_from_cast_info``), ``DISCOVERY_WINDOW_S``, and
``_make_discovery_zeroconf`` (stubbed to keep the network out of the
gating tests).

All therefore live here in ``__init__.py``, not in a submodule.
The mixin code reads them back through the package
(``from jellytoast import cast_manager as _pkg; _pkg.pychromecast``) at
call time, never via a frozen ``from`` import — so a test patch is the
binding the code actually sees. The ``_ensure_*`` lazy-import gates
likewise live here and ``global``-mutate this namespace, so the
``CHROMECAST_AVAILABLE`` / ``ZEROCONF_AVAILABLE`` flags a test sets are
honoured and the real heavyweight import is never reached.
"""

import ipaddress
import logging
from typing import Optional

from jellytoast.async_io import run_async

from ._common import CastDevice, CastType, _AirPlayListener, _type_enabled
from ._manager import CastManager

# Lazy-import the cast / mDNS deps. pychromecast pulls protobuf +
# zeroconf transitively at import (~80-200ms cold) and we only need it
# when the user actually opens the cast dialog. The flags are computed
# on first access via `_ensure_*` so callers can still gate behavior.
pychromecast = None  # type: ignore[assignment]
CastBrowser = None  # type: ignore[assignment]
SimpleCastListener = None  # type: ignore[assignment]
get_chromecast_from_cast_info = None  # type: ignore[assignment]
get_chromecast_from_host = None  # type: ignore[assignment]
Zeroconf = None  # type: ignore[assignment]
ServiceBrowser = None  # type: ignore[assignment]
CHROMECAST_AVAILABLE: Optional[bool] = None
ZEROCONF_AVAILABLE: Optional[bool] = None

# How long the CastBrowser sweep listens for mDNS responses before we
# snapshot the discovered set. 3s matches the old `get_chromecasts(
# timeout=3)` balance — real Chromecasts answer well under a second,
# 3s is slack for marginal networks without making the dialog sluggish.
# Tests patch this to 0.0.
DISCOVERY_WINDOW_S: float = 3.0


def _ensure_chromecast() -> bool:
    global pychromecast, CastBrowser, SimpleCastListener
    global get_chromecast_from_cast_info, get_chromecast_from_host, CHROMECAST_AVAILABLE
    if CHROMECAST_AVAILABLE is None:
        try:
            import pychromecast as _pc
            from pychromecast.discovery import (
                CastBrowser as _CB,
            )
            from pychromecast.discovery import (
                SimpleCastListener as _SCL,
            )

            pychromecast = _pc
            CastBrowser = _CB
            SimpleCastListener = _SCL
            get_chromecast_from_cast_info = _pc.get_chromecast_from_cast_info
            # Host-based materialisation: connect straight to the
            # discovered host:port instead of re-resolving the device via
            # the zeroconf instance at connect time. The service path needs
            # a zeroconf whose loop is still running, but discovery's
            # CastBrowser stops that loop on stop_discovery — every cast
            # happens afterwards, so service-based connects fail
            # ("Zeroconf instance loop must be running"). Host-based needs
            # no live zeroconf. See reference_chromecast_tailscale_discovery.
            get_chromecast_from_host = _pc.get_chromecast_from_host
            # pychromecast 14.x still emits an INFO "discover_chromecasts
            # is deprecated…" line whenever the legacy entry point runs
            # internally. We've removed our caller (CastBrowser is the
            # replacement) but any future library codepath that touches
            # the deprecated helper would re-spam the log. Pin the
            # discovery sub-logger at WARNING so we get genuine
            # discovery failures but not the deprecation noise.
            logging.getLogger("pychromecast.discovery").setLevel(logging.WARNING)
            CHROMECAST_AVAILABLE = True
        except ImportError:
            CHROMECAST_AVAILABLE = False
    return bool(CHROMECAST_AVAILABLE)


def _ensure_zeroconf() -> bool:
    global Zeroconf, ServiceBrowser, ZEROCONF_AVAILABLE
    if ZEROCONF_AVAILABLE is None:
        try:
            from zeroconf import ServiceBrowser as _Sb
            from zeroconf import Zeroconf as _Zc

            Zeroconf = _Zc
            ServiceBrowser = _Sb
            ZEROCONF_AVAILABLE = True
        except ImportError:
            ZEROCONF_AVAILABLE = False
    return bool(ZEROCONF_AVAILABLE)


# Tailscale (and other WireGuard-style overlays) hand the host an address
# in the carrier-grade-NAT range. mDNS bound across that tunnel sends the
# _googlecast._tcp query out the overlay, where no Chromecast answers.
_OVERLAY_CGNAT = ipaddress.ip_network("100.64.0.0/10")


def _discovery_interfaces() -> Optional[list]:
    """Local IPv4 addresses zeroconf should bind to for Chromecast mDNS
    discovery, with the Tailscale/CGNAT overlay excluded.

    A default ``Zeroconf()`` binds across ALL interfaces. With a
    Tailscale tunnel (``tailscale0``, a ``100.64.0.0/10`` address) up,
    the ``_googlecast._tcp`` query can leave via the tunnel instead of
    the LAN and find nothing — Chromecasts only advertise on the local
    network. AirPlay (pyatv) and DLNA (SSDP) pick interfaces themselves,
    which is why only Chromecast regressed.

    Returns the non-overlay LAN addresses; if a host has ONLY an overlay
    address (a pure-Tailscale box, rare) returns those rather than
    nothing; ``None`` when enumeration is unavailable, so the caller
    falls back to zeroconf's default all-interface binding."""
    try:
        import ifaddr
    except Exception:
        return None
    real: list = []
    overlay: list = []
    try:
        adapters = ifaddr.get_adapters()
    except Exception:
        return None
    for adapter in adapters:
        for ip in getattr(adapter, "ips", []):
            if not getattr(ip, "is_IPv4", False):
                continue
            addr = ip.ip
            if not isinstance(addr, str):
                continue
            if addr.startswith("127.") or addr.startswith("169.254."):
                continue
            try:
                in_overlay = ipaddress.ip_address(addr) in _OVERLAY_CGNAT
            except ValueError:
                continue
            (overlay if in_overlay else real).append(addr)
    if real:
        return real
    if overlay:
        return overlay
    return None


def _lan_cidrs() -> list:
    """Human-facing LAN network CIDR(s) (e.g. ``192.168.50.0/24``) for the
    non-overlay interfaces — used by the Casting-page firewall hint to build a
    copy-paste ``allow from <subnet>`` rule. Mirrors _discovery_interfaces'
    interface selection but returns the network rather than the host address.
    Empty list when enumeration is unavailable."""
    try:
        import ifaddr
    except Exception:
        return []
    try:
        adapters = ifaddr.get_adapters()
    except Exception:
        return []
    out: list = []
    for adapter in adapters:
        for ip in getattr(adapter, "ips", []):
            if not getattr(ip, "is_IPv4", False):
                continue
            addr = ip.ip
            if not isinstance(addr, str):
                continue
            if addr.startswith("127.") or addr.startswith("169.254."):
                continue
            try:
                if ipaddress.ip_address(addr) in _OVERLAY_CGNAT:
                    continue
                prefix = int(getattr(ip, "network_prefix", 24) or 24)
                cidr = str(ipaddress.ip_network(f"{addr}/{prefix}", strict=False))
            except ValueError:
                continue
            if cidr not in out:
                out.append(cidr)
    return out


def _make_discovery_zeroconf():
    """Build a ``Zeroconf`` bound to the LAN interfaces (Tailscale
    excluded) for the Chromecast ``CastBrowser`` sweep, or ``None`` to
    let ``CastBrowser`` create its own default-bound instance.

    Isolated behind a single function so ``tests/test_cast_gating.py``
    can stub it to ``None`` and keep the network out of the gating
    tests. ``discover_chromecasts`` closes the returned instance after
    the sweep (``CastBrowser`` only closes the instance it created
    itself, i.e. when passed ``None``)."""
    ifaces = _discovery_interfaces()
    if not ifaces:
        return None
    try:
        import zeroconf as _zc

        return _zc.Zeroconf(interfaces=ifaces)
    except Exception as e:
        logging.getLogger(__name__).warning(
            "Chromecast discovery zeroconf bind to %s failed (%s); "
            "falling back to default binding",
            ifaces,
            e,
        )
        return None


# Re-exported for the public import surface and the test monkeypatch
# contract. ruff would flag CastDevice / _AirPlayListener / _type_enabled
# / run_async as unused (F401) since this file only re-exports them — the
# explicit __all__ documents that the re-export is intentional.
__all__ = [
    "CastManager",  # public — every importer pulls this
    "CastDevice",  # public — now_playing_bar imports it
    "CastType",  # public — str-backed cast-protocol enum
    "_AirPlayListener",  # re-exported for completeness / tests
    "_type_enabled",  # re-exported for completeness / tests
    "run_async",  # monkeypatched by tests/test_cast_gating.py
    "pychromecast",  # monkeypatched by tests/test_cast_gating.py
    "CastBrowser",  # monkeypatched by tests/test_cast_gating.py
    "SimpleCastListener",  # monkeypatched by tests/test_cast_gating.py
    "get_chromecast_from_cast_info",  # monkeypatched by tests/test_cast_gating.py
    "get_chromecast_from_host",  # monkeypatched by tests/test_cast_gating.py
    "DISCOVERY_WINDOW_S",  # monkeypatched by tests/test_cast_gating.py
    "Zeroconf",  # monkeypatched by tests/test_cast_gating.py
    "ServiceBrowser",  # monkeypatched by tests/test_cast_gating.py
    "CHROMECAST_AVAILABLE",  # monkeypatched by tests/test_cast_gating.py
    "ZEROCONF_AVAILABLE",  # monkeypatched by tests/test_cast_gating.py
    "_make_discovery_zeroconf",  # monkeypatched by tests/test_cast_gating.py
    "_discovery_interfaces",
    "_ensure_chromecast",
    "_ensure_zeroconf",
]
