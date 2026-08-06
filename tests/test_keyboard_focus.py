"""Tests for jellytoast.keyboard_focus — the registry that replaced the
per-click ``QApplication.allWidgets()`` walk (review app-shell-2). The
mouse-press filter must clear ``_keyboard_mode`` on every REGISTERED view
(and only those), repainting any that expose a viewport."""

from jellytoast import keyboard_focus as kf


class _FakeView:
    def __init__(self):
        self._keyboard_mode = False
        self.vp_updates = 0

    def viewport(self):
        outer = self

        class _VP:
            def update(self):
                outer.vp_updates += 1

        return _VP()


def test_registered_view_is_cleared_and_repainted():
    v = _FakeView()
    kf.register_keyboard_mode_view(v)
    v._keyboard_mode = True
    kf.clear_all_keyboard_mode()
    assert v._keyboard_mode is False
    assert v.vp_updates == 1


def test_unregistered_widget_is_untouched():
    reg = _FakeView()
    kf.register_keyboard_mode_view(reg)
    unreg = _FakeView()
    unreg._keyboard_mode = True
    kf.clear_all_keyboard_mode()
    # Only registered views are touched — the old allWidgets() walk would have
    # cleared anything with the attr; the registry must NOT.
    assert unreg._keyboard_mode is True


def test_view_without_viewport_just_clears_flag():
    class _NoVP:
        def __init__(self):
            self._keyboard_mode = True

    v = _NoVP()
    kf.register_keyboard_mode_view(v)
    kf.clear_all_keyboard_mode()  # must not raise on a view with no viewport()
    assert v._keyboard_mode is False


# ── Shared list-view keyboard wiring (engage / seed / clear) ─────────────
#
# These back the focus-ring integration on every list-backed view (Songs,
# Genres, Smart playlists, Radio, Downloads, …). A real QListView +
# QStringListModel exercises the actual currentIndex()/indexAt() paths.


def _list_view(rows):
    from PySide6.QtCore import QStringListModel
    from PySide6.QtWidgets import QListView

    v = QListView()
    v._keyboard_mode = False
    v.setModel(QStringListModel(list(rows)))
    return v


def _focus_in(reason):
    from PySide6.QtGui import QFocusEvent

    return QFocusEvent(QFocusEvent.Type.FocusIn, reason)


def _key_press(key):
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QKeyEvent

    return QKeyEvent(QKeyEvent.Type.KeyPress, key, Qt.KeyboardModifier.NoModifier)


def test_focus_in_engages_and_seeds_on_keyboard(qapp):
    from PySide6.QtCore import Qt

    v = _list_view(["a", "b", "c"])
    kf.keyboard_focus_in(v, _focus_in(Qt.FocusReason.TabFocusReason))
    assert v._keyboard_mode is True
    assert v.currentIndex().isValid()


def test_focus_in_ignores_mouse(qapp):
    from PySide6.QtCore import Qt

    v = _list_view(["a", "b"])
    kf.keyboard_focus_in(v, _focus_in(Qt.FocusReason.MouseFocusReason))
    # A click must NOT light the ring — that's the whole point of the gate.
    assert v._keyboard_mode is False


def test_focus_out_clears(qapp):
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QFocusEvent

    v = _list_view(["a"])
    kf.keyboard_focus_in(v, _focus_in(Qt.FocusReason.TabFocusReason))
    kf.keyboard_focus_out(v, QFocusEvent(QFocusEvent.Type.FocusOut))
    assert v._keyboard_mode is False


def test_arrow_press_seeds_when_unseeded(qapp):
    from PySide6.QtCore import Qt

    v = _list_view(["x", "y"])
    seeded = kf.keyboard_arrow_press(v, _key_press(Qt.Key.Key_Down))
    assert seeded is True  # caller should accept + return
    assert v._keyboard_mode is True
    assert v.currentIndex().isValid()


def test_arrow_press_defers_to_super_when_seeded(qapp):
    from PySide6.QtCore import Qt

    v = _list_view(["x", "y", "z"])
    v.setCurrentIndex(v.model().index(0, 0))
    # Already seeded: engage mode but return False so super() does the move.
    assert kf.keyboard_arrow_press(v, _key_press(Qt.Key.Key_Down)) is False
    assert v._keyboard_mode is True


def test_non_arrow_key_ignored(qapp):
    from PySide6.QtCore import Qt

    v = _list_view(["x"])
    assert kf.keyboard_arrow_press(v, _key_press(Qt.Key.Key_Return)) is False


def test_focus_first_item_on_seeds_and_engages(qapp):
    v = _list_view(["p", "q"])
    kf.focus_first_item_on(v)
    assert v._keyboard_mode is True
    assert v.currentIndex().isValid()


