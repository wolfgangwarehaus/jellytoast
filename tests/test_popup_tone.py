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


def test_popup_body_fill_opaque_without_blur(qapp, monkeypatch):
    # No verified blur → the popup MUST stay the opaque token (a bare combo /
    # menu paints this directly and gets no blur.apply()).
    monkeypatch.setattr(uh, "popup_blur_active", lambda: False)
    assert uh.popup_body_fill() == uh.POPUP_OPAQUE_FILL


def _flip_light():
    from modules import icons
    from modules.settings import get_settings

    get_settings().theme_mode = "frosted_light"
    uh.refresh_theme()
    icons.refresh_theme()


class TestLightPopupFrost:
    """The light family's POPUP_OPAQUE_FILL is tuned opaque (alpha 0.80), so
    its menus read as stark white over the frosted body. With verified blur
    the painted alpha is capped to _POPUP_FROST_ALPHA so the blur lifts
    through ('a slight lift, still frosty')."""

    def test_menu_fill_frosts_under_blur(self, qapp, isolated_settings, monkeypatch):
        from modules.settings import get_settings

        _flip_light()
        try:
            monkeypatch.setattr(uh, "popup_blur_active", lambda: True)
            _r, _g, _b, a = uh._parse_qss_color(uh.popup_body_fill())
            assert a <= uh._POPUP_FROST_ALPHA + 1e-6  # frosty, not the 0.80 token
            # popup_paint_qcolor (tooltip/About/selector) caps to the same depth.
            assert uh.popup_paint_qcolor().alpha() <= round(uh._POPUP_FROST_ALPHA * 255)
        finally:
            get_settings().theme_mode = "auto"
            uh.refresh_theme()

    def test_volume_popup_not_stark_white(self, qapp, isolated_settings):
        # Opaque child surface — can't blur, so it just must not be near-white.
        _flip_light()
        try:
            r, g, b, _a = uh._parse_qss_color(uh.volume_popup_fill())
            assert r <= 238 and g <= 238 and b <= 238
        finally:
            from modules.settings import get_settings

            get_settings().theme_mode = "auto"
            uh.refresh_theme()
