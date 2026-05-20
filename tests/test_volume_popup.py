"""Regression tests for `_VolumeSliderPopup` construction.

Commit 468c599 ("mini-player volume slot") moved the slider + layout
creation inside `_apply_right_edge_qss`, which only runs in right-edge
mode. That left the center-mode popup (the now-playing bar) with no
`self.slider`, so `set_value()` raised AttributeError and silently
aborted `VolumeButton._show_popup` — the slider never appeared on the
main window. These tests pin the slider into existence for both modes.
"""

import pytest

from modules.now_playing_bar import _VolumeSliderPopup


@pytest.fixture
def host(qapp):
    from PySide6.QtWidgets import QWidget

    return QWidget()


class TestVolumeSliderPopupConstruction:
    def test_center_mode_has_slider(self, host):
        """The now-playing bar popup (default center mode) must build a
        slider — the regression left it absent."""
        popup = _VolumeSliderPopup(host)
        assert hasattr(popup, "slider")

    def test_center_mode_set_value_does_not_raise(self, host):
        """`VolumeButton._show_popup` calls `set_value` before `show()`;
        a missing slider made that raise AttributeError and aborted the
        whole show path."""
        popup = _VolumeSliderPopup(host)
        popup.set_value(55)
        assert popup.slider.value() == 55

    def test_right_edge_mode_has_slider(self, host):
        popup = _VolumeSliderPopup(host, height=96, right_edge_mode=True)
        popup.set_value(33)
        assert popup.slider.value() == 33

    def test_reapply_right_edge_qss_keeps_same_slider(self, host):
        """`_position_popup` re-runs `_apply_right_edge_qss` on every
        reposition; it must be a pure stylesheet refresh and not rebuild
        the layout/slider (which orphaned widgets before the fix)."""
        popup = _VolumeSliderPopup(host, height=96, right_edge_mode=True)
        original = popup.slider
        popup._apply_right_edge_qss(top_right_radius=12)
        assert popup.slider is original
