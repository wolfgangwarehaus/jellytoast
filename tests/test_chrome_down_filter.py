"""``_ChromeDownFilter`` — the app-level Down-arrow dive from chrome into
the content surface — must EXEMPT a focused control that owns Up/Down for
itself (the volume button: arrows adjust level + open the slider popup).

Regression: chrome-Down is an *application* event filter, so it ran before
the volume button's own keyPressEvent and swallowed Down — Up adjusted the
volume but Down silently dived into the content list instead (caught only
by live testing). The fix is a duck-typed ``_consumes_arrow_keys`` marker
the filter honours.

``QApplication.focusWidget()`` / ``activePopupWidget()`` are patched so the
filter's branch is exercised deterministically without real window
activation (flaky offscreen).
"""

from types import SimpleNamespace

from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import QApplication, QWidget

from jellytoast.app import _ChromeDownFilter


def _down_event():
    return QKeyEvent(
        QEvent.Type.KeyPress, Qt.Key.Key_Down, Qt.KeyboardModifier.NoModifier
    )


def _window_with_content(content):
    """A stand-in main window exposing just the bits the filter reads."""
    return SimpleNamespace(
        content_stack=SimpleNamespace(currentWidget=lambda: content)
    )


def test_exempts_arrow_consuming_widget(qapp, monkeypatch):
    dived = []
    content = QWidget()
    content.focus_first_item = lambda: dived.append(True)
    consumer = QWidget()
    consumer._consumes_arrow_keys = True

    flt = _ChromeDownFilter(_window_with_content(content))
    monkeypatch.setattr(QApplication, "activePopupWidget", lambda: None)
    monkeypatch.setattr(QApplication, "focusWidget", lambda: consumer)

    # The filter must bail (return False) and NOT dive into the content.
    assert flt.eventFilter(consumer, _down_event()) is False
    assert dived == []


def test_dives_for_plain_chrome_widget(qapp, monkeypatch):
    # Contrast: a focused chrome control WITHOUT the marker still dives, so
    # the exemption is narrow and didn't break chrome-Down for everything.
    dived = []
    win = QWidget()  # real top-level so focused.window() is self._w
    content = QWidget()
    content.focus_first_item = lambda: dived.append(True)
    plain = QWidget(win)  # child of the window → window() is win

    flt = _ChromeDownFilter(win)
    win.content_stack = SimpleNamespace(currentWidget=lambda: content)
    monkeypatch.setattr(QApplication, "activePopupWidget", lambda: None)
    monkeypatch.setattr(QApplication, "focusWidget", lambda: plain)

    assert flt.eventFilter(plain, _down_event()) is True
    assert dived == [True]
