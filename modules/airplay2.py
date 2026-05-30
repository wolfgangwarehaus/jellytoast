"""
AirPlay 2 client — thin wrapper around ``pyatv`` that bridges its
async-first API into jellytoast's synchronous worker-thread model.

``pyatv`` is the canonical pure-Python AirPlay 2 / DACP / MRP client.
It handles the HomeKit-style pairing handshake, the chacha20-poly1305
encrypted control channel, and RTSP audio streaming — all the things
that AirPlay 1's "POST /play with a URL" approach can't do. Newer
Apple TVs and AirPlay-2-capable receivers (LG webOS TVs, Sonos, HomePod,
etc.) only accept this protocol.

This module:
- Lazy-imports ``pyatv`` so a user without the dependency still gets
  AirPlay 1 fallback through ``cast_manager.cast_to_airplay``.
- Exposes synchronous helpers (``scan_sync``, ``play_url_sync``,
  ``pair_begin_sync`` / ``pair_finish_sync``) that run a fresh
  ``asyncio`` event loop per call — fine for one-shot operations
  invoked from the shared worker thread pool. A long-lived loop
  would let us keep a persistent connection but isn't necessary for
  the simple "play this URL" flow.
- Persists pairing credentials per device UUID in QSettings so the
  user only pairs once per receiver.

The pairing PIN dialog itself lives outside this module — this layer
just exposes the begin/finish primitives that the dialog drives.
"""

import logging
import os
from dataclasses import dataclass
from typing import List, Optional

logger = logging.getLogger(__name__)

# Set JT_AP2_DEBUG=1 to re-enable the play-by-play tracing this module
# emitted by default during the LG-webOS pairing investigation. Off in
# normal use — pairing dialogs and connect failures still print via
# the unconditional error paths in jellytoast.py.
_AP2_DBG = os.environ.get("JT_AP2_DEBUG") == "1"


def _dbg(msg: str) -> None:
    if _AP2_DBG:
        logger.debug("%s", msg)


pyatv = None  # type: ignore[assignment]
_PYATV_AVAILABLE: Optional[bool] = None


def is_available() -> bool:
    """True iff ``pyatv`` imported cleanly. Lazy so the import cost
    (and missing-dep failure mode) is paid once, on first cast."""
    global pyatv, _PYATV_AVAILABLE
    if _PYATV_AVAILABLE is None:
        try:
            import pyatv as _pa

            pyatv = _pa
            _install_lg_webos_compat()
            _PYATV_AVAILABLE = True
        except ImportError:
            _PYATV_AVAILABLE = False
    return bool(_PYATV_AVAILABLE)


def _install_lg_webos_compat() -> None:
    """LG webOS implements AirPlay 2 audio but rejects the post-``/play``
    RTSP property/rate setters that pyatv issues to every receiver
    (``PUT /setProperty?…``, ``POST /rate?…``). LG returns 501 Not
    Implemented and pyatv treats that as fatal, aborting before
    ``/rate`` runs — without ``/rate``, playback stays paused.

    The ``/play`` POST that actually queues the stream URL already uses
    ``allow_error=True`` upstream, so the cosmetic property exchanges
    can be made tolerant without affecting RTSP setup correctness.
    We mark the class once so repeated calls to ``is_available()`` are
    idempotent.
    """
    try:
        from pyatv.support.rtsp import RtspSession
    except Exception as e:
        _dbg(f"LG compat: rtsp import failed: {e}")
        return
    if getattr(RtspSession, "_jt_lg_patched", False):
        return
    _orig_exchange = RtspSession.exchange

    async def _exchange(self, method, uri=None, *args, allow_error=False, **kwargs):
        if uri and (
            (method == "PUT" and uri.startswith("/setProperty"))
            or (method == "POST" and uri.startswith("/rate"))
        ):
            allow_error = True
        return await _orig_exchange(self, method, uri, *args, allow_error=allow_error, **kwargs)

    RtspSession.exchange = _exchange
    RtspSession._jt_lg_patched = True
    _dbg("LG compat: RtspSession.exchange patched")


@dataclass
class AirPlay2Device:
    """Discovered AirPlay 2 receiver. ``config`` is the ``pyatv``
    ``AppleTV`` config object — opaque to the rest of the app but
    needed for connect / pair / play calls."""

    name: str
    host: str
    identifier: str
    requires_pairing: bool
    config: object


# ── Settings: per-device credentials ─────────────────────────────────────


def _credentials_key(identifier: str) -> str:
    return f"airplay2/credentials/{identifier}"


