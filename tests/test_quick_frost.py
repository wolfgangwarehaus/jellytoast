"""Frosted glass on the phone (august, 2026-10-27): the top bar, mini player
and bottom sheets blur what's behind them; Now Playing gets a blurred
album-art backdrop; Settings → Frosted glass turns it all off."""

from __future__ import annotations


def _children(item):
    out = []
    for c in item.childItems():
        out.append(c)
        out.extend(_children(c))
    return out


def _effect(pane):
    return next(c for c in _children(pane) if c.metaObject().className() == "QQuickMultiEffect")


def test_pages_scroll_under_the_frosted_top_bar(quick):
    quick.eval("win.showSection('album')")
    quick.wait_idle()
    top = quick.get("topFrost")
    grid = quick.wait_for("albumGrid")
    # The page fills the window; its content starts clear of the bar.
    assert grid.mapToItem(None, 0, 0).y() == 0
    assert abs(grid.property("topMargin") - top.height()) < 1
    assert _effect(top).isVisible()


def test_frosted_glass_switch_turns_the_blur_off(quick):
    quick.eval("prefs.setFrosted(false)")
    quick.wait_idle()
    try:
        top = quick.get("topFrost")
        assert not top.property("live")
        assert not _effect(top).isVisible()
    finally:
        quick.eval("prefs.setFrosted(true)")
    assert quick.get("topFrost").property("live")


def test_sheets_sample_the_app_only_while_open(quick):
    # A sheet holding the window's content as its texture source crashed
    # window teardown; it connects only while visible.
    sheet = quick.eval("optionsSheet")
    assert quick.eval("optionsSheet.background.children[0].behind") is None
    quick.tap("moreButton")
    quick.wait_until(lambda: quick.eval("optionsSheet.opened"))
    assert quick.eval("optionsSheet.background.children[0].behind") is not None
    quick.key("esc")
    quick.wait_until(lambda: not quick.eval("optionsSheet.visible"))
    assert quick.eval("optionsSheet.background.children[0].behind") is None
    assert sheet is not None


def test_now_playing_has_a_blurred_art_backdrop(quick):
    quick.fake_now_playing("Song", "Someone", "Album 004", album_id="al4")
    quick.eval("nowPlaying.open()")
    quick.wait_idle()
    assert quick.get("npBackdrop").isVisible()
    quick.eval("prefs.setFrosted(false)")
    try:
        quick.wait_idle()
        assert not quick.find("npBackdrop")
    finally:
        quick.eval("prefs.setFrosted(true)")


def test_album_and_artist_pages_get_a_blurred_art_header(quick):
    quick.tap("text:Album 001")
    quick.wait_for("trackList")
    quick.wait_idle()
    assert quick.get("albumBackdrop").isVisible()
    quick.key("esc")
    quick.wait_idle()
    quick.eval("win.openItem('artist', {id: 'ar1', name: 'Artist 1', cover: 'image://cover/ar1/256'})")
    quick.wait_idle()
    assert quick.get("artistBackdrop").isVisible()
    assert quick.get("artistPortrait").isVisible()


def test_accent_swatches_set_the_shared_accent(quick, isolated_settings):
    quick.eval("stack.push(settingsPage)")
    quick.wait_idle()
    row = quick.wait_for("accentRow")
    assert row.isVisible()  # built-in theme
    quick.tap("accent_Teal")
    quick.wait_idle()
    assert isolated_settings.accent_color.lower() == "#1eb1ab"
    assert quick.eval("theme.accent.toString()").lower() == "#1eb1ab"


def test_a_loading_cover_pulses_then_settles(quick):
    # A cover whose art never arrives pulses, then stops (gaveUp).
    quick.eval(
        "Qt.createQmlObject('Cover { objectName: \\'probeCover\\'; width: 40; height: 40;"
        " source: \\'image://cover/none/80\\' }', stack, 'probe') !== null"
    )
    quick.wait_idle()
    probe = quick.get("probeCover")
    ph = next(c for c in probe.childItems() if c.property("gaveUp") is not None)
    assert ph.property("loading") is True
    ph.setProperty("gaveUp", True)
    quick.wait_idle()
    assert ph.property("loading") is False
    assert ph.opacity() == 1
    probe.deleteLater()
