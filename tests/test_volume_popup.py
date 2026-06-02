"""Regression tests for `_VolumeSliderPopup` construction.

Commit 468c599 ("mini-player volume slot") moved the slider + layout
creation inside `_apply_right_edge_qss`, which only runs in right-edge
mode. That left the center-mode popup (the now-playing bar) with no
`self.slider`, so `set_value()` raised AttributeError and silently
aborted `VolumeButton._show_popup` — the slider never appeared on the
main window. These tests pin the slider into existence for both modes.
"""

import pytest

from modules.ui_helpers import POPUP_OPAQUE_FILL, WASH_HOVER
from modules.volume_button import _GroupVolumePopup, _VolumeSliderPopup


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


class TestGroupVolumePopupOpacity:
    """The cast-GROUP volume popup must use the same opaque body fill as
    the single-device popup. It had regressed to WASH_HOVER (the
    translucent icon-button hover wash), so when casting to a group the
    popup read as far too see-through over the frosted body (reported
    2026-06-02)."""

    def test_group_popup_body_is_opaque_fill(self, host):
        popup = _GroupVolumePopup(host)
        qss = popup.styleSheet()
        assert POPUP_OPAQUE_FILL in qss
        # The body must NOT be the translucent hover wash anymore.
        assert f"background: {WASH_HOVER}" not in qss

    def test_group_popup_matches_single_device_fill(self, host):
        """Parity: both popups float over the frosted body and share the
        opaque fill — the divergence is exactly what this fix closes."""
        single = _VolumeSliderPopup(host)
        group = _GroupVolumePopup(host)
        assert POPUP_OPAQUE_FILL in single.styleSheet()
        assert POPUP_OPAQUE_FILL in group.styleSheet()

    def test_group_popup_restamps_opaque_fill_on_theme_change(self, host):
        """`_reapply_accent` (theme_changed) must re-stamp the body so a
        dark↔light flip recolors the opaque pill rather than leaving the
        prior mode's fill."""
        popup = _GroupVolumePopup(host)
        popup._reapply_accent()
        assert POPUP_OPAQUE_FILL in popup.styleSheet()
