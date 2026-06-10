"""Tests for the right-click context-menu *construction* on the music
grids — which actions appear for each item kind.

The 2026-05-20 context-menu pass wired "Create smart playlist" and
track radio into the song / album / artist / genre menus. These tests
pin the action set per kind so a future menu edit can't silently drop
an entry.

The menus are built inline inside each view's ``contextMenuEvent`` /
``_on_context_menu``. ``QMenu.exec`` is stubbed to capture the action
labels and return ``None`` (nothing chosen), so no dialog or queue
side effect fires. ``indexAt`` is stubbed to a known row so the test
doesn't depend on the view being laid out / shown.
"""

from __future__ import annotations

import pytest
from PySide6.QtCore import QPoint
from PySide6.QtGui import QContextMenuEvent
from PySide6.QtWidgets import QMenu


@pytest.fixture
def captured_menu(monkeypatch):
    """Patch ``opaque_menu`` (the one menu factory every view uses) to
    return a QMenu subclass whose ``exec`` records the non-separator
    action labels and chooses nothing.

    Patching ``QMenu.exec`` directly doesn't work — PySide6 dispatches
    the C++ slot regardless, so the real modal menu opens and blocks.
    A Python subclass override *is* honoured for Python callers.
    """
    recorded: dict = {}

    class _RecordingMenu(QMenu):
        def exec(self, *args, **kwargs):
            recorded["labels"] = [a.text() for a in self.actions() if a.text()]
            return None

    def _fake_opaque_menu(parent=None):
        return _RecordingMenu(parent)

    # genres_view / library_grid lazy-import opaque_menu from ui_helpers;
    # songs_view binds it at module load — patch both names.
    monkeypatch.setattr("jellytoast.ui_helpers.opaque_menu", _fake_opaque_menu)
    monkeypatch.setattr(
        "jellytoast.songs_view.opaque_menu", _fake_opaque_menu, raising=False
    )
    return recorded


def _ctx_event() -> QContextMenuEvent:
    return QContextMenuEvent(
        QContextMenuEvent.Reason.Mouse, QPoint(5, 5), QPoint(5, 5)
    )


# ── Genre tiles ──────────────────────────────────────────────────────────


def test_genre_menu_offers_radio_and_smart_playlist(
    qapp, captured_menu, monkeypatch
):
    from jellytoast.genres_view import _GenreDelegate, _GenresListView, _GenresModel

    model = _GenresModel()
    model.set_items([{"Id": "g1", "Name": "Trip-Hop"}])
    view = _GenresListView(_GenreDelegate())
    view.setModel(model)
    monkeypatch.setattr(view, "indexAt", lambda _pos: model.index(0, 0))

    view.contextMenuEvent(_ctx_event())

    assert captured_menu["labels"] == [
        "Start genre radio",
        "Create smart playlist: Trip-Hop Discoveries",
    ]


# ── Album / artist / playlist tiles ──────────────────────────────────────


@pytest.fixture
def _no_downloads(monkeypatch):
    """``contextMenuEvent`` probes ``offline.is_downloaded`` — pin it to
    False so the menu always shows the *Download* (not *Remove*) label
    and never touches the real downloads DB."""
    from jellytoast import offline

    monkeypatch.setattr(offline, "is_downloaded", lambda _id: False)


def _make_library_view(kind: str):
    from jellytoast.library_grid import (
        _LibraryItemsModel,
        _LibraryListView,
        _RowDelegate,
        _TileDelegate,
    )

    model = _LibraryItemsModel()
    model.set_items([{"Id": "x1", "Name": "Homogenic"}])
    view = _LibraryListView(_TileDelegate(kind), _RowDelegate(kind))
    view.setModel(model)
    return view, model


def test_album_menu_offers_radio_smart_playlist_download(
    qapp, captured_menu, _no_downloads, monkeypatch
):
    view, model = _make_library_view("album")
    monkeypatch.setattr(view, "indexAt", lambda _pos: model.index(0, 0))

    view.contextMenuEvent(_ctx_event())

    assert captured_menu["labels"] == [
        "Start album radio",
        "Create smart playlist: More like Homogenic",
        "Download",
    ]


def test_artist_menu_offers_radio_smart_playlist_download(
    qapp, captured_menu, _no_downloads, monkeypatch
):
    view, model = _make_library_view("artist")
    monkeypatch.setattr(view, "indexAt", lambda _pos: model.index(0, 0))

    view.contextMenuEvent(_ctx_event())

    assert captured_menu["labels"] == [
        "Start artist radio",
        "Create smart playlist: Deep Cuts: Homogenic",
        "Download",
    ]


def test_playlist_menu_has_no_radio_or_smart_playlist(
    qapp, captured_menu, _no_downloads, monkeypatch
):
    """A playlist is already a curated set — no radio, no smart-playlist
    recipe; only the download entry."""
    view, model = _make_library_view("playlist")
    monkeypatch.setattr(view, "indexAt", lambda _pos: model.index(0, 0))

    view.contextMenuEvent(_ctx_event())

    assert captured_menu["labels"] == ["Download"]


# ── Song rows ────────────────────────────────────────────────────────────


def test_song_menu_offers_queue_radio_and_smart_playlist(
    qapp, captured_menu, monkeypatch
):
    from jellytoast.songs_view import SongsView

    sv = SongsView()
    sv._model.set_items(
        [{"Id": "s1", "Name": "Joga", "Artists": ["Bjork"]}]
    )
    monkeypatch.setattr(
        sv._view, "indexAt", lambda _pos: sv._model.index(0, 0)
    )

    sv._on_context_menu(QPoint(5, 5))

    # Track-seeded recipe: "More like {Track}" — uses track's Genres
    # + ProductionYear (when present) for the era-vibe seed.
    assert captured_menu["labels"] == [
        "Play next",
        "Add to queue",
        "Start radio from this song",
        "Create smart playlist: More like Joga",
    ]


def test_song_menu_without_name_drops_smart_playlist(
    qapp, captured_menu, monkeypatch
):
    """A track with no Name (and no Title fallback) gets queue + radio
    but no smart-playlist entry — the recipe needs a label to seed the
    suggested name."""
    from jellytoast.songs_view import SongsView

    sv = SongsView()
    sv._model.set_items([{"Id": "s2"}])
    monkeypatch.setattr(
        sv._view, "indexAt", lambda _pos: sv._model.index(0, 0)
    )

    sv._on_context_menu(QPoint(5, 5))

    assert captured_menu["labels"] == [
        "Play next",
        "Add to queue",
        "Start radio from this song",
    ]
