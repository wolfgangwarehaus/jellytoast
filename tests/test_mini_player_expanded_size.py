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


@pytest.fixture
def width_setting(qapp):
    """Snapshot + restore the persisted expanded width around each test."""
    s = get_settings()
    before = s._s.value("ui/mini_player_expanded_width")
    yield s
    if before is None:
        s._s.remove("ui/mini_player_expanded_width")
    else:
        s._s.setValue("ui/mini_player_expanded_width", before)


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
