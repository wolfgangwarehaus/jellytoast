"""Starter smart-playlist rule sets.

Four ready-made rule sets the smart-playlist editor offers as
one-click starters. Each preset validates against
``modules.providers.smart_rule_schema.validate_rules`` and runs
through ``modules.providers.smart_rule_eval.refine_items``.

The v1 schema (see ``smart_rule_schema.py``) does not carry
``date_added`` or ``last_played`` fields. Two of the presets here
fall back to schema-supported proxies and call that out in their
user-facing ``friendly`` text. Swap to the real fields once v2 lands.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple


Preset = Tuple[str, str, str, Dict[str, Any]]


def _current_year() -> int:
    return datetime.now().year


def _recently_added() -> Dict[str, Any]:
    # TODO: swap to date_added field when v2 schema lands.
    return {
        "match": "all",
        "rules": [
            {"field": "year", "op": "greater_than", "value": _current_year() - 2},
        ],
        "limit": 100,
        "sort": "year",
        "sort_desc": True,
    }


def _forgotten_favorites() -> Dict[str, Any]:
    # TODO: add a last_played rule when v2 schema lands.
    return {
        "match": "all",
        "rules": [
            {"field": "play_count", "op": "greater_than", "value": 5},
        ],
        "limit": 50,
        "sort": "play_count",
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


PRESETS: List[Preset] = [
    (
        "Recently added",
        "Newest releases by tag year (proxy for date_added, which v1 doesn't store).",
        "Newest releases from the last couple of years.",
        _recently_added(),
    ),
    (
        "Forgotten favorites",
        "Tracks with more than 5 plays (no last_played field in v1, so recency isn't filtered).",
        "Tracks you've played more than five times.",
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
