"""Elevated popups harden when blur isn't verified behind them (2026-06-07).

Tooltips, menus, the About dialog, and the _Selector dropdown paint the
``popup_paint_qcolor`` lifted tone. On a frosted theme that tone is translucent
(alpha ~0.65) so it composites over compositor blur — but on a box where blur
never lands it read thin / see-through. ``popup_paint_qcolor`` now tracks the
verified blur status (the popup analogue of ``body_color_tuple``): the
translucent glass tone when blur is active, a near-opaque panel otherwise.

`qapp` (conftest.py) — QColor construction is happiest with a QApplication.
"""

import modules.ui_helpers as uh


def _frosted():
    """Only meaningful when the active theme's popup fill is translucent."""
    return "rgba" in uh.POPUP_OPAQUE_FILL


def test_popup_stays_glass_when_blur_active(qapp, monkeypatch):
    if not _frosted():
        return
    monkeypatch.setattr(uh, "popup_blur_active", lambda: True)
    assert uh.popup_paint_qcolor().alpha() < 200, "should stay translucent glass"


def test_popup_hardens_when_blur_inactive(qapp, monkeypatch):
    if not _frosted():
        return
    monkeypatch.setattr(uh, "popup_blur_active", lambda: False)
    assert uh.popup_paint_qcolor().alpha() >= 230, "should harden to near-opaque"


def test_popup_blur_active_never_raises(qapp):
    # Degrades safely (returns a bool) regardless of theme / blur availability.
    assert isinstance(uh.popup_blur_active(), bool)
