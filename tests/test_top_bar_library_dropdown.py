"""Top-bar multi-library dropdown — visibility gating, title routing, and
the toggle → ``libraries_selected`` emission logic.

Widget-level (needs ``qapp``) but headless: we drive the public setters and
the toggle handler directly rather than popping a real QMenu, so there's no
event-loop / Wayland dependency.
"""

from modules.top_bar import JtTopBar


def _bar(qapp):
    return JtTopBar()


def test_single_library_keeps_plain_label(qapp):
    bar = _bar(qapp)
    bar.set_title("Music")
    bar.set_available_libraries([{"Id": "m", "Name": "Music"}])
    # 0–1 libraries → plain label, no dropdown chevron.
    assert not bar.title_label.isHidden()
    assert bar.library_btn.isHidden()


def test_multiple_libraries_swaps_to_dropdown(qapp):
    bar = _bar(qapp)
    bar.set_title("Music")
    bar.set_available_libraries(
        [{"Id": "m", "Name": "Music"}, {"Id": "d", "Name": "Discover"}]
    )
    assert not bar.library_btn.isHidden()
    assert bar.title_label.isHidden()
    # Title text is preserved across the swap and reflected on the button.
    assert bar.library_btn.text() == "Music"


def test_set_title_drives_both_widgets(qapp):
    bar = _bar(qapp)
    bar.set_available_libraries(
        [{"Id": "m", "Name": "Music"}, {"Id": "d", "Name": "Discover"}]
    )
    bar.set_title("Discover")
    assert bar.library_btn.text() == "Discover"
    assert bar.title_label.text() == "Discover"


def test_toggle_from_all_drops_a_library(qapp):
    """From the 'all' state (empty = every library effectively ON),
    toggling a library turns it OFF — yielding 'all except that one' —
    rather than paradoxically selecting just it. The 2026-06-02 fix:
    makes "show only Music" reachable in ONE click (drop Discover)
    instead of the old "had to toggle a few times" dance."""
    bar = _bar(qapp)
    bar.set_available_libraries(
        [{"Id": "m", "Name": "Music"}, {"Id": "d", "Name": "Discover"}]
    )
    bar.set_selected_libraries([])  # all
    emitted = []
    bar.libraries_selected.connect(lambda ids: emitted.append(list(ids)))

    bar._on_library_toggled("d")  # drop Discover → just Music
    assert emitted[-1] == ["m"]
    # Re-toggling Discover adds it back (→ the explicit pair; the host
    # then re-collapses 'every library' to '' = all).
    bar._on_library_toggled("d")
    assert emitted[-1] == ["m", "d"]


def test_toggle_off_last_explicit_library_returns_to_all(qapp):
    bar = _bar(qapp)
    bar.set_available_libraries(
        [{"Id": "m", "Name": "Music"}, {"Id": "d", "Name": "Discover"}]
    )
    bar.set_selected_libraries(["m"])  # explicit single
    emitted = []
    bar.libraries_selected.connect(lambda ids: emitted.append(list(ids)))

    bar._on_library_toggled("m")  # remove the only one → empty → 'all'
    assert emitted[-1] == []


def test_all_row_clears_selection(qapp):
    bar = _bar(qapp)
    bar.set_available_libraries(
        [{"Id": "m", "Name": "Music"}, {"Id": "d", "Name": "Discover"}]
    )
    bar.set_selected_libraries(["d"])
    emitted = []
    bar.libraries_selected.connect(lambda ids: emitted.append(list(ids)))

    bar._on_library_toggled(None)  # the "All libraries" reset row
    assert emitted[-1] == []


def test_reseed_libraries_is_idempotent(qapp):
    bar = _bar(qapp)
    bar.set_title("Music")
    bar.set_available_libraries(
        [{"Id": "m", "Name": "Music"}, {"Id": "d", "Name": "Discover"}]
    )
    # A second server with a single library reverts to the plain label.
    bar.set_available_libraries([{"Id": "x", "Name": "Solo"}])
    assert not bar.title_label.isHidden()
    assert bar.library_btn.isHidden()