def test_focus_first_item_on_empty_is_noop(qapp):
    v = _list_view([])
    kf.focus_first_item_on(v)
    assert v._keyboard_mode is False


def test_install_arrow_nav_traverses_visible_enabled(qapp):
    from PySide6.QtCore import QEvent, Qt
    from PySide6.QtGui import QKeyEvent
    from PySide6.QtWidgets import QHBoxLayout, QPushButton, QWidget

    host = QWidget()
    lay = QHBoxLayout(host)
    btns = [QPushButton(str(i)) for i in range(4)]
    for b in btns:
        lay.addWidget(b)
    host.show()
    qapp.processEvents()
    btns[2].setEnabled(False)  # disabled → the walker skips it
    nav = kf.install_arrow_nav(btns)

    # Keyboard-focusable but NOT click-focusable (ring stays keyboard-only).
    assert all(b.focusPolicy() == Qt.FocusPolicy.TabFocus for b in btns)

    moved = []
    for b in btns:
        b.setFocus = lambda reason=None, _b=b: moved.append(_b)  # spy

    def press(on, key):
        return nav.eventFilter(
            on, QKeyEvent(QEvent.Type.KeyPress, key, Qt.KeyboardModifier.NoModifier)
        )

    assert press(btns[0], Qt.Key.Key_Right) is True
    assert moved[-1] is btns[1]
    assert press(btns[1], Qt.Key.Key_Right) is True  # skips disabled btns[2]
    assert moved[-1] is btns[3]
    n = len(moved)
    assert press(btns[3], Qt.Key.Key_Right) is True  # last: consumed, no move
    assert len(moved) == n
    assert press(btns[1], Qt.Key.Key_Left) is True
    assert moved[-1] is btns[0]
    assert press(btns[0], Qt.Key.Key_Space) is False  # non-arrow passes through


def test_arrow_nav_rejects_non_keyboard_focus(qapp):
    """A chrome button only KEEPS focus (so it shows its accent ring) when
    focus arrived via the keyboard. Boot auto-focus (ActiveWindow) or the
    default Other reason is cleared — nothing is highlighted on launch —
    while Tab focus is kept."""
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QFocusEvent
    from PySide6.QtWidgets import QHBoxLayout, QPushButton, QWidget

    host = QWidget()
    lay = QHBoxLayout(host)
    btns = [QPushButton(str(i)) for i in range(3)]
    for b in btns:
        lay.addWidget(b)
    host.show()
    qapp.processEvents()
    nav = kf.install_arrow_nav(btns)

    cleared = []
    for b in btns:
        b.clearFocus = lambda _b=b: cleared.append(_b)  # spy

    def focus_in(on, reason):
        return nav.eventFilter(on, QFocusEvent(QFocusEvent.Type.FocusIn, reason))

    # Boot auto-focus and the default Other reason are rejected (cleared).
    focus_in(btns[0], Qt.FocusReason.ActiveWindowFocusReason)
    assert cleared[-1] is btns[0]
    focus_in(btns[1], Qt.FocusReason.OtherFocusReason)
    assert cleared[-1] is btns[1]
    # Keyboard focus (Tab) is kept — no clear.
    n = len(cleared)
    focus_in(btns[2], Qt.FocusReason.TabFocusReason)
    assert len(cleared) == n


def test_install_row_grid_nav(qapp):
    from PySide6.QtCore import QEvent, Qt
    from PySide6.QtGui import QFocusEvent, QKeyEvent
    from PySide6.QtWidgets import QPushButton, QWidget

    rows = []
    for _ in range(2):
        r = QWidget()
        r.b0 = QPushButton(r)
        r.b1 = QPushButton(r)
        rows.append(r)
    nav = kf.install_row_grid_nav(lambda: rows, lambda r: [r.b0, r.b1])
    for r in rows:
        nav.wire(r)

    assert all(
        b.focusPolicy() == Qt.FocusPolicy.TabFocus for r in rows for b in (r.b0, r.b1)
    )

    moved = []
    for r in rows:
        for b in (r.b0, r.b1):
            b.setFocus = lambda reason=None, _b=b: moved.append(_b)

    def press(on, key):
        return nav.eventFilter(
            on, QKeyEvent(QEvent.Type.KeyPress, key, Qt.KeyboardModifier.NoModifier)
        )

    # Left/Right within a row.
    assert press(rows[0].b0, Qt.Key.Key_Right) is True
    assert moved[-1] is rows[0].b1
    # Up/Down between rows, same column.
    assert press(rows[0].b1, Qt.Key.Key_Down) is True
    assert moved[-1] is rows[1].b1
    assert press(rows[1].b0, Qt.Key.Key_Up) is True
    assert moved[-1] is rows[0].b0
    # Clamp at an edge: consumed, no move.
    n = len(moved)
    assert press(rows[0].b0, Qt.Key.Key_Left) is True
    assert len(moved) == n
    # Row highlight flag toggles on focus.
    nav.eventFilter(rows[0].b0, QFocusEvent(QFocusEvent.Type.FocusIn))
    assert rows[0]._kb_active is True


