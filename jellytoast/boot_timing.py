"""Boot-phase wall-clock instrumentation, opt-in via ``JT_BOOT_TIMING=1``.

Answers "where does startup time go on THIS machine" without a profiler:
``mark()`` calls sprinkled along the boot path log the delta since
process-ish start (this module's import, which app.py does first) and
since the previous mark. Designed for cross-machine comparison — run the
same build on Linux and Windows and diff the phase table.

Disabled (the default), each ``mark()`` is a single attribute check —
safe to leave the call sites in permanently.

Usage on any install:
    JT_BOOT_TIMING=1 jellytoast            (fish/bash)
    $env:JT_BOOT_TIMING="1"; jellytoast    (PowerShell)
"""

from __future__ import annotations

import logging
import os
import time

logger = logging.getLogger("jellytoast.boot")

_ENABLED = os.environ.get("JT_BOOT_TIMING") == "1"
_T0 = time.perf_counter()
_LAST = _T0


def mark(label: str) -> None:
    """Record a boot milestone. No-op unless JT_BOOT_TIMING=1."""
    global _LAST
    if not _ENABLED:
        return
    now = time.perf_counter()
    logger.info(
        "boot %8.1f ms  (+%7.1f ms)  %s",
        (now - _T0) * 1000.0,
        (now - _LAST) * 1000.0,
        label,
    )
    _LAST = now


_STALL_DUMP_INTERVAL_S = 4


def arm_stall_tracebacks() -> None:
    """While boot timing is on, dump every thread's stack to stderr each
    few seconds — a GUI-thread stall during boot then shows up as the
    exact frame it's stuck in (2026-06-12 Windows find: an 8s block
    between 'home surface routed' and the reveal timer firing).

    Best-effort: a fileno-less stderr (the GUI-subsystem exe runs
    sys.stderr=None) makes faulthandler raise — never fatal."""
    if not _ENABLED:
        return
    try:
        import faulthandler

        faulthandler.dump_traceback_later(_STALL_DUMP_INTERVAL_S, repeat=True)
    except Exception:
        pass


def disarm_stall_tracebacks() -> None:
    """Boot finished — stop the periodic dumps."""
    if not _ENABLED:
        return
    try:
        import faulthandler

        faulthandler.cancel_dump_traceback_later()
    except Exception:
        pass