def test_stay_open_menu_keeps_open_on_checkable_click(qapp):
    # The keystone interaction: clicking a checkable row must NOT dismiss the
    # menu, so the user can flip several libraries in one visit. A plain
    # QMenu closes on the first click — this is why the picker needs
    # _StayOpenMenu (the previous "checkable triggers don't dismiss" comment
    # was false). Drives a REAL popped menu + click (the unit toggle tests
    # can't catch this because they never pop the menu).
    from PySide6.QtCore import QPoint
    from PySide6.QtCore import Qt as _Qt
    from PySide6.QtGui import QAction
    from PySide6.QtTest import QTest

    from modules.top_bar import _StayOpenMenu

    menu = _StayOpenMenu()
    act = QAction("Music", menu)
    act.setCheckable(True)
    fired = []
    act.triggered.connect(lambda *_: fired.append(True))
    menu.addAction(act)

    menu.popup(QPoint(0, 0))
    QTest.qWaitForWindowExposed(menu)
    QTest.mouseClick(
        menu,
        _Qt.MouseButton.LeftButton,
        _Qt.KeyboardModifier.NoModifier,
        menu.actionGeometry(act).center(),
    )

    assert fired == [True]  # the toggle fired
    assert act.isChecked()  # check state flipped
    assert menu.isVisible()  # ...and the menu stayed open (the fix)
    menu.close()


def test_set_selected_libraries_resyncs_open_menu(qapp):
    # When the host normalizes a selection (collapsing 'every library' → ''
    # = all) and pushes it back, an OPEN menu must re-sync its checkmarks, or
    # they'd contradict the title and the next toggle would start from a
    # stale model.
    from PySide6.QtCore import QPoint
    from PySide6.QtGui import QAction

    from modules.top_bar import _StayOpenMenu

    bar = _bar(qapp)
    bar.set_available_libraries(
        [{"Id": "m", "Name": "Music"}, {"Id": "d", "Name": "Discover"}]
    )
    # Stand up an open menu the way _show_library_menu does (its exec()
    # blocks, so we wire the refs directly + popup non-blocking).
    menu = _StayOpenMenu(bar)
    all_act = QAction("All libraries", menu)
    all_act.setCheckable(True)
    m_act = QAction("Music", menu)
    m_act.setCheckable(True)
    d_act = QAction("Discover", menu)
    d_act.setCheckable(True)
    for a in (all_act, m_act, d_act):
        menu.addAction(a)
    bar._all_action = all_act
    bar._lib_actions = {"m": m_act, "d": d_act}
    bar._lib_menu = menu
    menu.popup(QPoint(0, 0))

    # 'both selected' collapses to all (empty). All-mode shows the reset
    # row checked AND every library checked (they're all effectively ON).
    bar.set_selected_libraries([])
    assert all_act.isChecked()
    assert m_act.isChecked() and d_act.isChecked()

    # A single-library selection → All unchecked, that one checked.
    bar.set_selected_libraries(["d"])
    assert not all_act.isChecked()
    assert d_act.isChecked() and not m_act.isChecked()
    menu.close()


def test_set_selected_libraries_noop_when_no_menu(qapp):
    # No menu open → just updates the stored list (no crash on the refresh).
    bar = _bar(qapp)
    bar.set_available_libraries(
        [{"Id": "m", "Name": "Music"}, {"Id": "d", "Name": "Discover"}]
    )
    bar.set_selected_libraries(["d"])
    assert bar._selected_library_ids == ["d"]


