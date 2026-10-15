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


def test_flick_tuning_is_applied(app, listw):
    from PySide6.QtWidgets import QScroller, QScrollerProperties

    lw, _ = listw
    props = QScroller.scroller(lw.viewport()).scrollerProperties()
    m = QScrollerProperties.ScrollMetric
    assert props.scrollMetric(m.DecelerationFactor) == touch.FLICK_DECELERATION
    assert props.scrollMetric(m.MaximumVelocity) == touch.FLICK_MAX_VELOCITY


def test_a_hard_flick_settles_quickly(app):
    """Qt's defaults glided ~4 s (mostly a 1-px crawl). A hard flick should
    travel a few screens and settle well inside 2.5 s."""
    import time

    from PySide6.QtCore import QElapsedTimer, QPointF
    from PySide6.QtWidgets import QScrollArea, QScroller, QWidget

    area = QScrollArea()
    content = QWidget()
    content.setFixedSize(300, 100000)
    area.setWidget(content)
    area.resize(320, 600)
    touch.enable_kinetic_scrolling(area)
    scroller = QScroller.scroller(area.viewport())
    clock = QElapsedTimer()
    clock.start()
    y = 500.0
    scroller.handleInput(QScroller.Input.InputPress, QPointF(100, y), clock.elapsed())
    for _ in range(12):  # ~5000 px/s upward
        QTest.qWait(8)
        y -= 40
        scroller.handleInput(QScroller.Input.InputMove, QPointF(100, y), clock.elapsed())
    scroller.handleInput(QScroller.Input.InputRelease, QPointF(100, y), clock.elapsed())
    start = time.perf_counter()
    while scroller.state() == QScroller.State.Scrolling and time.perf_counter() - start < 6:
        QTest.qWait(10)
    glide = time.perf_counter() - start
    try:
        assert glide < 2.5
        assert area.verticalScrollBar().value() > 3 * area.viewport().height()
    finally:
        area.deleteLater()


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


# ── A–Z rail ────────────────────────────────────────────────────────────


@pytest.fixture
def rail(app):
    from jellytoast.library_grid import _AlphabetIndex

    host = QWidget()
    host.resize(400, 700)
    lay = QVBoxLayout(host)
    rail = _AlphabetIndex()
    lay.addWidget(rail)
    host.show()
    _settle(app)
    jumps = []
    rail.jump_requested.connect(jumps.append)
    yield rail, jumps
    host.deleteLater()


