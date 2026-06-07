"""Swatch ring / outline colors are theme-aware (findings #2 + #3, 2026-06-07).

Two swatches drew their outline in a hardcoded color:

* ``settings_dialog._AccentSwatch`` selection ring was hardcoded black —
  invisible against the dark default theme's dialog body (#2, medium).
* ``settings_colors_page._Swatch`` definition outline was hardcoded
  translucent white — invisible on light-fill tokens in a light theme (#3).

Both now paint with theme-aware ink (``modules.theme.ink_rgb`` → white on
dark, near-black on light), which reads ``ui_helpers.TEXT`` at paint time.

Each test renders the swatch under two ink values and asserts the rendered
output DIFFERS — a hardcoded ring/outline would render identically
regardless of the active theme's ink.

`qapp` (conftest.py) provides the QApplication widget rendering needs.
"""

from PySide6.QtCore import QBuffer, QByteArray, QIODevice
from PySide6.QtGui import QColor

import modules.ui_helpers as ui_helpers


def _render_under_ink(widget, monkeypatch, hex_text: str) -> bytes:
    """Grab the widget to a PNG with the theme ink token forced to
    ``hex_text``. ink_rgb() reads ui_helpers.TEXT live, so the paint path
    picks this up on the next grab()."""
    monkeypatch.setattr(ui_helpers, "TEXT", hex_text)
    img = widget.grab().toImage()
    ba = QByteArray()
    buf = QBuffer(ba)
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    img.save(buf, "PNG")
    return bytes(ba)


def test_accent_swatch_selection_ring_is_theme_aware(qapp, monkeypatch):
    from modules.settings_dialog import _AccentSwatch

    sw = _AccentSwatch("#3b82f6")
    sw.setChecked(True)  # the selection ring — the headline #2 case
    on_dark = _render_under_ink(sw, monkeypatch, "#ffffff")
    on_light = _render_under_ink(sw, monkeypatch, "#000000")
    assert on_dark != on_light, (
        "accent swatch selection ring must change with theme ink "
        "(regression: was hardcoded black, invisible on the dark default)"
    )


def test_color_token_swatch_outline_is_theme_aware(qapp, monkeypatch):
    from modules.settings_colors_page import _Swatch

    sw = _Swatch(QColor("#ffffff"))  # a light-fill token — the #3 case
    on_dark = _render_under_ink(sw, monkeypatch, "#ffffff")
    on_light = _render_under_ink(sw, monkeypatch, "#000000")
    assert on_dark != on_light, (
        "color-token swatch outline must change with theme ink "
        "(regression: was hardcoded white, invisible on light-fill tokens)"
    )