def test_view_dropdown_pinned_to_bar_centre(qapp):
    # The view dropdown ("Albums" / "Radio" / …) is absolutely positioned so
    # its centre lands on the bar's geometric centre (the library controls ride
    # to its right). It stays centred whether or not the controls show and
    # doesn't bounce as the page-name width changes.
    from PySide6.QtCore import QPoint

    bar = _bar(qapp)
    bar.view_btn.setText("Radio")
    bar.view_btn.show()
    bar.resize(1400, 48)
    bar.show()
    qapp.processEvents()

    def dropdown_offset() -> int:
        qapp.processEvents()
        vb = bar.view_btn
        cx = vb.mapTo(bar, QPoint(vb.width() // 2, vb.height() // 2)).x()
        return cx - bar.width() // 2

    # Controls hidden → dropdown centred.
    bar.set_library_controls_visible(False)
    assert abs(dropdown_offset()) <= 2

    # Controls shown (they ride to the dropdown's right) → still centred.
    bar.set_library_controls_visible(True)
    assert abs(dropdown_offset()) <= 2

    # A longer page name doesn't shift the centre.
    bar.set_library_controls_visible(False)
    bar.view_btn.setText("Smart playlists")
    assert abs(dropdown_offset()) <= 2


def test_view_dropdown_clamped_and_library_not_clipped_when_narrow(qapp):
    # At a narrow width the centre cluster yields rather than overlapping the
    # left cluster, and the library dropdown still shows its FULL text — the
    # old equal-thirds layout squeezed it and clipped "Music Library".
    bar = _bar(qapp)
    bar.set_available_libraries(
        [{"Id": "m", "Name": "Music"}, {"Id": "d", "Name": "Discover"}]
    )
    bar.set_title("Music Library")
    bar.view_btn.setText("Albums")
    bar.view_btn.show()
    bar.set_library_controls_visible(True)
    bar.resize(760, 48)
    bar.show()
    qapp.processEvents()

    # Library dropdown shows its full text (width is at least what it needs).
    assert bar.library_btn.width() >= bar.library_btn.sizeHint().width() - 1

    # Centre cluster never overlaps the left cluster.
    assert bar._center_cluster.geometry().left() >= bar._left_col.geometry().right()


def _item_left_pad(qss: str) -> int:
    """Pull the QMenu::item left padding (the 4th value) out of the QSS."""
    import re

    m = re.search(r"padding:\s*\d+px\s+\d+px\s+\d+px\s+(\d+)px", qss)
    assert m, qss
    return int(m.group(1))


class TestDropdownMenuChrome:
    """The three top-bar dropdowns (view / sort / library) share one QSS
    source so they can't drift, and the *checkable* ones (sort + library)
    inset the native ✓ column off the rounded corner it was hugging — the
    'crowded' library picker (reported by eye 2026-06-08)."""

    def test_one_shared_qss_source(self, qapp):
        # The same helper backs every dropdown — same background/selection so
        # they read identical. (The library picker used to carry its own copy.)
        bar = _bar(qapp)
        plain = bar._dropdown_menu_qss()
        checkable = bar._dropdown_menu_qss(checkable=True)
        for qss in (plain, checkable):
            assert "QMenu" in qss
            assert "border-radius: 8px" in qss

    def test_checkable_menus_inset_the_check_column(self, qapp):
        # Checkable menus get MORE left padding than plain ones so the ✓
        # clears the corner instead of hugging it.
        bar = _bar(qapp)
        plain_left = _item_left_pad(bar._dropdown_menu_qss())
        check_left = _item_left_pad(bar._dropdown_menu_qss(checkable=True))
        assert check_left > plain_left
        # And the checkable indent is comfortably off the edge (was 14px).
        assert check_left >= 20

    def test_dropdowns_frost_when_blur_active(self, qapp, monkeypatch):
        # The dropdown background must FROST (capped alpha, blur lifts through)
        # over verified compositor blur — not paint the raw near-white opaque
        # token. This is what made the LIGHT dropdowns match the dark ones
        # instead of reading stark white (reported by eye 2026-06-08).
        import modules.ui_helpers as u

        bar = _bar(qapp)
        monkeypatch.setattr(u, "popup_blur_active", lambda: True)
        monkeypatch.setattr(u, "POPUP_OPAQUE_FILL", "rgba(248, 248, 248, 0.80)")
        qss = bar._dropdown_menu_qss()
        assert "0.62" in qss  # capped frost alpha (lets blur through)
        assert "0.80" not in qss  # NOT the raw near-white opaque token

    def test_dropdowns_stay_opaque_without_blur(self, qapp, monkeypatch):
        # No verified blur (solid theme, or a box where blur didn't land) →
        # keep the opaque token so the menu stays legible, never see-through.
        import modules.ui_helpers as u

        bar = _bar(qapp)
        monkeypatch.setattr(u, "popup_blur_active", lambda: False)
        monkeypatch.setattr(u, "POPUP_OPAQUE_FILL", "rgba(248, 248, 248, 0.80)")
        assert "0.80" in bar._dropdown_menu_qss()

    def test_library_menu_centred_under_button(self, qapp):
        # The picker pops centred under its button now (was bottom-left
        # anchored, which read off-centre beside the centred view menu).
        from PySide6.QtCore import QPoint
        from PySide6.QtWidgets import QMenu

        bar = _bar(qapp)
        bar.set_available_libraries(
            [{"Id": "m", "Name": "Music"}, {"Id": "d", "Name": "Discover"}]
        )
        bar.resize(1400, 48)
        bar.show()
        qapp.processEvents()

        menu = QMenu(bar)
        menu.addAction("All libraries")
        menu.addAction("Music Library")
        pt = bar._popup_menu_centered(menu, bar.library_btn)

        btn_tl = bar.library_btn.mapToGlobal(QPoint(0, 0))
        menu_w = max(menu.sizeHint().width(), bar.library_btn.width())
        # Menu centre sits on the button centre, and it drops below the button.
        assert abs((pt.x() + menu_w // 2) - (btn_tl.x() + bar.library_btn.width() // 2)) <= 1
        assert pt.y() == btn_tl.y() + bar.library_btn.height()
        # A short menu is widened to at least the button width.
        assert menu.minimumWidth() >= bar.library_btn.width()
