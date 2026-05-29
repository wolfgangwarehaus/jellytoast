"""Starter smart-playlist rule sets.

Three ready-made rule sets the smart-playlist editor offers as
one-click starters. Each preset validates against
``modules.providers.smart_rule_schema.validate_rules`` and runs
through ``modules.providers.smart_rule_eval.refine_items``.

As of schema v2 the rule catalogue carries a real ``date_added``
date field (see ``smart_rule_schema.py``), so the "Recently added"
preset filters on actual dates rather than a ``year`` proxy.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

Preset = Tuple[str, str, str, Dict[str, Any]]


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
# artist / album / genre / track" affordances. Each returns a rules
# dict the editor consumes via its ``preset_rules`` path; all four
# round-trip cleanly through ``smart_rule_schema.validate_rules``.
#
# Design principle (post-rework 2026-05-28): a seeded playlist must
# either EXCLUDE the seed or RE-RANK by a signal the seed doesn't
# trivially satisfy. The old "field=name" recipes failed both — an
# "album=Homogenic" playlist just IS the album, which is already a
# click away on the album page. The new recipes are non-redundant
# explorations seeded from the context the user right-clicked.


_VIBE_YEAR_WINDOW = 3   # ± years for "around this album / track" recipes
_DEEP_CUTS_PLAYS = 3    # play_count threshold for "Deep Cuts"
_DISCOVERY_DAYS = 90    # date_added window for "Discoveries"


def from_artist(artist_name: str) -> Dict[str, Any]:
    """Deep Cuts from this artist — tracks by ``artist_name`` that
    you've barely played (play_count < 3), shuffled. Surfaces the
    "I own this but never heard it" catalog from artists you already
    care about. Non-redundant with the artist page (which shows
    everything by them in album order)."""
    return {
        "match": "all",
        "rules": [
            {"field": "artist", "op": "equals", "value": str(artist_name)},
            {"field": "play_count", "op": "less_than", "value": _DEEP_CUTS_PLAYS},
        ],
        "limit": 50,
        "sort": "random",
        "sort_desc": False,
    }


def from_album(album: Any) -> Dict[str, Any]:
    """More like this album — tracks in the same genre + era ( ± 3 yrs )
    as the seed album, EXCLUDING the album itself. Captures the
    vibe / era around an album rather than the album's own tracks
    (which are one click away on the album page).

    ``album`` accepts either the item dict (preferred — has Genres +
    ProductionYear) or a plain name string (legacy fallback —
    degrades to just album-name exclusion). Missing genre or year
    metadata gracefully drops the corresponding rule rather than
    failing; the user can add it back in the editor."""
    if isinstance(album, dict):
        name = str(album.get("Name") or album.get("Album") or "")
        genres = album.get("Genres") or []
        year = album.get("ProductionYear")
    else:
        name = str(album)
        genres = []
        year = None

    rules: List[Dict[str, Any]] = []
    primary_genre = genres[0] if genres else None
    if primary_genre:
        rules.append({"field": "genre", "op": "equals", "value": primary_genre})
    if isinstance(year, int) and year > 0:
        rules.append({
            "field": "year",
            "op": "between",
            "value": [year - _VIBE_YEAR_WINDOW, year + _VIBE_YEAR_WINDOW],
        })
    if name:
        rules.append({"field": "album", "op": "not_equals", "value": name})

    return {
        "match": "all",
        "rules": rules,
        "limit": 50,
        "sort": "random",
        "sort_desc": False,
    }


def from_genre(genre_name: str) -> Dict[str, Any]:
    """{Genre} Discoveries — tracks tagged with ``genre_name`` that
    you've added recently (last 90 days) and never played. The
    "I added this and forgot" pile, scoped to one genre. Non-redundant
    with the genre tile (which shows everything in the genre, sorted
    however you like)."""
    return {
        "match": "all",
        "rules": [
            {"field": "genre", "op": "equals", "value": str(genre_name)},
            {"field": "play_count", "op": "equals", "value": 0},
            {"field": "date_added", "op": "in_the_last", "value": _DISCOVERY_DAYS},
        ],
        "limit": 50,
        "sort": "date_added",
        "sort_desc": True,
    }


def from_track(track: Any) -> Dict[str, Any]:
    """More like this track — tracks in the same genre + era as the
    seed track. Tighter year window than from_album feels right since
    a single track has its own moment. ``track`` accepts the item
    dict (preferred — has Genres + ProductionYear via the album
    pull-through) or a plain name string (degrades to empty rules
    that the user fills in manually)."""
    if isinstance(track, dict):
        genres = track.get("Genres") or []
        year = track.get("ProductionYear")
    else:
        genres = []
        year = None

    rules: List[Dict[str, Any]] = []
    primary_genre = genres[0] if genres else None
    if primary_genre:
        rules.append({"field": "genre", "op": "equals", "value": primary_genre})
    if isinstance(year, int) and year > 0:
        rules.append({
            "field": "year",
            "op": "between",
            "value": [year - _VIBE_YEAR_WINDOW, year + _VIBE_YEAR_WINDOW],
        })

    return {
        "match": "all",
        "rules": rules,
        "limit": 30,
        "sort": "random",
        "sort_desc": False,
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
