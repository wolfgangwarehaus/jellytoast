"""contrast_ink picks a legible glyph ink for the download badge arrow + the
eyedropper swatch glyph (bug-hunt finding #5, 2026-06-07).

A hardcoded white arrow went sub-AA (~2.4–2.8:1) on the green / teal / orange
accent presets. contrast_ink uses WCAG relative luminance: white only on dark
fills, near-black otherwise — so it clears the contrast bar on every preset.
"""

from PySide6.QtGui import QColor

from jellytoast.theme import contrast_ink


def test_dark_fills_get_white():
    for hexc in ("#000000", "#101018", "#1a1a2e", "#222222"):
        assert contrast_ink(QColor(hexc)).name() == "#ffffff", hexc


def test_mid_and_light_fills_get_near_black():
    # The accent presets a white arrow used to fail on, plus light fills.
    for hexc in ("#2fbe8a", "#1eb1ab", "#e28336", "#34d399", "#ffffff", "#967de1"):
        assert contrast_ink(QColor(hexc)).name() == "#1a1a1a", hexc
