"""
Chromecast + AirPlay (legacy v1 mDNS + modern pyatv AirPlay 2) cast
manager. AirPlay 2 receivers route through ``modules/airplay2.py``
(pyatv) when the library is installed; the v1 mDNS discovery path
remains as a fallback for older receivers. Newer cast backends
(DLNA, Sonos, Snapcast) live under ``modules/cast/``; this package is
Chromecast + AirPlay only.

Package layout (split from the former 794-line ``cast_manager.py``):

    _common.py      CastDevice dataclass, _AirPlayListener, _type_enabled
    _chromecast.py  _ChromecastMixin — discovery + transport + groups
    _airplay.py     _AirplayMixin — v1 mDNS + pyatv discovery + transport
    _manager.py     CastManager — thin orchestrator composing both mixins

Monkeypatch contract — load-bearing. ``tests/test_cast_gating.py``
patches module-level names on *this package's* namespace
(``modules.cast_manager``): ``pychromecast``, ``CHROMECAST_AVAILABLE``,
``ZEROCONF_AVAILABLE``, ``Zeroconf``, ``ServiceBrowser``, ``run_async``,
plus the CastBrowser trio (``CastBrowser``, ``SimpleCastListener``,
``get_chromecast_from_cast_info``) and ``DISCOVERY_WINDOW_S``.

All therefore live here in ``__init__.py``, not in a submodule.
The mixin code reads them back through the package
(``from modules import cast_manager as _pkg; _pkg.pychromecast``) at
call time, never via a frozen ``from`` import — so a test patch is the
binding the code actually sees. The ``_ensure_*`` lazy-import gates
likewise live here and ``global``-mutate this namespace, so the
``CHROMECAST_AVAILABLE`` / ``ZEROCONF_AVAILABLE`` flags a test sets are
honoured and the real heavyweight import is never reached.
"""

import logging
from typing import Optional

from modules.async_io import run_async

from ._common import CastDevice, _AirPlayListener, _type_enabled
from ._manager import CastManager

# Lazy-import the cast / mDNS deps. pychromecast pulls protobuf +
# zeroconf transitively at import (~80-200ms cold) and we only need it
# when the user actually opens the cast dialog. The flags are computed
# on first access via `_ensure_*` so callers can still gate behavior.
pychromecast = None  # type: ignore[assignment]
CastBrowser = None  # type: ignore[assignment]
SimpleCastListener = None  # type: ignore[assignment]
get_chromecast_from_cast_info = None  # type: ignore[assignment]
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
    global get_chromecast_from_cast_info, CHROMECAST_AVAILABLE
    if CHROMECAST_AVAILABLE is None:
        try:
            import pychromecast as _pc
            from pychromecast.discovery import (
                CastBrowser as _CB,
                SimpleCastListener as _SCL,
            )

            pychromecast = _pc
            CastBrowser = _CB
            SimpleCastListener = _SCL
            get_chromecast_from_cast_info = _pc.get_chromecast_from_cast_info
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
            from zeroconf import Zeroconf as _Zc, ServiceBrowser as _Sb

            Zeroconf = _Zc
            ServiceBrowser = _Sb
            ZEROCONF_AVAILABLE = True
        except ImportError:
            ZEROCONF_AVAILABLE = False
    return bool(ZEROCONF_AVAILABLE)


# Re-exported for the public import surface and the test monkeypatch
# contract. ruff would flag CastDevice / _AirPlayListener / _type_enabled
# / run_async as unused (F401) since this file only re-exports them — the
# explicit __all__ documents that the re-export is intentional.
__all__ = [
    "CastManager",  # public — every importer pulls this
    "CastDevice",  # public — now_playing_bar imports it
    "_AirPlayListener",  # re-exported for completeness / tests
    "_type_enabled",  # re-exported for completeness / tests
    "run_async",  # monkeypatched by tests/test_cast_gating.py
    "pychromecast",  # monkeypatched by tests/test_cast_gating.py
    "CastBrowser",  # monkeypatched by tests/test_cast_gating.py
    "SimpleCastListener",  # monkeypatched by tests/test_cast_gating.py
    "get_chromecast_from_cast_info",  # monkeypatched by tests/test_cast_gating.py
    "DISCOVERY_WINDOW_S",  # monkeypatched by tests/test_cast_gating.py
    "Zeroconf",  # monkeypatched by tests/test_cast_gating.py
    "ServiceBrowser",  # monkeypatched by tests/test_cast_gating.py
    "CHROMECAST_AVAILABLE",  # monkeypatched by tests/test_cast_gating.py
    "ZEROCONF_AVAILABLE",  # monkeypatched by tests/test_cast_gating.py
    "_ensure_chromecast",
    "_ensure_zeroconf",
]
