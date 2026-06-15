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
