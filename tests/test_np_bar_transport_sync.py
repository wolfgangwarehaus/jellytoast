"""Regression: the now-playing-bar repeat/shuffle buttons must reflect
external repeat_changed/shuffle_changed (queue restore, MPRIS) — not just local
clicks — and without re-emitting (the shuffle button uses `toggled`, so a naive
setChecked would loop)."""

import jellytoast.now_playing_bar as npb
from jellytoast.now_playing_bar import NowPlayingBar


class _FakeBtn:
    def __init__(self, checked=False):
        self._checked = checked
        self.icons = []
        self.block_calls = []

    def isChecked(self):
        return self._checked

    def setChecked(self, v):
        self._checked = bool(v)

    def setIcon(self, ic):
        self.icons.append(ic)

    def blockSignals(self, b):
        self.block_calls.append(bool(b))


def _bar(monkeypatch):
    monkeypatch.setattr(npb, "icon", lambda name: ("icon", name))
    monkeypatch.setattr(npb, "accent_icon", lambda name: ("accent", name))
    bar = NowPlayingBar.__new__(NowPlayingBar)
    bar._repeat_state = "off"
    bar.repeat_btn = _FakeBtn()
    bar.shuffle_btn = _FakeBtn()
    return bar


def test_external_repeat_change_syncs_button(monkeypatch):
    bar = _bar(monkeypatch)
    bar._on_repeat_changed("all")
    assert bar._repeat_state == "all" and bar.repeat_btn.isChecked() is True
    assert bar.repeat_btn.icons[-1] == ("accent", "repeat")
    bar._on_repeat_changed("one")
    assert bar.repeat_btn.icons[-1] == ("accent", "repeat_one")
    bar._on_repeat_changed("off")
    assert bar.repeat_btn.isChecked() is False and bar.repeat_btn.icons[-1] == ("icon", "repeat")


def test_external_repeat_same_mode_is_noop(monkeypatch):
    bar = _bar(monkeypatch)
    bar._repeat_state = "all"
    bar._on_repeat_changed("all")
    assert bar.repeat_btn.icons == []


def test_external_shuffle_blocks_signals(monkeypatch):
    bar = _bar(monkeypatch)
    bar._on_shuffle_changed(True)
    assert bar.shuffle_btn.isChecked() is True
    assert bar.shuffle_btn.block_calls == [True, False]  # no toggled re-fire
    assert bar.shuffle_btn.icons[-1] == ("accent", "shuffle")


def test_external_shuffle_same_state_is_noop(monkeypatch):
    bar = _bar(monkeypatch)
    bar.shuffle_btn._checked = True
    bar._on_shuffle_changed(True)
    assert bar.shuffle_btn.block_calls == []
