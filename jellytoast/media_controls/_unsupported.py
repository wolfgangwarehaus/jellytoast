"""No-op media-controls backend for platforms without an OS-level media
hub wired up yet. Mirrors the public surface (start / stop) so callers
never have to platform-branch.

When Windows SMTC and macOS NowPlaying backends land, they'll replace
this stub on those platforms; nothing in jellytoast/app.py needs to change.
"""

from __future__ import annotations

from PySide6.QtCore import QObject


class UnsupportedMediaControlsService(QObject):
    """No-op stand-in. Same shape as the MPRIS service so call sites
    don't need to know which backend is active."""

    def __init__(self, parent=None):
        super().__init__(parent)

    def start(self):
        # Nothing to register. We deliberately don't print here — the
        # facade's job is to be invisible on platforms where media keys
        # aren't yet integrated.
        pass

    def stop(self):
        pass
