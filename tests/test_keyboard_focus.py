"""Tests for modules.keyboard_focus — the registry that replaced the
per-click ``QApplication.allWidgets()`` walk (review app-shell-2). The
mouse-press filter must clear ``_keyboard_mode`` on every REGISTERED view
(and only those), repainting any that expose a viewport."""

from modules import keyboard_focus as kf


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
