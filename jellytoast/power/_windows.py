"""Windows keep-awake backend.

Holds off *system* sleep during playback via ``SetThreadExecutionState``.
``ES_SYSTEM_REQUIRED`` resets the system idle timer; ``ES_CONTINUOUS``
makes the request persist until explicitly cleared. We deliberately do
NOT set ``ES_DISPLAY_REQUIRED`` — this is audio, so the screen is free to
sleep. State is per-thread, so inhibit/release must run on the same
(GUI) thread; the SleepInhibitor controller guarantees that.
"""

from __future__ import annotations

import ctypes
import logging

logger = logging.getLogger(__name__)

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001

try:  # ``windll`` only exists on Windows; importable (no-op) elsewhere.
    _kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
except (AttributeError, OSError):  # pragma: no cover — non-Windows
    _kernel32 = None


def is_supported() -> bool:
    return _kernel32 is not None


def inhibit() -> bool:
    """Request that the system stay awake. Returns True on success."""
    if _kernel32 is None:
        return False
    try:
        # Returns the previous state (non-zero) on success, 0 on failure.
        rc = _kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
        return rc != 0
    except Exception as e:  # pragma: no cover — defensive
        logger.debug("SetThreadExecutionState inhibit failed: %s", e)
        return False


def release() -> None:
    """Drop the keep-awake request (ES_CONTINUOUS alone clears the flags
    set above while leaving any unrelated state untouched)."""
    if _kernel32 is None:
        return
    try:
        _kernel32.SetThreadExecutionState(ES_CONTINUOUS)
    except Exception as e:  # pragma: no cover — defensive
        logger.debug("SetThreadExecutionState release failed: %s", e)
