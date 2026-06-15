"""Windows taskbar overlay badge (jellytoast.taskbar).

The comtypes ITaskbarList3 path is Windows-only and verified on-device.
On Linux CI we assert what's portable: the badge images render to valid
PNGs (pure Qt), the controller is a clean no-op without Windows, and the
play↔pause state tracking + dedup behave.
"""

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
