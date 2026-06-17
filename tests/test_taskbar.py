"""Windows taskbar overlay badge (jellytoast.taskbar).

The comtypes ITaskbarList3 path is Windows-only and verified on-device.
On Linux CI we assert what's portable: the badge images render to valid
PNGs (pure Qt), the controller is a clean no-op without Windows, and the
play↔pause state tracking + dedup behave.
"""

import jellytoast.taskbar as taskbar_mod
from jellytoast.taskbar import TaskbarOverlay, _badge_png


class TestBadgeRender:
    def test_play_and_pause_render_to_png(self, qapp):
        for kind in ("play", "pause"):
            data = _badge_png(kind)
            assert isinstance(data, bytes) and len(data) > 0
            # PNG signature.
            assert data[:8] == b"\x89PNG\r\n\x1a\n"


class TestController:
    def test_start_is_noop_off_windows(self, qapp):
        ov = TaskbarOverlay()
        ov.start(None)  # IS_WINDOWS False → returns before touching window
        assert ov._taskbar is None
        ov.stop()  # safe even though it never came up

    def test_state_tracking_and_dedup(self, qapp):
        ov = TaskbarOverlay()
        # _apply early-returns while not ready, so these can't raise even
        # without a taskbar COM object.
        ov._set_state("play")
        assert ov._desired == "play"
        ov._set_state("play")  # dedup
        assert ov._desired == "play"
        ov._set_state("pause")
        assert ov._desired == "pause"
        ov._set_state(None)
        assert ov._desired is None


class _FakeTaskbar:
    def __init__(self):
        self.calls = []

    def SetOverlayIcon(self, hwnd, hicon, desc):
        self.calls.append((hwnd, hicon, desc))


class TestFailedHiconNotCached:
    """A failed badge build (HICON 0) must not be cached, and must not be
    handed to SetOverlayIcon — a NULL handle clears the overlay, and a
    cached 0 would pin it off for the whole session."""

    def _ready_overlay(self):
        ov = TaskbarOverlay()
        ov._ready = True
        ov._hwnd = 1
        ov._taskbar = _FakeTaskbar()
        return ov

    def test_zero_hicon_skips_call_and_is_not_cached(self, qapp, monkeypatch):
        monkeypatch.setattr(taskbar_mod, "_badge_ico_path", lambda kind: "x.ico")
        monkeypatch.setattr(taskbar_mod, "_load_hicon", lambda path: 0)
        ov = self._ready_overlay()
        ov._set_state("play")  # build fails → 0
        assert ov._taskbar.calls == []  # never passed NULL to SetOverlayIcon
        assert "play" not in ov._hicons  # 0 not cached → retries next time

    def test_recovers_once_the_load_succeeds(self, qapp, monkeypatch):
        monkeypatch.setattr(taskbar_mod, "_badge_ico_path", lambda kind: "x.ico")
        handles = iter([0, 4242])  # first build fails, second succeeds
        monkeypatch.setattr(taskbar_mod, "_load_hicon", lambda path: next(handles))
        ov = self._ready_overlay()
        ov._set_state("play")  # 0 → skipped, not cached
        assert ov._taskbar.calls == []
        ov._desired = None  # force _set_state("play") to re-run _apply
        ov._set_state("play")  # retries → real handle
        assert ov._taskbar.calls == [(1, 4242, "play")]
        assert ov._hicons["play"] == 4242