def get_stored_credentials(identifier: str) -> str:
    """Return the stored AirPlay 2 credentials blob for ``identifier``,
    or "" if the device hasn't been paired yet."""
    from modules.settings import get_settings

    return get_settings()._s.value(_credentials_key(identifier), "", type=str)


def store_credentials(identifier: str, credentials: str) -> None:
    from modules.settings import get_settings

    get_settings()._s.setValue(_credentials_key(identifier), credentials)


def forget_credentials(identifier: str) -> None:
    from modules.settings import get_settings

    get_settings()._s.remove(_credentials_key(identifier))


# ── Async bridge ─────────────────────────────────────────────────────────


def _run_async(coro):
    """Run an async coroutine on a fresh event loop on the calling
    thread. Blocks until the coroutine completes. Must be called from
    a worker thread (never the GUI thread) — pyatv operations involve
    network I/O that can stall for seconds.
    """
    import asyncio

    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(coro)
    finally:
        # Clean up any tasks pyatv left running before closing.
        try:
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        except Exception:
            pass
        loop.close()
        asyncio.set_event_loop(None)


# ── Scan ─────────────────────────────────────────────────────────────────


async def _scan_async(timeout: float = 3.0) -> List[AirPlay2Device]:
    if not is_available():
        return []
    import asyncio

    loop = asyncio.get_event_loop()
    from pyatv.const import PairingRequirement, Protocol

    configs = await pyatv.scan(loop, timeout=timeout)
    out: List[AirPlay2Device] = []
    for cfg in configs:
        # Only surface devices that actually expose the AirPlay
        # protocol — pyatv also scans for DACP / MRP / RAOP.
        airplay_svc = cfg.get_service(Protocol.AirPlay)
        if airplay_svc is None:
            continue
        # ``requires_pairing`` covers AirPlay 2 receivers that need a
        # paired key exchange. Older AirPlay 1 devices report False
        # and can be streamed to without credentials.
        # ``requires_pairing`` is True only when the receiver MANDATES a
        # paired key exchange (AirPlay 2). PairingRequirement members are
        # int-valued, so the old ``.value != "Disabled"`` compared an int
        # against a string — always True — and flagged EVERY device as
        # needing pairing, blocking AirPlay-1 / NotNeeded receivers.
        pairing = getattr(airplay_svc, "pairing", None)
        requires_pairing = bool(getattr(airplay_svc, "requires_password", False)) or (
            pairing == PairingRequirement.Mandatory
        )
        try:
            host = str(cfg.address)
        except Exception:
            host = ""
        out.append(
            AirPlay2Device(
                name=cfg.name,
                host=host,
                identifier=cfg.identifier or "",
                requires_pairing=requires_pairing,
                config=cfg,
            )
        )
    return out


def scan_sync(timeout: float = 3.0) -> List[AirPlay2Device]:
    """Blocking AirPlay 2 scan. Call from a worker thread."""
    if not is_available():
        return []
    return _run_async(_scan_async(timeout=timeout))


# ── Play / pair ──────────────────────────────────────────────────────────


class PairingRequired(Exception):
    """Raised by ``play_url_sync`` when the receiver demands paired
    credentials we don't have. The cast dialog surfaces this as a
    user-friendly "Pair this device first" message."""

    pass


async def _play_url_async(config, url: str, credentials: str = "") -> None:
    import asyncio

    from pyatv.const import Protocol

    loop = asyncio.get_event_loop()

    # AirPlay 2 receivers (LG webOS, HomePod, recent Apple TVs, etc.)
    # advertise both _airplay._tcp and _raop._tcp. The AirPlay protocol's
    # play_url path performs an extended RTSP property handshake that
    # LG webOS rejects (501 Not Implemented on /setProperty?…). RAOP +
    # stream_file avoids that handshake — receivers that implement
    # AirPlay 2 audio at all implement it via RAOP underneath. So when
    # the device has a RAOP service we always go through it.
    raop_svc = config.get_service(Protocol.RAOP)
    airplay_svc = config.get_service(Protocol.AirPlay)
    _dbg(f"_play_url_async: services raop={raop_svc is not None} airplay={airplay_svc is not None}")
    if credentials:
        _dbg(f"_play_url_async: setting credentials (len={len(credentials)})")
        # AirPlay 2 pairing produces credentials accepted by both
        # services on the same receiver — set them on whichever
        # services exist so connect() can authenticate either way.
        if airplay_svc is not None:
            config.set_credentials(Protocol.AirPlay, credentials)
        if raop_svc is not None:
            config.set_credentials(Protocol.RAOP, credentials)

    use_raop = raop_svc is not None
    target_protocol = Protocol.RAOP if use_raop else Protocol.AirPlay
    _dbg(f"_play_url_async: connecting via pyatv (Protocol.{target_protocol.name})…")
    try:
        atv = await pyatv.connect(config, loop, protocol=target_protocol)
    except Exception as e:
        _dbg(f"_play_url_async: connect raised {type(e).__name__}: {e}")
        # pyatv raises specific exceptions for missing credentials.
        # NoCredentialsError lives at module top in modern releases;
        # fall back to string match for older pyatv.
        msg = str(e).lower()
        if "credential" in msg or "pair" in msg or "password" in msg:
            raise PairingRequired(str(e)) from e
        raise
    try:
        if use_raop:
            _dbg("_play_url_async: connected; calling stream.stream_file")
            await atv.stream.stream_file(url)
            _dbg("_play_url_async: stream.stream_file completed")
        else:
            _dbg("_play_url_async: connected; calling stream.play_url")
            await atv.stream.play_url(url)
            _dbg("_play_url_async: stream.play_url completed")
    except Exception as e:
        _dbg(f"_play_url_async: stream raised {type(e).__name__}: {e}")
        raise
    finally:
        atv.close()


