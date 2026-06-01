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


def test_toggle_adds_then_removes(qapp):
    bar = _bar(qapp)
    bar.set_available_libraries(
        [{"Id": "m", "Name": "Music"}, {"Id": "d", "Name": "Discover"}]
    )
    bar.set_selected_libraries([])
    emitted = []
    bar.libraries_selected.connect(lambda ids: emitted.append(list(ids)))

    bar._on_library_toggled("d")  # select Discover
    assert emitted[-1] == ["d"]
    bar._on_library_toggled("m")  # add Music
    assert emitted[-1] == ["d", "m"]
    bar._on_library_toggled("d")  # remove Discover
    assert emitted[-1] == ["m"]


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
