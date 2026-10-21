"""Drive and inspect the Qt Quick front end — the dev / test rig.

One ``Rig`` wraps a loaded ``QQmlApplicationEngine`` and offers what a
person does with the phone (tap, long-press, drag, swipe, flick, keys) plus
what a developer needs to see (item tree, state snapshot, screenshots,
waiting on a condition). Input is synthesized in-process with QTest, so it
goes through real QML hit-testing, handlers and flick physics, on any
platform plugin (offscreen included).

Used three ways:
- live, over the test bridge (``JT_TEST_BRIDGE=1``; ``dev/qctl.py``),
- from pytest (``tests/quick_rig.py`` builds the app against a fake library),
- from scratch scripts.

Targets are an item, an ``objectName``, ``"text:<visible text>"``, or an
``(x, y)`` window point.
"""

from __future__ import annotations

import time
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

from PySide6.QtCore import QCoreApplication, QObject, QPoint, QPointF, Qt
from PySide6.QtQuick import QQuickItem, QQuickWindow
from PySide6.QtTest import QTest

Target = Union[QQuickItem, str, Tuple[float, float]]

_KEYS = {
    "back": Qt.Key.Key_Back,
    "esc": Qt.Key.Key_Escape,
    "escape": Qt.Key.Key_Escape,
    "enter": Qt.Key.Key_Return,
    "return": Qt.Key.Key_Return,
    "space": Qt.Key.Key_Space,
    "up": Qt.Key.Key_Up,
    "down": Qt.Key.Key_Down,
    "left": Qt.Key.Key_Left,
    "right": Qt.Key.Key_Right,
    "tab": Qt.Key.Key_Tab,
    "backspace": Qt.Key.Key_Backspace,
}


def pump(ms: int = 0) -> None:
    """Run the event loop for ``ms`` (animations, async results, images)."""
    if ms <= 0:
        QCoreApplication.processEvents()
        return
    QTest.qWait(ms)


class RigError(RuntimeError):
    pass


