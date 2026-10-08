"""Touch input (Plasma Mobile phase 3), driven with real QTest touch events.

- Scroll areas flick-scroll by touch; a tap inside one still clicks, but a
  drag delivers NO press (the grids act on press — touching a tile to
  scroll used to open it).
- Long-press opens the same context menu a right-click does (exactly once,
  even with a menu-bearing parent), with no click after; with no menu it
  falls back to a tap; a pressed button isn't left stuck down.
- Mouse input is untouched.
- Library tiles: a tap on a hover-only corner opens the item (a finger
  can't reveal the play disc), while a mouse click there still plays.
"""

from __future__ import annotations

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QListWidget, QPushButton, QVBoxLayout, QWidget

from jellytoast import touch


@pytest.fixture(scope="module")
def device():
    return QTest.createTouchDevice()


@pytest.fixture
def app(qapp):
    touch.install(qapp)
    return qapp


def _settle(app, n=5):
    for _ in range(n):
        app.processEvents()


def _tap(app, device, widget, pos):
    QTest.touchEvent(widget, device).press(0, pos)
    _settle(app)
    QTest.touchEvent(widget, device).release(0, pos)
    _settle(app)


def _long_press(app, device, widget, pos):
    QTest.touchEvent(widget, device).press(0, pos)
    _settle(app)
    QTest.qWait(touch.LONG_PRESS_MS + 150)
    _settle(app)
    QTest.touchEvent(widget, device).release(0, pos)
    _settle(app)


@pytest.fixture
def listw(app):
    lw = QListWidget()
    for i in range(200):
        lw.addItem(f"item {i}")
    lw.resize(300, 400)
    lw.show()
    _settle(app)
    log = []
    original = lw.mousePressEvent

    def press(e):
        log.append("press")
        original(e)

    lw.mousePressEvent = press
    lw.clicked.connect(lambda i: log.append(("clicked", i.row())))
    yield lw, log
    lw.deleteLater()


def test_scroll_areas_get_kinetic_scrolling_on_show(app, listw):
    lw, _ = listw
    assert lw.viewport().property("jtKineticScroll")


def test_tap_in_a_list_clicks(app, device, listw):
    lw, log = listw
    _tap(app, device, lw.viewport(), QPoint(50, 30))
    assert log == ["press", ("clicked", 1)]
    assert touch.last_input_was_touch()


def test_drag_scrolls_without_any_press(app, device, listw):
    lw, log = listw
    vp = lw.viewport()
    QTest.touchEvent(vp, device).press(0, QPoint(50, 350))
    _settle(app)
    for y in range(340, 40, -20):
        QTest.qWait(10)
        QTest.touchEvent(vp, device).move(0, QPoint(50, y))
        _settle(app, 1)
    QTest.touchEvent(vp, device).release(0, QPoint(50, 50))
    for _ in range(30):
        QTest.qWait(10)
        _settle(app, 1)
    assert lw.verticalScrollBar().value() > 0
    assert log == []


def test_long_press_opens_one_menu_and_no_click(app, device):
    host = QWidget()
    lw = QListWidget()
    QVBoxLayout(host).addWidget(lw)
    for i in range(20):
        lw.addItem(str(i))
    log = []
    for w, name in ((lw, "list"), (host, "host")):
        w.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        w.customContextMenuRequested.connect(lambda _p, n=name: log.append(("menu", n)))
    lw.clicked.connect(lambda i: log.append("clicked"))
    host.resize(300, 400)
    host.show()
    _settle(app)
    try:
        _long_press(app, device, lw.viewport(), QPoint(40, 20))
        assert log == [("menu", "list")]
    finally:
        host.deleteLater()


def test_long_press_without_a_menu_is_a_tap(app, device, listw):
    lw, log = listw
    _long_press(app, device, lw.viewport(), QPoint(40, 20))
    assert ("clicked", 1) in log


def test_long_press_on_a_button_menu_resets_the_button(app, device):
    b = QPushButton("cast")
    b.resize(120, 40)
    b.show()
    _settle(app)
    log = []
    b.clicked.connect(lambda: log.append("clicked"))
    b.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
    b.customContextMenuRequested.connect(lambda _p: log.append("menu"))
    try:
        _long_press(app, device, b, QPoint(20, 20))
        assert log == ["menu"] and not b.isDown()
        _tap(app, device, b, QPoint(20, 20))
        assert log == ["menu", "clicked"]
    finally:
        b.deleteLater()


def test_mouse_is_unaffected(app, listw):
    lw, log = listw
    QTest.mouseMove(lw.viewport(), QPoint(50, 70))
    QTest.mouseClick(lw.viewport(), Qt.MouseButton.LeftButton, pos=QPoint(50, 60))
    _settle(app)
    assert log[0] == "press" and log[-1][0] == "clicked"
    assert not touch.last_input_was_touch()


class TestLibraryTile:
    @pytest.fixture
    def grid(self, app, monkeypatch):
        from jellytoast.library_grid import (
            _LibraryItemsModel,
            _LibraryListView,
            _RowDelegate,
            _TileDelegate,
        )

        model = _LibraryItemsModel()
        model.set_items([{"Id": "a1", "Name": "Homogenic"}])
        view = _LibraryListView(_TileDelegate("album"), _RowDelegate("album"))
        view.setModel(model)
        view.resize(600, 500)
        view.show()
        _settle(app)
        log = []
        view.play_requested.connect(lambda i: log.append(("play", i)))
        view.browse_requested.connect(lambda i: log.append(("browse", i)))
        cell = view.visualRect(model.index(0, 0))
        play_pos = view._tile_delegate.overlay_rect_for(cell).center()
        yield view, log, play_pos
        view.deleteLater()

    def test_touch_tap_on_the_play_corner_opens(self, app, device, grid):
        view, log, play_pos = grid
        _tap(app, device, view.viewport(), play_pos)
        assert log == [("browse", "a1")]

    def test_mouse_click_on_the_play_corner_still_plays(self, app, grid):
        view, log, play_pos = grid
        QTest.mouseMove(view.viewport(), play_pos)
        QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=play_pos)
        _settle(app)
        assert log == [("play", "a1")]
