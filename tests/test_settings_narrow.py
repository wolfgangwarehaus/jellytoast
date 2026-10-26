"""Settings opened from a narrow window (august, 2026-10-26): at 360 px,
Playback, Display, Hotkeys and Casting were wider than the dialog and
scrolled sideways (the EQ, a long button label, fixed combo widths,
one-line labels). Every page must fit its viewport with no horizontal
scroll; the desktop dialog keeps its full layout."""

import pytest
from PySide6.QtWidgets import QScrollArea

from jellytoast import responsive


def _page_scroll(dlg):
    areas = [
        s
        for s in dlg.findChildren(QScrollArea)
        if s.isVisible() and s.widget() is not None and s.widget().isVisible()
    ]
    return max(areas, key=lambda s: s.widget().height()) if areas else None


@pytest.fixture
def narrow_dialog(qapp, isolated_settings, monkeypatch):
    monkeypatch.setattr(responsive, "is_compact", lambda: True)
    from jellytoast.settings_dialog import SettingsDialog

    dlg = SettingsDialog()
    dlg.resize(360, 720)
    dlg.show()
    yield dlg
    dlg.close()
    dlg.deleteLater()


def test_every_settings_page_fits_360px(qapp, narrow_dialog):
    dlg = narrow_dialog
    too_wide = {}
    for i in range(dlg.nav.count()):
        dlg.nav.setCurrentRow(i)
        for _ in range(3):
            qapp.processEvents()
        area = _page_scroll(dlg)
        if area is None:
            continue
        over = area.horizontalScrollBar().maximum()
        if over > 0:
            too_wide[dlg.nav.item(i).text()] = over
    assert not too_wide, f"pages scroll sideways at 360 px: {too_wide}"


def test_narrow_settings_uses_the_short_bit_perfect_label(qapp, narrow_dialog):
    dlg = narrow_dialog
    dlg.nav.setCurrentRow(1)  # Playback
    btn = getattr(dlg, "_pw_install_btn", None)
    if btn is None:
        pytest.skip("no PipeWire bit-perfect row on this platform")
    assert "PipeWire" not in btn.text()
