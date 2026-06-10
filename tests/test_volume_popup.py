"""Regression tests for `_VolumeSliderPopup` construction.

Commit 468c599 ("mini-player volume slot") moved the slider + layout
creation inside `_apply_right_edge_qss`, which only runs in right-edge
mode. That left the center-mode popup (the now-playing bar) with no
`self.slider`, so `set_value()` raised AttributeError and silently
aborted `VolumeButton._show_popup` — the slider never appeared on the
main window. These tests pin the slider into existence for both modes.
"""

import pytest

from jellytoast.ui_helpers import WASH_HOVER, volume_popup_fill
from jellytoast.volume_button import _GroupVolumePopup, _VolumeSliderPopup


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


class TestGroupVolumePopupFrost:
    """The cast-GROUP volume popup is a TOP-LEVEL frosted window — the
    same true-frost conversion the single-device popup got 2026-06-09.
    It had been left behind as an in-window child with an opaque pill,
    so when casting to a group the mixer visibly didn't match the
    single popup / tooltips / menus (reported 2026-06-10, Windows
    round). Both popups are now ToolTip-class top-levels with a
    transparent QSS body; paintEvent Source-paints the status-aware
    popup_paint_qcolor (near-opaque on a no-blur box — never
    see-through, preserving the 2026-06-02 WASH_HOVER lesson)."""

    def test_group_popup_is_toplevel_frosted(self, host):
        from PySide6.QtCore import Qt

        group = _GroupVolumePopup(host)
        assert group._toplevel is True
        assert bool(group.windowFlags() & Qt.WindowType.ToolTip)
        assert "background: transparent" in group.styleSheet()
        assert group.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        # The translucent hover wash must never come back as the body.
        assert f"background: {WASH_HOVER}" not in group.styleSheet()

    def test_single_and_group_share_the_toplevel_contract(self, host):
        """Both popups must present the same surface — top-level +
        transparent QSS + Source-painted frost — so single-device and
        group cast modes read identically."""
        from PySide6.QtCore import Qt

        single = _VolumeSliderPopup(host)
        group = _GroupVolumePopup(host)
        for popup in (single, group):
            assert popup._toplevel is True
            assert bool(popup.windowFlags() & Qt.WindowType.ToolTip)
            assert "background: transparent" in popup.styleSheet()

    def test_group_popup_restamps_transparent_body_on_theme_change(self, host):
        """`_reapply_accent` (theme_changed) re-stamps the chrome QSS; the
        body must STAY transparent (paintEvent reads popup_paint_qcolor
        live, so the flip recolors the frost without a baked fill)."""
        popup = _GroupVolumePopup(host)
        popup._reapply_accent()
        assert "background: transparent" in popup.styleSheet()
        assert volume_popup_fill() not in popup.styleSheet()


class TestVolumePopupFillNeutral:
    """``volume_popup_fill()`` must be a NEUTRAL opaque tone (no blue cast)
    so the popup reads as the same elevated surface as the volume button's
    hover highlight (WASH_HOVER is a neutral white wash). The bug it fixes:
    the popup borrowed the global POPUP_OPAQUE_FILL, which on frosted dark
    is deliberately blue-tinted (64,67,74) → the popup looked "cooler" than
    the button it sits over (reported 2026-06-07)."""

    def test_fill_is_opaque_rgb(self, qapp):
        assert volume_popup_fill().startswith("rgb(")
        assert "rgba(" not in volume_popup_fill()

    def test_fill_has_no_blue_cast(self, qapp):
        """Baked from a neutral white/off-white wash over a neutral body —
        the channels must stay within a hair of each other (the old token
        had blue 10 units over red)."""
        inner = volume_popup_fill()[volume_popup_fill().index("(") + 1 : -1]
        r, g, b = (int(x) for x in inner.split(","))
        # Blue must not run hotter than red by more than a rounding unit —
        # the frosted-dark theme's wash is pure white, so r==g==b there.
        assert b - r <= 4, f"blue cast detected: {volume_popup_fill()}"