def _at(rail, ch):
    """Window coordinates of letter ``ch`` (touch goes in via the window,
    as a real finger's does)."""
    centre = rail._buttons[ch].geometry().center()
    return rail.mapTo(rail.window(), QPoint(rail.width() // 2, centre.y()))


def test_touching_the_rail_jumps_and_sliding_scrubs(app, device, rail):
    """A finger lands on a letter and slides: every new letter it crosses
    jumps live (a button tap needed press + release on one tiny letter)."""
    r, jumps = rail
    win = r.window().windowHandle()
    QTest.touchEvent(win, device).press(0, _at(r, "C"))
    _settle(app)
    assert jumps == ["C"]
    assert r._bubble is not None and r._bubble.isVisible()
    assert r._bubble.letter() == "C"
    for ch in "DEFG":
        QTest.touchEvent(win, device).move(0, _at(r, ch))
        _settle(app)
    assert jumps == ["C", "D", "E", "F", "G"]
    assert r._bubble.letter() == "G"
    QTest.touchEvent(win, device).release(0, _at(r, "G"))
    _settle(app)
    assert not r._bubble.isVisible()


def test_rail_clamps_past_its_ends(app, rail):
    r, _ = rail
    assert r.letter_at(-50) == "A"
    assert r.letter_at(r.height() + 50) == "Z"


def test_rail_mouse_click_still_jumps(app, rail):
    r, jumps = rail
    QTest.mouseClick(r._buttons["M"], Qt.MouseButton.LeftButton)
    assert jumps == ["M"]


def test_a_rail_jump_stops_a_running_flick(qapp, monkeypatch):
    from unittest.mock import MagicMock

    from PySide6.QtWidgets import QScroller

    from jellytoast import disk_cache
    from jellytoast import library_grid as lg

    monkeypatch.setattr(disk_cache, "load", lambda *a, **k: None)
    g = lg.LibraryGrid("album")
    g.api = MagicMock()
    g._letter_to_row = {"A": 0}
    g._model.set_items([{"Id": "1", "Name": "Adele"}])
    touch.enable_kinetic_scrolling(g._view)
    stopped = []
    scroller = QScroller.scroller(g._view.viewport())
    monkeypatch.setattr(scroller, "stop", lambda: stopped.append(True))
    monkeypatch.setattr(QScroller, "scroller", staticmethod(lambda _w: scroller))
    g._on_alphabet_jump("A")
    assert stopped == [True]
    g.deleteLater()


# ── Hover / tooltips after a tap ────────────────────────────────────────


def test_a_tap_leaves_no_hover_behind(app, device):
    """Qt parks its emulated pointer where the finger lifted, so the tapped
    button kept its hover fill until the next touch elsewhere."""
    host = QWidget()
    host.resize(300, 200)
    lay = QVBoxLayout(host)
    btn = QPushButton("tap me")
    lay.addWidget(btn)
    host.show()
    _settle(app)
    clicks = []
    btn.clicked.connect(lambda: clicks.append(1))
    at = btn.mapTo(host, btn.rect().center())
    win = host.windowHandle()
    QTest.touchEvent(win, device).press(0, at)
    _settle(app)
    QTest.touchEvent(win, device).release(0, at)
    _settle(app)
    try:
        assert clicks == [1]
        assert not btn.underMouse()
        assert not btn.hasFocus()  # no :focus ring left on it either
    finally:
        host.deleteLater()


def test_no_tooltip_after_a_tap(app, device, monkeypatch):
    from PySide6.QtCore import QEvent
    from PySide6.QtGui import QHelpEvent

    from jellytoast import custom_tooltip

    shown = []
    monkeypatch.setattr(
        custom_tooltip.ToolTipPopup, "instance", classmethod(lambda cls: _Rec(shown))
    )
    btn = QPushButton("x")
    btn.setToolTip("Mute")
    flt = custom_tooltip.ToolTipFilter()
    tip = QHelpEvent(QEvent.Type.ToolTip, QPoint(1, 1), QPoint(1, 1))
    monkeypatch.setattr(touch, "_last_input_touch", True)
    assert flt.eventFilter(btn, tip) is True
    assert shown == []
    monkeypatch.setattr(touch, "_last_input_touch", False)
    flt.eventFilter(btn, tip)
    assert shown == ["Mute"]
    btn.deleteLater()


class _Rec:
    def __init__(self, log):
        self.log = log

    def show_under(self, _w, text):
        self.log.append(text)


# ── Volume button by touch ──────────────────────────────────────────────


def test_tapping_volume_opens_the_slider_instead_of_muting(app, device):
    """Hover opens the slider and a click mutes — so on a touchscreen a tap
    muted and the slider was unreachable. A tap now toggles the slider."""
    from jellytoast.player_state import PlayerBus
    from jellytoast.volume_button import VolumeButton

    bus = PlayerBus()
    mutes = []
    bus.mute_toggled.connect(lambda: mutes.append(1))
    host = QWidget()
    host.resize(300, 300)
    lay = QVBoxLayout(host)
    lay.addStretch(1)
    vol = VolumeButton(bus)
    lay.addWidget(vol)
    host.show()
    _settle(app)
    win = host.windowHandle()
    at = vol.mapTo(host, vol.rect().center())
    try:
        _tap(app, device, win, at)
        assert mutes == []
        assert vol._popup is not None and vol._popup.isVisible()
        _tap(app, device, win, at)
        assert not vol._popup.isVisible()
        assert mutes == []
    finally:
        if vol._popup is not None:
            vol._popup.hide()
        host.deleteLater()


def test_mouse_click_on_volume_still_mutes(app):
    from jellytoast.player_state import PlayerBus
    from jellytoast.volume_button import VolumeButton

    bus = PlayerBus()
    mutes = []
    bus.mute_toggled.connect(lambda: mutes.append(1))
    vol = VolumeButton(bus)
    vol.show()
    _settle(app)
    QTest.mouseClick(vol, Qt.MouseButton.LeftButton)
    assert mutes == [1]
    if vol._popup is not None:
        vol._popup.hide()
    vol.deleteLater()


# ── Swipes ──────────────────────────────────────────────────────────────


def _swipe(app, device, win, start, dx, dy, steps=8):
    QTest.touchEvent(win, device).press(0, start)
    _settle(app)
    p = QPoint(start)
    for i in range(1, steps + 1):
        QTest.qWait(8)
        p = QPoint(start.x() + dx * i // steps, start.y() + dy * i // steps)
        QTest.touchEvent(win, device).move(0, p)
        _settle(app, 2)
    QTest.touchEvent(win, device).release(0, p)
    _settle(app)


@pytest.fixture
def swipe_host(app):
    host = QWidget()
    host.resize(400, 400)
    lay = QVBoxLayout(host)
    btn = QPushButton("big")
    btn.setMinimumHeight(200)
    lay.addWidget(btn)
    host.show()
    _settle(app)
    got, clicks = [], []
    btn.clicked.connect(lambda: clicks.append(1))
    yield host, btn, got, clicks
    host.deleteLater()


def test_a_swipe_fires_once_and_clicks_nothing(app, device, swipe_host):
    host, btn, got, clicks = swipe_host
    touch.on_swipe(host, got.append, ("left", "up"))
    start = btn.mapTo(host, btn.rect().center())
    _swipe(app, device, host.windowHandle(), start, -150, 0)
    _settle(app)
    assert got == ["left"]
    assert clicks == []


def test_wrong_direction_or_gate_off_is_no_swipe(app, device, swipe_host):
    host, btn, got, _ = swipe_host
    gate = {"on": False}
    touch.on_swipe(host, got.append, ("up",), when=lambda: gate["on"])
    start = btn.mapTo(host, btn.rect().center())
    _swipe(app, device, host.windowHandle(), start, 0, -120)  # gated off
    _swipe(app, device, host.windowHandle(), start, -150, 0)  # not "up"
    _settle(app)
    assert got == []


def test_a_scrollable_list_keeps_its_drag(app, device, listw):
    """A vertical drag on a list that scrolls is a scroll, never a swipe."""
    lw, _ = listw
    got = []
    touch.on_swipe(lw.window(), got.append, ("up", "down"))
    start = lw.viewport().mapTo(lw.window(), QPoint(50, 300))
    _swipe(app, device, lw.window().windowHandle(), start, 0, -200)
    _settle(app)
    assert got == []
    assert lw.verticalScrollBar().value() > 0


def test_a_slider_keeps_its_drag(app, device):
    from PySide6.QtWidgets import QSlider

    host = QWidget()
    host.resize(400, 200)
    lay = QVBoxLayout(host)
    s = QSlider(Qt.Orientation.Horizontal)
    lay.addWidget(s)
    host.show()
    _settle(app)
    got = []
    touch.on_swipe(host, got.append, ("left", "right"))
    start = s.mapTo(host, s.rect().center())
    _swipe(app, device, host.windowHandle(), start, 150, 0)
    _settle(app)
    try:
        assert got == []
    finally:
        host.deleteLater()


class _FakeTab:
    def __init__(self, checked):
        self.checked = checked

    def isChecked(self):
        return self.checked

    def setChecked(self, v):
        self.checked = v


@pytest.mark.parametrize(
    "on_playing,direction,expect",
    [
        (True, "left", "tracks"),
        (False, "right", "playing"),
        (True, "down", "dismiss"),
        (False, "down", None),  # the Tracks list owns vertical drags
        (True, "right", None),
    ],
)
def test_now_playing_compact_swipes(on_playing, direction, expect):
    from types import SimpleNamespace

    from jellytoast.now_playing_page import NowPlayingPage

    playing, tracks = _FakeTab(on_playing), _FakeTab(not on_playing)
    dismissed = []
    page = SimpleNamespace(
        _tab_playing=playing,
        _tab_tracks=tracks,
        dismiss_requested=SimpleNamespace(emit=lambda: dismissed.append(1)),
    )
    NowPlayingPage._on_compact_swipe(page, direction)
    result = (
        "dismiss"
        if dismissed
        else "tracks"
        if tracks.checked and on_playing
        else "playing"
        if playing.checked and not on_playing
        else None
    )
    assert result == expect


def test_a_swipe_starting_on_a_label_reaches_the_page(app, device):
    """A label ignores the press, which bubbles up through every parent; the
    filter used to record the top-level window as the target, above the
    page's swipe handler, so swipes over cover art / text did nothing."""
    from PySide6.QtWidgets import QLabel

    host = QWidget()
    host.resize(400, 400)
    outer = QVBoxLayout(host)
    page = QWidget()
    outer.addWidget(page)
    inner = QVBoxLayout(page)
    label = QLabel("cover")
    label.setMinimumHeight(200)
    inner.addWidget(label)
    host.show()
    _settle(app)
    got = []
    touch.on_swipe(page, got.append, ("left",))
    start = label.mapTo(host, label.rect().center())
    _swipe(app, device, host.windowHandle(), start, -160, 0)
    _settle(app)
    try:
        assert got == ["left"]
    finally:
        host.deleteLater()


def test_volume_slider_closes_on_a_tap_elsewhere(app, device):
    from jellytoast.player_state import PlayerBus
    from jellytoast.volume_button import VolumeButton

    host = QWidget()
    host.resize(300, 300)
    lay = QVBoxLayout(host)
    other = QPushButton("elsewhere")
    lay.addWidget(other)
    lay.addStretch(1)
    vol = VolumeButton(PlayerBus())
    lay.addWidget(vol)
    host.show()
    _settle(app)
    win = host.windowHandle()
    try:
        _tap(app, device, win, vol.mapTo(host, vol.rect().center()))
        assert vol._popup.isVisible()
        _tap(app, device, win, other.mapTo(host, other.rect().center()))
        assert not vol._popup.isVisible()
    finally:
        if vol._popup is not None:
            vol._popup.hide()
        host.deleteLater()


def test_no_button_focus_ring_after_touch(app, monkeypatch):
    """A page hiding hands focus to the next button (OtherFocusReason); with
    the last input a finger, that button must not keep focus (its :focus
    ring would paint with no keyboard in sight)."""
    from jellytoast import keyboard_focus

    host = QWidget()
    lay = QVBoxLayout(host)
    btn = QPushButton("Albums")
    lay.addWidget(btn)
    host.show()
    _settle(app)
    monkeypatch.setattr(touch, "_last_input_touch", True)
    keyboard_focus.note_pointer_input()
    btn.setFocus(Qt.FocusReason.OtherFocusReason)
    _settle(app)
    try:
        assert not btn.hasFocus()
        keyboard_focus.note_keyboard_input()  # a real key press: keep it
        btn.setFocus(Qt.FocusReason.TabFocusReason)
        _settle(app)
        assert btn.hasFocus()
    finally:
        keyboard_focus.note_pointer_input()
        host.deleteLater()
