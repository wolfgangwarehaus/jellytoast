"""Add to playlist (#238): one submenu every song / album menu can carry.

``add_playlist_submenu(menu, resolve_ids, host=…)`` appends an
**Add to playlist ▸** entry listing the user's server playlists plus
**New playlist…**. The entries act on their own (``triggered``), so the
calling menu's ``chosen = menu.exec(...)`` dispatch needs no new branch.

``resolve_ids`` runs on a worker thread and returns the track ids to add —
a plain list for songs, or a fetch (an album's tracks) that would block the
GUI thread. Results land as a toast; ``PlayerBus.playlists_changed`` lets
the Playlists grid refresh.

Strings are literal ``QCoreApplication.translate("PlaylistActions", …)``
calls: lupdate only extracts literals, so a ``_t()`` shorthand would leave
them untranslatable.
"""

from __future__ import annotations

import logging
import time
from typing import Callable, Dict, List, Optional

from PySide6.QtCore import QCoreApplication, QTimer
from PySide6.QtWidgets import QMenu, QWidget

logger = logging.getLogger(__name__)

_CACHE_TTL_S = 120.0
# The playlists menu shows instantly from this; refreshed in the background
# when stale, dropped after any add / create. Keyed by server so a sign-in to
# another server never lists the old one's playlists.
_cache: Dict[str, object] = {"server": "", "at": 0.0, "items": None}


def _provider():
    from jellytoast.providers import get_provider

    return get_provider()


def available() -> bool:
    """Whether "Add to playlist" can work right now."""
    from jellytoast import offline

    try:
        prov = _provider()
    except Exception:
        return False
    try:
        # A property on the real providers (MediaProvider.is_authenticated);
        # tolerate a callable too.
        auth = prov.is_authenticated
        signed_in = bool(auth() if callable(auth) else auth)
    except Exception:
        signed_in = False
    return (
        signed_in
        and bool(getattr(prov, "supports_playlist_editing", False))
        and not offline.is_offline_mode()
    )


def invalidate_cache() -> None:
    _cache.update(at=0.0, items=None)


def _cached_playlists() -> Optional[List[dict]]:
    prov = _provider()
    fresh = time.monotonic() - float(_cache["at"]) < _CACHE_TTL_S
    if _cache["items"] is not None and _cache["server"] == prov.server_url and fresh:
        return list(_cache["items"])  # type: ignore[arg-type]
    return None


def album_track_ids(album_id: str) -> Callable[[], List[str]]:
    """A resolver for every track of an album (runs on the worker)."""
    return lambda: [t.get("Id", "") for t in _provider().get_album_tracks(album_id) if t.get("Id")]


def playlist_track_ids(playlist_id: str) -> Callable[[], List[str]]:
    return lambda: [
        t.get("Id", "") for t in _provider().get_playlist_items(playlist_id) if t.get("Id")
    ]


def add_playlist_submenu(
    menu: QMenu, resolve_ids: Callable[[], List[str]], *, host: QWidget
) -> Optional[QMenu]:
    """Append the "Add to playlist" submenu to ``menu`` (None when the server
    can't edit playlists or the app is offline)."""
    if not available():
        return None
    sub = menu.addMenu(QCoreApplication.translate("PlaylistActions", "Add to playlist"))

    def _fill(items: Optional[List[dict]]) -> None:
        try:
            sub.clear()
        except RuntimeError:  # the menu closed and was deleted meanwhile
            return
        new = sub.addAction(QCoreApplication.translate("PlaylistActions", "New playlist…"))
        new.triggered.connect(
            lambda: QTimer.singleShot(0, lambda: _new_playlist(host, resolve_ids))
        )
        if items is None:
            sub.addSeparator()
            loading = sub.addAction(QCoreApplication.translate("PlaylistActions", "Loading…"))
            loading.setEnabled(False)
            return
        if items:
            sub.addSeparator()
        for p in items:
            pid, name = p.get("Id", ""), p.get("Name") or ""
            act = sub.addAction(name)
            act.triggered.connect(
                lambda _c=False, pid=pid, name=name: _add(host, pid, name, resolve_ids)
            )

    def _on_show() -> None:
        cached = _cached_playlists()
        _fill(cached)
        if cached is not None:
            return
        from jellytoast.async_io import run_async

        prov = _provider()

        def _loaded(items):
            _cache.update(server=prov.server_url, at=time.monotonic(), items=list(items or []))
            _fill(list(items or []))

        run_async(prov.get_playlists, on_result=_loaded, on_error=lambda _e: _fill([]))

    sub.aboutToShow.connect(_on_show)
    return sub


def _toast(host: QWidget, message: str) -> None:
    from jellytoast.toast import show_toast

    try:
        show_toast(host.window(), message)
    except RuntimeError:  # host went away while the request ran
        pass


def _changed() -> None:
    invalidate_cache()
    from jellytoast.player_state import PlayerBus

    PlayerBus.get().playlists_changed.emit()


def _added_text(n: int, name: str) -> str:
    if n == 1:
        return QCoreApplication.translate("PlaylistActions", "Added to {0}").format(name)
    return QCoreApplication.translate("PlaylistActions", "Added {0} songs to {1}").format(n, name)


def _add(host: QWidget, playlist_id: str, name: str, resolve_ids) -> None:
    from jellytoast.async_io import run_async

    prov = _provider()

    def _work() -> int:
        ids = [i for i in resolve_ids() if i]
        if ids:
            prov.add_to_playlist(playlist_id, ids)
        return len(ids)

    def _done(n: int) -> None:
        if n:
            _changed()
            _toast(host, _added_text(n, name))
        else:
            _toast(host, QCoreApplication.translate("PlaylistActions", "Nothing to add"))

    def _failed(e) -> None:
        logger.warning("add to playlist %s failed: %s", playlist_id, e)
        _toast(
            host, QCoreApplication.translate("PlaylistActions", "Couldn't add to {0}").format(name)
        )

    run_async(_work, on_result=_done, on_error=_failed)


def _new_playlist(host: QWidget, resolve_ids) -> None:
    from jellytoast.frosted_dialog import frosted_text_input

    name = frosted_text_input(
        host,
        QCoreApplication.translate("PlaylistActions", "New playlist"),
        QCoreApplication.translate("PlaylistActions", "Name"),
        placeholder=QCoreApplication.translate("PlaylistActions", "My playlist"),
        confirm_text=QCoreApplication.translate("PlaylistActions", "Create"),
    )
    if not name:
        return
    from jellytoast.async_io import run_async

    prov = _provider()

    def _work() -> int:
        ids = [i for i in resolve_ids() if i]
        prov.create_playlist(name, ids)
        return len(ids)

    def _done(n: int) -> None:
        _changed()
        _toast(
            host,
            QCoreApplication.translate("PlaylistActions", "Created {0}").format(name)
            if n == 0
            else _added_text(n, name),
        )

    def _failed(e) -> None:
        logger.warning("create playlist %r failed: %s", name, e)
        _toast(
            host, QCoreApplication.translate("PlaylistActions", "Couldn't create {0}").format(name)
        )

    run_async(_work, on_result=_done, on_error=_failed)
