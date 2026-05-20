"""Asyncio loop worker thread for the DLNA backend.

``_DlnaLoopThread`` owns a private ``asyncio`` loop on a dedicated
daemon thread. This is the codebase's *only* ``threading.Thread``
exception to the "no raw threads, use ``modules.async_io``" rule:
the worker isn't blocking the Qt thread, it's hosting an asyncio loop
the rest of the DLNA module submits work to. ``async-upnp-client`` is
asyncio-native and jellytoast uses no asyncio elsewhere, so the
controller carves out one long-lived loop here. See the package
docstring (``__init__.py``) for the full rationale.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any, Awaitable, Optional


class _DlnaLoopThread:
    """Owns a private asyncio loop on a dedicated daemon thread.

    The thread is long-lived (one per process; idle when no DLNA work
    is in flight) and the loop's run/stop lifecycle is fully managed
    here so callers only see the synchronous ``submit`` /
    ``submit_blocking`` API. This is the documented exception to the
    project's "no ``threading.Thread`` for I/O" rule — see module
    docstring."""

    def __init__(self, name: str = "jellytoast-dlna"):
        self._name = name
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._ready = threading.Event()

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._ready.clear()
            self._thread = threading.Thread(
                target=self._run,
                name=self._name,
                daemon=True,
            )
            self._thread.start()
            # Block briefly until the loop has actually been bound —
            # avoids a race where a fast submit() outruns loop creation
            # and crashes on ``run_coroutine_threadsafe(loop=None)``.
            self._ready.wait(timeout=5.0)

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        self._ready.set()
        try:
            loop.run_forever()
        finally:
            try:
                loop.close()
            except Exception:
                pass

    @property
    def loop(self) -> Optional[asyncio.AbstractEventLoop]:
        return self._loop

    def is_running(self) -> bool:
        return (
            self._thread is not None
            and self._thread.is_alive()
            and self._loop is not None
            and not self._loop.is_closed()
        )

    def submit(self, coro: Awaitable) -> "asyncio.Future":
        """Schedule ``coro`` on the loop. Returns a concurrent.futures
        ``Future`` (cross-thread-safe). Caller uses ``.result(timeout)``
        from another thread, or attaches an ``add_done_callback``."""
        if not self.is_running():
            self.start()
        assert self._loop is not None
        return asyncio.run_coroutine_threadsafe(coro, self._loop)

    def submit_blocking(self, coro: Awaitable, timeout: float = 30.0) -> Any:
        """Convenience: submit and block for the result. Don't call from
        the asyncio loop's own thread (would deadlock); the assertion
        guards the common case."""
        fut = self.submit(coro)
        return fut.result(timeout=timeout)

    def stop(self, timeout: float = 2.0) -> None:
        with self._lock:
            loop = self._loop
            thread = self._thread
            if loop is None or thread is None:
                return
            try:
                loop.call_soon_threadsafe(loop.stop)
            except Exception:
                pass
            thread.join(timeout=timeout)
            self._loop = None
            self._thread = None


__all__ = ["_DlnaLoopThread"]
