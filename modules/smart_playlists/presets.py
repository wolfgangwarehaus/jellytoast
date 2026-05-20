"""Starter smart-playlist rule sets.

Four ready-made rule sets the smart-playlist editor offers as
one-click starters. Each preset validates against
``modules.providers.smart_rule_schema.validate_rules`` and runs
through ``modules.providers.smart_rule_eval.refine_items``.

As of schema v2 the rule catalogue carries real ``date_added`` and
``last_played`` date fields (see ``smart_rule_schema.py``), so the
"Recently added" and "Forgotten favorites" presets filter on actual
dates rather than ``year`` / ``play_count`` proxies.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple


Preset = Tuple[str, str, str, Dict[str, Any]]


def _current_year() -> int:
    return datetime.now().year


def _days_ago_iso(days: int) -> str:
    """ISO 8601 date (``YYYY-MM-DD``) for ``days`` ago from today.

    Used by the ``last_played`` preset so its ``before`` bound is a
    concrete absolute date the schema validator accepts. The value is
    computed when the preset is built — a preset created today and
    re-built tomorrow will carry a one-day-newer bound, which is the
    intended "rolling window" behaviour for a starter."""
    return (datetime.now() - timedelta(days=days)).date().isoformat()


def _recently_added() -> Dict[str, Any]:
    # Real date_added filter (schema v2): tracks that entered the
    # library within the last 60 days, newest first.
    return {
        "match": "all",
        "rules": [
            {"field": "date_added", "op": "in_the_last", "value": 60},
        ],
        "limit": 100,
        "sort": "date_added",
        "sort_desc": True,
    }


def _forgotten_favorites() -> Dict[str, Any]:
    # Real last_played filter (schema v2): tracks played more than
    # five times but not in the last 90 days — the ones you loved and
    # then forgot. Sorted least-recently-played first so the most
    # neglected favorites surface at the top.
    return {
        "match": "all",
        "rules": [
            {"field": "play_count", "op": "greater_than", "value": 5},
            {"field": "last_played", "op": "before", "value": _days_ago_iso(90)},
        ],
        "limit": 50,
        "sort": "last_played",
        "sort_desc": False,
    }


def _top_played() -> Dict[str, Any]:
    return {
        "match": "all",
        "rules": [
            {"field": "play_count", "op": "greater_than", "value": 0},
        ],
        "limit": 100,
        "sort": "play_count",
        "sort_desc": True,
    }


def make_year_preset(year: int) -> Dict[str, Any]:
    return {
        "match": "all",
        "rules": [
            {"field": "year", "op": "equals", "value": int(year)},
        ],
        "limit": None,
        "sort": "artist",
        "sort_desc": False,
    }


YEAR_PRESET_NAME = "Year (custom)"


# ── "Create from this X" recipe factories ────────────────────────────
#
# These power the right-click "Create smart playlist from this
# artist / album / genre" affordances. Each returns a rules dict the
# editor consumes via its ``preset_rules`` path; all four round-trip
# cleanly through ``smart_rule_schema.validate_rules``.


def from_artist(artist_name: str) -> Dict[str, Any]:
    """More by this artist — every track whose artist equals
    ``artist_name``, ordered by play count (most-played first), capped
    at 50 tracks.

    ``artist equals`` has no server-side mapping on either backend, so
    the rule refines client-side; the play_count sort and 50-item cap
    apply in the Python refine layer.
    """
    return {
        "match": "all",
        "rules": [
            {"field": "artist", "op": "equals", "value": str(artist_name)},
        ],
        "limit": 50,
        "sort": "play_count",
        "sort_desc": True,
    }


def from_album(album_name: str) -> Dict[str, Any]:
    """Tracks from this album — every track whose album equals
    ``album_name``, ordered by year ascending, with no cap (albums are
    small enough that a limit just gets in the way)."""
    return {
        "match": "all",
        "rules": [
            {"field": "album", "op": "equals", "value": str(album_name)},
        ],
        "limit": None,
        "sort": "year",
        "sort_desc": False,
    }


def from_genre(genre_name: str) -> Dict[str, Any]:
    """Tracks tagged with this genre — every track whose genre equals
    ``genre_name``, ordered top-played first, capped at 100.

    ``genre equals`` maps to a native server query on both backends
    (Jellyfin ``Genres=``, Subsonic ``getSongsByGenre``); the sort +
    cap apply in the Python refine layer.
    """
    return {
        "match": "all",
        "rules": [
            {"field": "genre", "op": "equals", "value": str(genre_name)},
        ],
        "limit": 100,
        "sort": "play_count",
        "sort_desc": True,
    }


def from_year(year: int) -> Dict[str, Any]:
    """Tracks from a specific year — alias of :func:`make_year_preset`
    so the four "create from this X" factories share one naming
    convention. Identical output."""
    return make_year_preset(year)


PRESETS: List[Preset] = [
    (
        "Recently added",
        "Tracks added to the library in the last 60 days, newest first.",
        "Tracks added to your library in the last couple of months.",
        _recently_added(),
    ),
    (
        "Forgotten favorites",
        "Tracks played more than 5 times but not in the last 90 days.",
        "Tracks you used to love but haven't played in a while.",
        _forgotten_favorites(),
    ),
    (
        "Top played",
        "All played tracks, ordered by play count.",
        "Your most-played tracks.",
        _top_played(),
    ),
    (
        YEAR_PRESET_NAME,
        "Factory preset — UI calls make_year_preset(year) to fill the value.",
        "Everything from a specific year.",
        {},
    ),
]


def get_preset(name: str) -> Optional[Dict[str, Any]]:
    for preset_name, _description, _friendly, rules in PRESETS:
        if preset_name == name:
            return dict(rules)
    return None
