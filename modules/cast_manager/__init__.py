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
patches six module-level names on *this package's* namespace
(``modules.cast_manager``): ``pychromecast``, ``CHROMECAST_AVAILABLE``,
``ZEROCONF_AVAILABLE``, ``Zeroconf``, ``ServiceBrowser``, ``run_async``.

All six therefore live here in ``__init__.py``, not in a submodule.
The mixin code reads them back through the package
(``from modules import cast_manager as _pkg; _pkg.pychromecast``) at
call time, never via a frozen ``from`` import — so a test patch is the
binding the code actually sees. The ``_ensure_*`` lazy-import gates
likewise live here and ``global``-mutate this namespace, so the
``CHROMECAST_AVAILABLE`` / ``ZEROCONF_AVAILABLE`` flags a test sets are
honoured and the real heavyweight import is never reached.
"""

from typing import Optional

from modules.async_io import run_async

from ._common import CastDevice, _AirPlayListener, _type_enabled
from ._manager import CastManager

# Lazy-import the cast / mDNS deps. pychromecast pulls protobuf +
# zeroconf transitively at import (~80-200ms cold) and we only need it
# when the user actually opens the cast dialog. The flags are computed
# on first access via `_ensure_*` so callers can still gate behavior.
pychromecast = None  # type: ignore[assignment]
Zeroconf = None  # type: ignore[assignment]
ServiceBrowser = None  # type: ignore[assignment]
CHROMECAST_AVAILABLE: Optional[bool] = None
ZEROCONF_AVAILABLE: Optional[bool] = None


def _ensure_chromecast() -> bool:
    global pychromecast, CHROMECAST_AVAILABLE
    if CHROMECAST_AVAILABLE is None:
        try:
            import pychromecast as _pc

            pychromecast = _pc
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
    "Zeroconf",  # monkeypatched by tests/test_cast_gating.py
    "ServiceBrowser",  # monkeypatched by tests/test_cast_gating.py
    "CHROMECAST_AVAILABLE",  # monkeypatched by tests/test_cast_gating.py
    "ZEROCONF_AVAILABLE",  # monkeypatched by tests/test_cast_gating.py
    "_ensure_chromecast",
    "_ensure_zeroconf",
]
