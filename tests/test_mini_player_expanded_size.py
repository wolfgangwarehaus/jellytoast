"""Expanded (tall-album-art) mini player open-size contract.

2026-06-10: opening the expanded view restores the user's last dragged
width from the persisted ``ui/mini_player_expanded_width`` setting; a
profile with NO persisted size opens at the smallest size
(``EXPANDED_MIN_WIDTH`` — also the setting's default, so absent-key and
never-resized land in the same place). This reverses the earlier
always-reset-to-smallest rule, which ignored the setting on purpose.
Out-of-range persisted values clamp into [MIN, MAX] so a stale or
hand-edited config can't open a giant or sub-minimum player.
"""

from __future__ import annotations

import pytest

from jellytoast.mini_player import FloatingMiniPlayer
from jellytoast.settings import get_settings

_KEYS = (
    "ui/mini_player_expanded_width",
    "ui/mini_player_geometry",
    "ui/mini_player_mode",
)


@pytest.fixture
def width_setting(qapp):
    """Snapshot + restore the persisted mini-player keys around each test."""
    s = get_settings()
    before = {k: s._s.value(k) for k in _KEYS}
    yield s
    for k, v in before.items():
        if v is None:
            s._s.remove(k)
        else:
            s._s.setValue(k, v)


def _seeded_width(mp: FloatingMiniPlayer) -> int:
    try:
        return mp._last_expanded_width
    finally:
        mp.deleteLater()


def test_no_persisted_size_opens_smallest(width_setting):
    width_setting._s.remove("ui/mini_player_expanded_width")
    assert _seeded_width(FloatingMiniPlayer()) == FloatingMiniPlayer.EXPANDED_MIN_WIDTH


def test_persisted_size_is_restored(width_setting):
    width_setting.mini_player_expanded_width = 450
    assert _seeded_width(FloatingMiniPlayer()) == 450


def test_oversize_persisted_value_clamps_to_max(width_setting):
    width_setting.mini_player_expanded_width = 9999
    assert _seeded_width(FloatingMiniPlayer()) == FloatingMiniPlayer.EXPANDED_MAX_WIDTH


def test_undersize_persisted_value_clamps_to_min(width_setting):
    width_setting.mini_player_expanded_width = 10
    assert _seeded_width(FloatingMiniPlayer()) == FloatingMiniPlayer.EXPANDED_MIN_WIDTH


def test_stale_geometry_blob_does_not_clobber_persisted_width(width_setting, qapp):
    """A geometry blob whose size disagrees with the persisted width
    (e.g. saved by a session predating the dedicated width setting) must
    lose: restoreGeometry fires resizeEvent, which used to overwrite
    _last_expanded_width with the blob's width BEFORE the snap-back
    correction read it — making the correction a no-op and persisting
    the stale width on close (2026-06-11 post-audit finding)."""
    from PySide6.QtWidgets import QWidget

    donor = QWidget()
    donor.resize(400, 400 + FloatingMiniPlayer.EXPANDED_BOTTOM_DELTA)
    blob = bytes(donor.saveGeometry())
    donor.deleteLater()

    width_setting.mini_player_expanded_width = 500
    width_setting.mini_player_geometry = blob
    width_setting.mini_player_mode = "expanded"

    mp = FloatingMiniPlayer()
    try:
        assert mp._last_expanded_width == 500
        assert mp.width() == 500
    finally:
        mp.deleteLater()
