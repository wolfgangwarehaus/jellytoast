"""Screen colour picker (eyedropper) — pure helpers + wiring (2026-06-07).

The live portal round-trip (user clicks a pixel) can't be unit-tested
headlessly, so these cover the deterministic parts: the 0..1-double →
``#rrggbb`` conversion, the tolerant portal-results extraction, the bundled
icon, and that both UI surfaces actually wire a dropper button.

`qapp` (conftest.py) provides the QApplication the widget/icon tests need.
"""

from jellytoast.color_picker import _color_from_results, rgb01_to_hex


def test_rgb01_to_hex_scales_and_clamps():
    assert rgb01_to_hex(1.0, 0.0, 0.0) == "#ff0000"
    assert rgb01_to_hex(0.0, 0.0, 0.0) == "#000000"
    assert rgb01_to_hex(1.0, 1.0, 1.0) == "#ffffff"
    # Out-of-range components clamp rather than overflow.
    assert rgb01_to_hex(1.5, -0.1, 0.5) == "#ff0080"


def test_color_from_results_parses_jeepney_shape():
    # jeepney represents the (ddd) variant as ('signature', (r, g, b)).
    assert _color_from_results({"color": ("(ddd)", (1.0, 0.0, 0.0))}) == "#ff0000"
    # Tolerate a bare (r, g, b) too.
    assert _color_from_results({"color": (0.0, 1.0, 0.0)}) == "#00ff00"
    assert _color_from_results({}) is None
    assert _color_from_results({"color": None}) is None


def test_eyedropper_icon_is_registered(qapp):
    from jellytoast.icons import icon

    assert not icon("eyedropper").isNull()


def test_colors_page_has_eyedropper_button(qapp):
    from PySide6.QtWidgets import QPushButton

    from jellytoast.settings_colors_page import build_colors_page

    page = build_colors_page()
    tips = [
        b.toolTip()
        for b in page.findChildren(QPushButton)
        if "screen" in b.toolTip().lower()
    ]
    assert tips, "Colors page must expose a 'pick from screen' eyedropper button"


def test_accent_row_has_eyedropper_swatch(qapp):
    from jellytoast.settings_dialog import SettingsDialog, _EyedropperSwatch

    dlg = SettingsDialog()
    try:
        dlg._build_accent_row()  # populates self._accent_dropper
        assert isinstance(dlg._accent_dropper, _EyedropperSwatch)
        assert "screen" in dlg._accent_dropper.toolTip().lower()
    finally:
        dlg.deleteLater()


def test_eyedropper_swatch_empty_then_loads_color(qapp):
    from jellytoast.settings_dialog import _EyedropperSwatch

    sw = _EyedropperSwatch()
    # Empty (no colour) and unselected until used.
    assert sw._fill is None
    assert not sw.isChecked()
    sw.grab()  # paints the empty (glyph-only) state without error

    # After sampling, the swatch holds the colour and reads as selected.
    sw.set_picked("#3b82f6")
    assert sw._fill is not None and sw._fill.name() == "#3b82f6"
    assert sw.isChecked()
    sw.grab()  # paints the filled state

    # Clearing returns it to empty/unselected.
    sw.set_picked(None)
    assert sw._fill is None
    assert not sw.isChecked()
