"""Smart-playlist starter presets package."""

from __future__ import annotations

from modules.smart_playlists.play import play_entry
from modules.smart_playlists.presets import (
    PRESETS,
    YEAR_PRESET_NAME,
    from_album,
    from_artist,
    from_genre,
    from_track,
    from_year,
    get_preset,
    make_year_preset,
)

__all__ = [
    "PRESETS",
    "YEAR_PRESET_NAME",
    "from_album",
    "from_artist",
    "from_genre",
    "from_track",
    "from_year",
    "get_preset",
    "make_year_preset",
    "play_entry",
]