class Rig:
    def __init__(self, engine):
        self.engine = engine
        self._window: Optional[QQuickWindow] = None

    # ── lookup ────────────────────────────────────────────────────────────
    @property
    def window(self) -> QQuickWindow:
        # Hold ONE wrapper: PySide parents the contentItem() wrapper to the
        # window wrapper it came from, and invalidates it ("already
        # deleted") when that window wrapper is garbage-collected.
        if self._window is None:
            roots = self.engine.rootObjects()
            if not roots:
                raise RigError("QML failed to load (no root object)")
            self._window = roots[0]
        return self._window

    def items(self) -> List[QQuickItem]:
        """Every QQuickItem in the window, depth-first."""
        out: List[QQuickItem] = []

        def walk(it: QQuickItem) -> None:
            try:
                kids = it.childItems()
            except RuntimeError:  # deleted under us (delegate recycled)
                return
            out.append(it)
            for c in kids:
                walk(c)

        walk(self.window.contentItem())
        return out

    @staticmethod
    def _shown(it: QQuickItem) -> bool:
        while it is not None:
            if not it.isVisible() or it.opacity() <= 0.01:
                return False
            it = it.parentItem()
        return True

    def _on_screen(self, it: QQuickItem) -> bool:
        if not self._shown(it) or it.width() <= 0 or it.height() <= 0:
            return False
        c = it.mapToScene(QPointF(it.width() / 2, it.height() / 2))
        w = self.window
        return 0 <= c.x() < w.width() and 0 <= c.y() < w.height()

    def find(self, target: str, *, visible: bool = True) -> Optional[QQuickItem]:
        """By objectName, or ``text:<substring>`` of a visible text property.
        With several matches the on-screen one wins (stacked pages keep old
        ones alive), then the last (top-most) in tree order."""
        if target.startswith("text:"):
            want = target[5:].lower()

            def match(it):
                t = it.property("text")
                return isinstance(t, str) and want in t.lower()
        else:

            def match(it):
                return it.objectName() == target

        hits = [it for it in self.items() if match(it)]
        if not hits and not target.startswith("text:"):
            # Popups (Menu, Dialog) aren't items — find the open one by name
            # and stand in its content item.
            for obj in self.window.findChildren(QObject, target):
                if obj.inherits("QQuickPopup") and obj.property("opened"):
                    content = obj.property("contentItem")
                    if content is not None:
                        hits.append(content)
        if visible:
            shown = [it for it in hits if self._on_screen(it)]
            hits = shown or [it for it in hits if self._shown(it)]
        return hits[-1] if hits else None

    def get(self, target: Target) -> QQuickItem:
        if isinstance(target, QQuickItem):
            return target
        it = self.find(target) if isinstance(target, str) else None
        if it is None:
            raise RigError(f"no visible item {target!r}")
        return it

    def point(self, target: Target) -> QPoint:
        if isinstance(target, (tuple, list)):
            return QPoint(int(target[0]), int(target[1]))
        it = self.get(target)
        c = it.mapToScene(QPointF(it.width() / 2, it.height() / 2))
        return QPoint(int(c.x()), int(c.y()))

    def prop(self, target: Target, name: str) -> Any:
        return self.get(target).property(name)

    # ── input ─────────────────────────────────────────────────────────────
    def _settle(self, timeout: float = 3.0) -> None:
        """Wait out page transitions — StackView drops input while busy, so a
        tap mid-slide would silently do nothing (a person waits too)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            busy = False
            for it in self.items():
                try:
                    if it.inherits("QQuickStackView") and it.property("busy"):
                        busy = True
                        break
                except RuntimeError:
                    continue
            if not busy:
                return
            pump(30)

    def tap(self, target: Target, settle: int = 120) -> None:
        self._settle()
        p = self.point(target)
        QTest.mouseClick(self.window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, p)
        pump(settle)

    def long_press(self, target: Target, hold: int = 1000, settle: int = 150) -> None:
        self._settle()
        p = self.point(target)
        w = self.window
        QTest.mousePress(w, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, p)
        pump(hold)
        QTest.mouseRelease(w, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, p)
        pump(settle)

    def drag(
        self,
        start: Target,
        end: Union[Target, None] = None,
        *,
        dx: float = 0,
        dy: float = 0,
        ms: int = 300,
        steps: int = 15,
        hold_end: int = 0,
        settle: int = 400,
    ) -> None:
        """Press at ``start``, move to ``end`` (or by dx/dy) over ``ms``,
        release. ``hold_end`` pauses before release (kills flick velocity);
        a short ``ms`` makes it a flick."""
        self._settle()
        a = self.point(start)
        b = self.point(end) if end is not None else QPoint(int(a.x() + dx), int(a.y() + dy))
        w = self.window
        QTest.mousePress(w, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, a)
        for i in range(1, steps + 1):
            f = i / steps
            QTest.mouseMove(
                w, QPoint(int(a.x() + (b.x() - a.x()) * f), int(a.y() + (b.y() - a.y()) * f))
            )
            pump(max(1, ms // steps))
        if hold_end:
            pump(hold_end)
        QTest.mouseRelease(w, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, b)
        pump(settle)

    def swipe(
        self, direction: str, *, on: Optional[Target] = None, frac: float = 0.4, ms: int = 220
    ) -> None:
        """Swipe ``up`` / ``down`` / ``left`` / ``right`` — across ``on`` or
        the middle of the window. Content moves with the finger: ``up``
        scrolls a list down."""
        w = self.window
        if on is not None:
            it = self.get(on)
            c = self.point(it)
            span_x, span_y = it.width() * frac, it.height() * frac
        else:
            c = QPoint(int(w.width() / 2), int(w.height() / 2))
            span_x, span_y = w.width() * frac, w.height() * frac
        d = {
            "up": (0, -span_y),
            "down": (0, span_y),
            "left": (-span_x, 0),
            "right": (span_x, 0),
        }[direction]
        start = (c.x() - d[0] / 2, c.y() - d[1] / 2)
        self.drag(start, dx=d[0], dy=d[1], ms=ms)

    def edge_swipe(self, *, ms: int = 250) -> None:
        """Back gesture: drag in from the window's left edge."""
        w = self.window
        self.drag((2, w.height() / 2), dx=w.width() * 0.5, ms=ms)

    def scroll_to_end(self, target: Target, *, timeout: float = 20.0) -> None:
        """Flick a list / grid down until it stops loading more."""
        it = self.get(target)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if it.property("atYEnd") and not self._busy():
                return
            self.swipe("up", on=it, frac=0.6, ms=120)
            pump(300)
        raise RigError("scroll_to_end timed out")

    def _busy(self) -> bool:
        """The page on screen is still loading (pages expose ``loading``)."""
        stack = self.find("stack", visible=False)
        page = stack.property("currentItem") if stack is not None else None
        if page is not None and page.property("loading"):
            return True
        albums = self.engine.rootContext().contextProperty("albums")
        return bool(albums is not None and albums.property("loading"))

    def key(self, name: str, settle: int = 150) -> None:
        k = _KEYS.get(name.lower())
        w = self.window
        if k is None and len(name) == 1:
            self.type(name, settle)
            return
        if k is None:
            raise RigError(f"unknown key {name!r}; one of {sorted(_KEYS)}")
        else:
            QTest.keyClick(w, k)
        pump(settle)

    def type(self, text: str, settle: int = 150) -> None:
        """Type into the focused field (QTest.keyClicks is Widgets-only in
        PySide, so post the key events to the window ourselves)."""
        from PySide6.QtCore import QEvent
        from PySide6.QtGui import QKeyEvent

        w = self.window
        for ch in text:
            code = ord(ch.upper()) if ch.isascii() and ch.isalnum() else 0
            for typ in (QEvent.Type.KeyPress, QEvent.Type.KeyRelease):
                QCoreApplication.sendEvent(
                    w, QKeyEvent(typ, code, Qt.KeyboardModifier.NoModifier, ch)
                )
        pump(settle)

    # ── waiting ───────────────────────────────────────────────────────────
    def wait_until(self, cond: Callable[[], Any], timeout: float = 10.0, step: int = 50) -> Any:
        deadline = time.monotonic() + timeout
        while True:
            try:
                v = cond()
            except RigError:
                v = None
            if v:
                return v
            if time.monotonic() > deadline:
                raise RigError("wait_until timed out")
            pump(step)

    def wait_for(self, target: str, timeout: float = 10.0) -> QQuickItem:
        return self.wait_until(lambda: self.find(target), timeout)

    def wait_idle(self, timeout: float = 10.0) -> None:
        """Until no model is loading and the page stack has stopped moving."""

        def idle():
            stack = self.find("stack", visible=False)
            busy = stack is not None and stack.property("busy")
            return not busy and not self._busy()

        self.wait_until(idle, timeout)
        pump(100)

    # ── seeing ────────────────────────────────────────────────────────────
    def shot(self, path: str) -> str:
        """Save what the window shows now. Offscreen, real covers and rounded
        corners need the GPU path (``QSG_RHI_BACKEND=opengl
        QT_QUICK_BACKEND=rhi``) at scale 1; dev/quick_rig.sh sets that."""
        pump(50)
        img = self.window.grabWindow()
        if not img.save(path):
            raise RigError(f"could not save {path}")
        return path

    def tree(self, *, all_items: bool = False, depth: int = 99) -> str:
        """Readable outline of the on-screen items that matter: named ones
        and ones showing text (or every item with ``all_items``)."""
        lines: List[str] = []

        win_w, win_h = self.window.width(), self.window.height()

        def walk(it: QQuickItem, d: int, indent: int) -> None:
            if d > depth or not self._shown(it):
                return
            if not all_items:
                tl = it.mapToScene(QPointF(0, 0))
                if (
                    tl.x() >= win_w
                    or tl.y() >= win_h
                    or tl.x() + it.width() <= 0
                    or tl.y() + it.height() <= 0
                ):
                    return  # off screen (list cache buffer, parked pages)
            name = it.objectName()
            text = it.property("text")
            text = text if isinstance(text, str) else ""
            interesting = all_items or name or text
            if interesting:
                p = it.mapToScene(QPointF(0, 0))
                cls = it.metaObject().className().split("_QML")[0].replace("QQuick", "")
                bits = [cls]
                if name:
                    bits.append(f"#{name}")
                if text:
                    bits.append(repr(text[:60]))
                bits.append(f"@{int(p.x())},{int(p.y())} {int(it.width())}x{int(it.height())}")
                lines.append("  " * indent + " ".join(bits))
            for c in it.childItems():
                walk(c, d + 1, indent + (1 if interesting else 0))

        walk(self.window.contentItem(), 0, 0)
        return "\n".join(lines)

    def state(self) -> Dict[str, Any]:
        """Snapshot of where the app is: page stack, sheets, player, models."""
        ctx = self.engine.rootContext()
        player = ctx.contextProperty("player")
        albums = ctx.contextProperty("albums")
        stack = self.find("stack", visible=False)
        np = self.find("nowPlaying", visible=False)
        cur = stack.property("currentItem") if stack is not None else None
        out: Dict[str, Any] = {
            "depth": stack.property("depth") if stack is not None else None,
            "page": cur.metaObject().className().split("_QML")[0] if cur is not None else None,
            "title": cur.property("title") if cur is not None else None,
            "nowPlaying": bool(np is not None and np.property("shown")),
            "window": [self.window.width(), self.window.height()],
        }
        if player is not None:
            out["player"] = {
                k: player.property(k)
                for k in (
                    "title",
                    "artist",
                    "album",
                    "itemId",
                    "playing",
                    "position",
                    "duration",
                    "repeat",
                    "shuffle",
                    "favorite",
                    "source",
                )
            }
        if albums is not None:
            out["albums"] = {
                "count": albums.property("count"),
                "loading": albums.property("loading"),
            }
        return out

    def eval(self, js: str) -> Any:
        """Evaluate a QML/JS expression with the window as ``this`` scope
        (ids of Main.qml in scope, plus the context properties)."""
        from PySide6.QtQml import QQmlEngine, QQmlExpression

        ctx = QQmlEngine.contextForObject(self.window) or self.engine.rootContext()
        expr = QQmlExpression(ctx, self.window, js)
        value, error = expr.evaluate()
        if error:
            raise RigError(expr.error().toString())
        if isinstance(value, QObject):
            return repr(value)
        return value

    # ── fixtures ──────────────────────────────────────────────────────────
    def fake_now_playing(
        self,
        title: str = "Test Track",
        artist: str = "Test Artist",
        album: str = "Test Album",
        *,
        item_id: str = "fake-track",
        album_id: str = "",
        cover: str = "",
        duration_ms: int = 215_000,
        position_ms: int = 60_000,
        playing: bool = True,
    ) -> None:
        """Fill the now-playing state WITHOUT playing anything — for layout
        work on the mini player / Now Playing with no audio."""
        from jellytoast.player_state import get_now_playing

        np = get_now_playing()
        np.item_id, np.title, np.subtitle, np.album = item_id, title, artist, album
        np.image_id, np.thumb_url, np.duration = "", cover, duration_ms
        np.raw = {"AlbumId": album_id} if album_id else {}
        player = self.engine.rootContext().contextProperty("player")
        player._duration, player._position, player._playing = duration_ms, position_ms, playing
        player._source = album
        player.changed.emit()
        player.positionChanged.emit()
        player.contextChanged.emit()
        pump(50)
