"""Tests for the in-app toast component (`jellytoast/toast.py`)."""

from PySide6.QtWidgets import QWidget

from jellytoast.toast import show_toast


def _host(qapp):
    w = QWidget()
    w.resize(800, 600)
    return w


class TestShowToast:
    def test_parents_to_host(self, qapp):
        host = _host(qapp)
        toast = show_toast(host, "hello")
        assert toast.parentWidget() is host

    def test_centred_near_bottom(self, qapp):
        host = _host(qapp)
        toast = show_toast(host, "a message")
        # Horizontally centred within the host.
        assert abs(toast.x() - (host.width() - toast.width()) // 2) <= 1
        # Sits in the lower portion, clear of the very bottom edge.
        assert toast.y() < host.height()
        assert toast.y() > host.height() // 2

    def test_bottom_margin_lifts_the_pill(self, qapp):
        host = _host(qapp)
        low = show_toast(host, "x", bottom_margin=28)
        high = show_toast(host, "x", bottom_margin=128)
        # A larger bottom margin moves the toast further up the host.
        assert high.y() < low.y()

    def test_fade_attaches_effect_and_animation(self, qapp):
        host = _host(qapp)
        toast = show_toast(host, "fading")
        toast._begin_fade()
        # The opacity effect + animation are live during the fade.
        assert toast.graphicsEffect() is not None
        assert toast._fade_anim is not None
