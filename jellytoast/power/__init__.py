"""Cross-platform "keep the machine awake while music plays" inhibitor.

A :class:`SleepInhibitor` watches :class:`PlayerBus` and asks the OS to
hold off *system* sleep while audio is actively playing, releasing the
hold on pause / stop / end. The screen is intentionally NOT kept awake —
audio playback doesn't need the display, and most users expect the
monitor to dim normally.

Backend (selected once per process, mirroring ``jellytoast.autostart`` /
``jellytoast.notifications``):
- Windows: ``SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)``
- Linux:   ``org.freedesktop.ScreenSaver`` Inhibit/UnInhibit (best-effort)
- other:   no-op
"""

from __future__ import annotations

import logging
from types import ModuleType

from PySide6.QtCore import QObject

logger = logging.getLogger(__name__)

_backend: ModuleType | None = None


def _select_backend() -> ModuleType:
    global _backend
    if _backend is not None:
        return _backend

    from jellytoast.platform_compat import IS_LINUX, IS_WINDOWS

    if IS_WINDOWS:
        from jellytoast.power import _windows as backend
    elif IS_LINUX:
        from jellytoast.power import _linux as backend
    else:
        from jellytoast.power import _unsupported as backend

    _backend = backend
    return _backend


def is_supported() -> bool:
    return _select_backend().is_supported()


class SleepInhibitor(QObject):
    """Holds off system sleep while audio plays.

    ``start()`` wires it to ``PlayerBus``; ``stop()`` releases any active
    hold. Idempotent — only true play↔not-play transitions reach the OS
    backend, and a backend failure never propagates (best-effort, like
    the media-controls / notifications facades).
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._inhibited = False
        self._started = False

    def start(self):
        if self._started:
            return
        self._started = True
        from jellytoast.player_state import PlayerBus

        bus = PlayerBus.get()
        bus.playback_started.connect(self._inhibit)
        bus.playback_resumed.connect(self._inhibit)
        bus.playback_paused.connect(self._release)
        bus.playback_stopped.connect(self._release)
        bus.playback_ended.connect(self._release)

    def _inhibit(self, *_):
        if self._inhibited:
            return
        try:
            if _select_backend().inhibit():
                self._inhibited = True
        except Exception as e:  # pragma: no cover — defensive
            logger.debug("sleep inhibit failed: %s", e)

    def _release(self, *_):
        if not self._inhibited:
            return
        try:
            _select_backend().release()
        except Exception as e:  # pragma: no cover — defensive
            logger.debug("sleep release failed: %s", e)
        finally:
            self._inhibited = False

    def stop(self):
        self._release()