class TestVolumePopupFillTracksButtonHover:
    """The popup is a flat OPAQUE pill (child surface — it can't ride KWin
    blur) but it must read as the hovered volume BUTTON, not a stark white
    slab. On the LIGHT frosted theme the old code dropped the token's alpha
    and baked its near-white wash (248,248,248 @ 0.80) to a flat 248 —
    whiter than the 0.55-alpha button highlight it sits over (reported by
    eye 2026-06-08; an interim ``min(lum, 238)`` cap still read white). The
    fill now composites ``wash_hover`` over a representative backdrop so it
    lands at the button's apparent tone in each family (≈224 light / ≈74
    dark). Solid themes keep their opaque ``rgb()`` token unchanged."""

    @staticmethod
    def _lum(monkeypatch, name):
        import jellytoast.theme as theme_mod
        from jellytoast.theme import THEMES
        from jellytoast.ui_helpers import volume_popup_fill

        # Patch the lookup the function does internally so no real config is
        # read or written — fully hermetic, theme forced in-memory.
        monkeypatch.setattr(theme_mod, "get_active_theme", lambda: THEMES[name])
        fill = volume_popup_fill()
        return int(fill[fill.index("(") + 1 : -1].split(",")[0])

    def test_light_frosted_is_not_stark_white(self, qapp, monkeypatch):
        # Was a near-white 248 (238 even after the interim cap); must drop
        # clearly below that toward the hovered-button tone (~224) while
        # staying a light pill.
        lum = self._lum(monkeypatch, "frosted_light")
        assert 205 <= lum <= 234, lum

    def test_light_frosted_below_raw_token_luminance(self, qapp, monkeypatch):
        # Pins the alpha-respecting behaviour: the fill is no longer the bare
        # luminance of popup_opaque_fill (248 on frosted_light).
        from jellytoast.theme import THEMES
        from jellytoast.ui_helpers import _parse_qss_color

        r, g, b, _a = _parse_qss_color(THEMES["frosted_light"].popup_opaque_fill)
        raw = round(0.2126 * r + 0.7152 * g + 0.0722 * b)
        assert self._lum(monkeypatch, "frosted_light") < raw

    def test_dark_frosted_stays_a_mid_dark_pill(self, qapp, monkeypatch):
        # Dark already read fine; it must stay a mid-dark pill (~74), not
        # lurch lighter or darker on the new derivation.
        assert 60 <= self._lum(monkeypatch, "frosted_dark") <= 86

    def test_solid_themes_unchanged(self, qapp, monkeypatch):
        # Solid themes carry an opaque rgb() token — the derivation must be a
        # no-op (light 238 / dark 30) so only the frosted families move.
        assert self._lum(monkeypatch, "light") == 238
        assert self._lum(monkeypatch, "dark") == 30


class TestSoftwareBackdropBlur:
    """The volume popup can't ride compositor blur (it's a child surface), so
    when verified blur is active it fakes frosted glass: grab the host pixels
    it covers, blur them in software (ui_helpers.capture_blurred_backdrop), and
    paint that + a thin veil in paintEvent. Gated on popup_blur_active() so on a
    no-blur box it's byte-identical to the opaque pill."""

    def test_capture_returns_padded_blurred_pixmap(self, qapp):
        from PySide6.QtCore import QRect
        from PySide6.QtWidgets import QWidget

        from jellytoast.ui_helpers import VOLUME_BACKDROP_RADIUS, capture_blurred_backdrop

        host = QWidget()
        host.resize(200, 200)
        pix = capture_blurred_backdrop(host, QRect(60, 50, 36, 101))
        assert pix is not None and not pix.isNull()
        # Grab is expanded by the blur radius on every side (padding so the
        # blur has room to bleed instead of a hard clipped edge). Compare in
        # LOGICAL px — the pixmap is physical (×dpr) and dpr-tagged.
        r = VOLUME_BACKDROP_RADIUS
        dpr = pix.devicePixelRatio() or 1.0
        assert round(pix.width() / dpr) == 36 + 2 * r
        assert round(pix.height() / dpr) == 101 + 2 * r

    def test_capture_is_null_safe(self, qapp):
        from PySide6.QtCore import QRect

        from jellytoast.ui_helpers import capture_blurred_backdrop

        assert capture_blurred_backdrop(None, QRect(0, 0, 36, 101)) is None

    def test_veil_is_semi_transparent(self, qapp):
        from jellytoast.ui_helpers import volume_popup_veil_qcolor

        # The veil must let the blur read through (alpha < fully opaque).
        assert 0 < volume_popup_veil_qcolor().alpha() < 255

    def test_backdrop_stays_none_without_blur(self, host, monkeypatch):
        # No verified blur (the default in tests) → no backdrop captured, popup
        # paints the opaque pill exactly as before.
        import jellytoast.ui_helpers as u

        monkeypatch.setattr(u, "popup_blur_active", lambda: False)
        popup = _VolumeSliderPopup(host)
        popup._refresh_backdrop()
        assert popup._backdrop is None

    def test_both_modes_toplevel_carry_no_software_backdrop(self, host, monkeypatch):
        # Both popups are top-level windows riding REAL KWin blur now, so
        # neither captures a software backdrop even with blur verified (the
        # paintEvent Source-paints the frosted body; the compositor blurs).
        import jellytoast.ui_helpers as u

        host.resize(200, 200)
        monkeypatch.setattr(u, "popup_blur_active", lambda: True)
        for right_edge in (False, True):
            popup = _VolumeSliderPopup(host, right_edge_mode=right_edge)
            popup.setGeometry(40, 30, popup.POPUP_W, popup.POPUP_H)
            assert popup._toplevel is True
            popup._refresh_backdrop()
            assert popup._backdrop is None

    def test_body_path_covers_both_modes(self, host):
        centre = _VolumeSliderPopup(host)
        assert not centre._body_path().isEmpty()
        edge = _VolumeSliderPopup(host, height=96, right_edge_mode=True)
        assert not edge._body_path().isEmpty()


