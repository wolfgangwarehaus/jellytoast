"""Phone UI: closing the window while music plays keeps it playing in the
background (hidden), and launching the app again or the media controls'
"open" bring the window back. Closing while paused / stopped still quits."""

from __future__ import annotations

import pytest
from PySide6.QtCore import QEvent
from PySide6.QtGui import QWindow

from jellytoast import quick
from jellytoast.player_state import NowPlaying, PlayerBus


class _Close:
    def __init__(self, spontaneous=True):
        self._spont = spontaneous
        self.ignored = False

    def type(self):
        return QEvent.Type.Close

    def spontaneous(self):
        return self._spont

    def ignore(self):
        self.ignored = True


@pytest.fixture
def window(qapp, monkeypatch):
    w = QWindow()
    w.show()
    state = {"np": NowPlaying()}
    monkeypatch.setattr("jellytoast.player_state.get_now_playing", lambda: state["np"])
    yield w, state
    cb = getattr(w, "_jt_bring_back", None)
    if cb is not None:
        PlayerBus.get().open_main_window.disconnect(cb)
    w.destroy()


def _keeper(app, w):
    quick._keep_running_in_background(app, w)
    return w._jt_close_keeper


def test_close_while_playing_hides_and_keeps_running(qapp, window):
    w, state = window
    state["np"] = NowPlaying(item_id="t1", is_paused=False)
    ev = _Close()
    assert _keeper(qapp, w).eventFilter(w, ev) is True
    assert ev.ignored
    assert not w.isVisible()


@pytest.mark.parametrize("np_kwargs", [{}, {"item_id": "t1", "is_paused": True}])
def test_close_while_not_playing_quits(qapp, window, np_kwargs):
    w, state = window
    state["np"] = NowPlaying(**np_kwargs)
    ev = _Close()
    assert _keeper(qapp, w).eventFilter(w, ev) is False
    assert not ev.ignored


def test_a_quit_the_app_asked_for_is_not_intercepted(qapp, window):
    w, state = window
    state["np"] = NowPlaying(item_id="t1", is_paused=False)
    ev = _Close(spontaneous=False)  # app.quit(), MPRIS Quit, session end
    assert _keeper(qapp, w).eventFilter(w, ev) is False


def test_media_controls_open_brings_the_window_back(qapp, window):
    w, _state = window
    _keeper(qapp, w)
    w.hide()
    PlayerBus.get().open_main_window.emit()
    assert w.isVisible()


def test_launching_again_brings_the_window_back(qapp, window, monkeypatch):
    from PySide6.QtCore import QObject, Signal

    class _Single(QObject):
        raise_requested = Signal()

    single = _Single()
    monkeypatch.setattr(qapp, "_single_instance", single, raising=False)
    w, _state = window
    _keeper(qapp, w)
    w.hide()
    single.raise_requested.emit()
    assert w.isVisible()