def test_keyboard_cursor_active(qapp):
    v = _list_view(["a", "b", "c"])
    v.setCurrentIndex(v.model().index(1, 0))
    idx0 = v.model().index(0, 0)
    idx1 = v.model().index(1, 0)
    # keyboard mode off → never active (a click must not light a row)
    assert kf.keyboard_cursor_active(v, idx1) is False
    v._keyboard_mode = True
    # only the current row is active
    assert kf.keyboard_cursor_active(v, idx1) is True
    assert kf.keyboard_cursor_active(v, idx0) is False
    # None view is safe
    assert kf.keyboard_cursor_active(None, idx1) is False


# ── Input modality: the focus ring is a KEYBOARD affordance ─────────────────
#
# Qt reassigns focus with OtherFocusReason when a stacked page hides, which
# is the same reason a deliberate setFocus() carries. Treating that as
# keyboard-driven lit the first album's ring every time the user clicked Home
# out of the now-playing page (field report 2026-08-06).


class _FocusView(_FakeView):
    """Adds the bits keyboard_focus_in touches beyond the flag."""

    def __init__(self, rows=3):
        super().__init__()
        self._rows = rows
        self.current = None

    def currentIndex(self):
        class _Idx:
            def __init__(self, valid):
                self._v = valid

            def isValid(self):
                return self._v

        return _Idx(self.current is not None)

    def model(self):
        outer = self

        class _M:
            def rowCount(self):
                return outer._rows

            def index(self, r, c):
                return ("idx", r, c)

        return _M()

    def indexAt(self, _pt):
        class _Bad:
            def isValid(self):
                return False

        return _Bad()

    def setCurrentIndex(self, idx):
        self.current = idx

    def rect(self):
        return self

    def topLeft(self):
        return (0, 0)


class _Evt:
    def __init__(self, reason):
        self._r = reason

    def reason(self):
        return self._r


def _viewport_rect_shim(view):
    """keyboard_focus._seed_first_index calls view.viewport().rect().topLeft();
    the fake's viewport only implements update(), so patch in the chain."""

    class _VP:
        def update(self):
            view.vp_updates += 1

        def rect(self):
            return self

        def topLeft(self):
            return (0, 0)

    view.viewport = lambda: _VP()


class TestFocusModality:
    def test_tab_focus_always_rings(self):
        from PySide6.QtCore import Qt

        v = _FocusView()
        _viewport_rect_shim(v)
        kf.note_pointer_input()  # even right after a click…
        kf.keyboard_focus_in(v, _Evt(Qt.FocusReason.TabFocusReason))
        assert v._keyboard_mode is True  # …Tab is keyboard by construction

    def test_page_switch_after_a_click_does_not_ring(self):
        """THE regression: click Home out of now-playing → the stack hands
        the grid focus with OtherFocusReason → no ring."""
        from PySide6.QtCore import Qt

        v = _FocusView()
        _viewport_rect_shim(v)
        kf.note_pointer_input()
        kf.keyboard_focus_in(v, _Evt(Qt.FocusReason.OtherFocusReason))
        assert v._keyboard_mode is False
        assert v.current is None  # and no cursor was seeded

    def test_page_switch_after_a_keypress_still_rings(self):
        """The other half: a keyboard user who tabs to Home and presses
        Enter must keep a visible cursor."""
        from PySide6.QtCore import Qt

        v = _FocusView()
        _viewport_rect_shim(v)
        kf.note_keyboard_input()
        kf.keyboard_focus_in(v, _Evt(Qt.FocusReason.OtherFocusReason))
        assert v._keyboard_mode is True
        assert v.current is not None  # cursor seeded for arrow keys

    def test_mouse_focus_never_rings(self):
        from PySide6.QtCore import Qt

        v = _FocusView()
        _viewport_rect_shim(v)
        kf.note_keyboard_input()
        kf.keyboard_focus_in(v, _Evt(Qt.FocusReason.MouseFocusReason))
        assert v._keyboard_mode is False

    def test_modality_flips_with_the_latest_input(self):
        kf.note_keyboard_input()
        assert kf.last_input_was_keyboard() is True
        kf.note_pointer_input()
        assert kf.last_input_was_keyboard() is False