class TestBitPerfectLockCentering:
    """The bit-perfect padlock must sit on the slider's track centre. It was
    positioned from a stale popup size in __init__ and never re-centred when
    the right-edge popup resized to the player height, so it rendered
    off-centre (reported 2026-06-05 on the laptop install)."""

    def test_lock_centers_on_slider_after_resize(self, host):
        from PySide6.QtWidgets import QApplication

        popup = _VolumeSliderPopup(host, height=200, right_edge_mode=True)
        popup.set_locked(True)
        # Resize away from the construction size — the original bug left the
        # lock pinned to the stale geometry's centre.
        popup.resize(44, 240)
        QApplication.processEvents()
        assert popup._lock_overlay is not None
        lock_cx = popup._lock_overlay.x() + popup._lock_overlay.width() // 2
        slider_cx = popup.slider.x() + popup.slider.width() // 2
        assert abs(lock_cx - slider_cx) <= 1
        lock_cy = popup._lock_overlay.y() + popup._lock_overlay.height() // 2
        slider_cy = popup.slider.y() + popup.slider.height() // 2
        assert abs(lock_cy - slider_cy) <= 1

    def test_lock_recenters_on_second_resize(self, host):
        from PySide6.QtWidgets import QApplication

        popup = _VolumeSliderPopup(host, height=120, right_edge_mode=True)
        popup.set_locked(True)
        popup.resize(44, 160)
        QApplication.processEvents()
        popup.resize(64, 320)  # a later reposition must re-centre, not stick
        QApplication.processEvents()
        lock_cx = popup._lock_overlay.x() + popup._lock_overlay.width() // 2
        slider_cx = popup.slider.x() + popup.slider.width() // 2
        assert abs(lock_cx - slider_cx) <= 1


class TestGroupPopupCollapsedFootprint:
    """The collapsed group popup must reopen at its collapsed footprint.

    Qt's layout cache can hold a stale EXPANDED sizeHint across an
    expand→collapse→reopen cycle (the cache problem
    ``_snap_to_collapsed_size`` documents); the open path used a blanket
    ``adjustSize()`` that trusted the cache, and once the popup became a
    top-level window the inflated footprint actually drew — "collapsed
    group popup sometimes opens too wide" (2026-06-10 Windows round).
    ``VolumeButton._show_popup`` now snaps a collapsed group popup to
    its collapsed size instead. The contract pinned here: a reopen
    after an expand/collapse cycle is EXACTLY as wide as a fresh open,
    and both are far narrower than the expanded mixer.
    """

    def _group_button(self, qapp):
        from types import SimpleNamespace

        from jellytoast.player_state import PlayerBus
        from jellytoast.volume_button import VolumeButton

        btn = VolumeButton(PlayerBus())
        btn.set_cast_manager(
            SimpleNamespace(
                active_cast=SimpleNamespace(cast_type="group", uuid="g-1"),
                group_members_async=lambda dev, cb: None,
            )
        )
        return btn

    def test_reopen_after_expand_collapse_matches_fresh_width(self, qapp):
        btn = self._group_button(qapp)
        btn._show_popup()
        popup = btn._popup
        assert isinstance(popup, _GroupVolumePopup)
        fresh_w = popup.width()
        # Expand with members, collapse, hide — the cycle that leaves a
        # stale expanded sizeHint in Qt's layout cache.
        popup._on_toggle()
        popup.set_members(
            [
                {"uuid": "a", "name": "Kitchen", "volume": 40, "available": True},
                {"uuid": "b", "name": "Patio", "volume": 60, "available": True},
            ]
        )
        expanded_w = popup.width()
        popup.collapse()
        popup.hide()
        btn._show_popup()  # reopen — must NOT inflate to the cached hint
        assert btn._popup.width() == fresh_w
        assert btn._popup.width() < expanded_w
        btn._popup.hide()
        btn.deleteLater()

    def test_fresh_open_is_narrow(self, qapp):
        # Sanity floor for the contract above: a collapsed popup is the
        # arrow column + master column + chrome, nowhere near the
        # expanded mixer footprint.
        btn = self._group_button(qapp)
        btn._show_popup()
        assert btn._popup.width() <= 120
        btn._popup.hide()
        btn.deleteLater()