def play_url_sync(device: AirPlay2Device, url: str) -> None:
    """Play ``url`` on the given AirPlay 2 device. Raises
    ``PairingRequired`` if the receiver needs paired credentials we
    don't have stored. Call from a worker thread."""
    if not is_available():
        raise RuntimeError("pyatv is not installed")
    creds = get_stored_credentials(device.identifier)
    _dbg(
        f"play_url_sync: dev={device.name!r} "
        f"requires_pairing={device.requires_pairing} "
        f"stored_creds_len={len(creds)}"
    )
    if device.requires_pairing and not creds:
        raise PairingRequired(f"{device.name} needs pairing before it can accept casts.")
    _run_async(_play_url_async(device.config, url, creds))


# ── Pairing flow ─────────────────────────────────────────────────────────
# Pairing is interactive (the receiver shows a 4-digit PIN), so the
# orchestrator runs in three steps:
#   1. pair_begin_sync(device) starts the handshake; the device shows
#      its PIN.
#   2. The UI prompts the user for the PIN.
#   3. pair_finish_sync(handler, pin) completes the exchange and
#      returns the credentials blob to store.
# The "handler" object is opaque to callers — it carries the
# in-flight pairing state across the two calls.


class _PairingHandle:
    """Opaque carrier for an in-flight pairing handshake. Holds the
    pyatv handler and a single-shot event loop so begin / finish run
    on the same loop (pyatv's pairing state isn't loop-portable)."""

    def __init__(self, loop, handler):
        self.loop = loop
        self.handler = handler


def pair_begin_sync(device: AirPlay2Device) -> _PairingHandle:
    """Initiate pairing. Blocks until the receiver acknowledges the
    request (typically shows a PIN on-screen). Returns a handle the
    caller must pass back to ``pair_finish_sync``."""
    if not is_available():
        raise RuntimeError("pyatv is not installed")
    import asyncio

    from pyatv.const import Protocol

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    async def _begin():
        _dbg(f"pair_begin: pyatv.pair() for {device.name!r}")
        handler = await pyatv.pair(device.config, Protocol.AirPlay, loop)
        _dbg("pair_begin: handler.begin()")
        await handler.begin()
        _dbg("pair_begin: handler.begin() returned")
        return handler

    try:
        handler = loop.run_until_complete(_begin())
    except Exception as e:
        _dbg(f"pair_begin: raised {type(e).__name__}: {e}")
        loop.close()
        asyncio.set_event_loop(None)
        raise
    return _PairingHandle(loop, handler)


def pair_finish_sync(handle: _PairingHandle, pin: str) -> str:
    """Complete pairing with the PIN the user read from the receiver.
    Returns the credentials blob, which the caller should pass to
    ``store_credentials``. Releases the pairing handle's loop."""
    loop = handle.loop
    handler = handle.handler

    async def _finish():
        _dbg(f"pair_finish: submitting pin (len={len(pin)})")
        handler.pin(pin)
        await handler.finish()
        creds = handler.service.credentials
        _dbg(f"pair_finish: handler.finish() returned, creds_len={len(creds or '')}")
        return creds

    try:
        creds = loop.run_until_complete(_finish())
    except Exception as e:
        _dbg(f"pair_finish: raised {type(e).__name__}: {e}")
        raise
    finally:
        try:
            loop.run_until_complete(handler.close())
        except Exception:
            pass
        loop.close()
        import asyncio

        asyncio.set_event_loop(None)
    return creds or ""
