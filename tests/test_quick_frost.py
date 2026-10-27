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
