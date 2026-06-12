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
