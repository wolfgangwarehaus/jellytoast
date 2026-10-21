"""Which music libraries the Qt Quick UI shows — the same selection (and
storage) as the desktop top bar's library picker, so both interfaces agree.

Fetches the server's libraries once signed in, exposes them to QML, and on a
toggle persists through ``library_selection`` and emits the shared
``PlayerBus.libraries_changed`` (every LibraryModel reloads on it)."""

from __future__ import annotations

import logging
from typing import Dict, List

from PySide6.QtCore import Property, QObject, Signal, Slot

from jellytoast import async_io
from jellytoast import library_selection as _ls
from jellytoast.player_state import PlayerBus

logger = logging.getLogger(__name__)


class Libraries(QObject):
    changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        PlayerBus.get().libraries_changed.connect(self.changed)

    @Slot()
    def refresh(self) -> None:
        """Ask the server for its music libraries (after sign-in / launch)."""
        from jellytoast.providers import get_provider

        prov = get_provider()

        def _listed(libs) -> None:
            prev = _ls.selected_ids()
            _ls.set_available_libraries(_ls.music_libraries(libs or []))
            self.changed.emit()
            # A stored library that no longer exists drops out of the
            # selection — reload so nothing is scoped to a ghost.
            if _ls.selected_ids() != prev:
                PlayerBus.get().libraries_changed.emit()

        async_io.run_async(
            prov.get_libraries,
            on_result=_listed,
            on_error=lambda e: logger.warning("couldn't list libraries: %s", e),
        )

    def _items(self) -> List[Dict]:
        sel = set(_ls.selected_ids())
        return [
            {
                "id": str(lib.get("Id") or ""),
                "name": str(lib.get("Name") or lib.get("Id") or ""),
                # Empty selection = all: every library shows checked.
                "checked": (not sel) or str(lib.get("Id") or "") in sel,
            }
            for lib in _ls.available_libraries()
        ]

    items = Property("QVariantList", _items, notify=changed)
    multiple = Property(bool, lambda self: _ls.has_multiple_libraries(), notify=changed)
    allSelected = Property(bool, lambda self: not _ls.is_filtered(), notify=changed)
    title = Property(str, lambda self: _ls.selection_title("Music"), notify=changed)

    def _apply(self, ids: List[str]) -> None:
        if _ls.set_selected_ids(ids):
            from jellytoast.settings import get_settings

            get_settings().flush()
            PlayerBus.get().libraries_changed.emit()
        else:
            self.changed.emit()

    @Slot(str)
    def toggle(self, lib_id: str) -> None:
        """Flip one library (same rules as the desktop picker)."""
        sel = list(_ls.selected_ids())
        if not sel:  # 'all' → start from every library
            sel = [str(lib.get("Id") or "") for lib in _ls.available_libraries()]
        if lib_id in sel:
            sel.remove(lib_id)
        else:
            sel.append(lib_id)
        if not sel:
            return  # never leave nothing selected
        self._apply(sel)

    @Slot()
    def selectAll(self) -> None:  # noqa: N802
        self._apply([])
