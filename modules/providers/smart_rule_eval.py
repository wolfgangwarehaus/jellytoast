"""Smart-playlist Python refinement layer.

The provider abstraction pushes as much of a smart-playlist rule set
as it can into a single server call (Subsonic ``getSongsByGenre`` /
``getAlbumList2``, Jellyfin ``/Items`` with ``Genres=``/``Years=``
filters). Whatever the server can't filter — ``play_count`` on
Subsonic, ``contains`` operators, ``not_equals``, the date operators
(``in_the_last`` / ``before`` / ``after``), multi-rule ``any``
unions — runs through this module as a pure Python pass over the
already-fetched item list.

The functions here operate on the adapted (Jellyfin-shape) item
dicts both providers emit (PascalCase ``Id`` / ``Name`` /
``ProductionYear`` / ``Genres`` / ``UserData`` / ``DateCreated`` /
etc.) so the same refinement code applies regardless of backend.

Public surface::

    refine_items(items, rules)          — full pipeline: filter + sort + limit
    matches_rule(item, rule)            — single-rule predicate
    sort_items(items, sort, sort_desc)  — stable sort helper

See ``modules.providers.smart_rule_schema`` for the rule schema and
field catalogue. The schema is the contract — any (field, op) pair
the validator accepts must be evaluable here.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from modules.providers.smart_rule_schema import parse_iso_date

# ── Field extractor ──────────────────────────────────────────────────
#
# Maps a schema field name onto the adapted-item value used for
# comparisons. Each entry returns either a scalar (str / int / None)
# or a list (for genre / artist where one item can have several).


def _field_value(item: Dict[str, Any], field: str) -> Any:
    """Pull the comparable value for ``field`` out of an adapted item.

    Returns None for fields the item doesn't carry; comparison
    operators below treat None as a non-match.
    """
    if field == "genre":
        return item.get("Genres") or []
    if field == "artist":
        # Both AlbumArtist and Artists matter for "equals" / "contains";
        # collect every non-empty string into a list and let the
        # operator decide how to compare.
        names: List[str] = []
        aa = item.get("AlbumArtist")
        if aa:
            names.append(aa)
        for n in item.get("Artists") or []:
            if n and n not in names:
                names.append(n)
        return names
    if field == "album":
        return item.get("Album") or ""
    if field == "year":
        return item.get("ProductionYear")
    if field == "play_count":
        return int((item.get("UserData") or {}).get("PlayCount") or 0)
    if field == "rating":
        # Jellyfin exposes CommunityRating as a float; Subsonic has no
        # numeric rating so adapted items leave it None. Treat None as
        # zero so "greater_than 0" reliably excludes unrated tracks.
        cr = item.get("CommunityRating")
        if cr is None:
            return 0
        try:
            return float(cr)
        except (TypeError, ValueError):
            return 0
    if field == "is_favorite":
        # Both providers carry UserData.IsFavorite (Jellyfin native,
        # Subsonic derived from the `starred` flag). A missing key /
        # missing UserData reads as False — an untagged item simply
        # isn't a favorite.
        return bool((item.get("UserData") or {}).get("IsFavorite"))
    if field == "date_added":
        # Jellyfin: top-level DateCreated (ISO 8601). Subsonic: the
        # adapted item copies the song's `created` timestamp onto
        # DateCreated. Returns a naive datetime, or None when the
        # field is absent / unparseable so a date rule simply
        # non-matches rather than crashing.
        return _parse_item_date(item.get("DateCreated"))
    if field == "last_played":
        # Jellyfin: UserData.LastPlayedDate (ISO 8601). Subsonic has
        # no per-track last-played timestamp, so adapted Subsonic
        # items carry no value here — a `last_played` rule simply
        # never matches on Subsonic (documented in the schema).
        return _parse_item_date((item.get("UserData") or {}).get("LastPlayedDate"))
    return None


def _parse_item_date(raw: Any) -> Optional[datetime]:
    """Coerce an item's raw date value into a naive ``datetime``.

    Returns None for a missing / empty / unparseable value — the date
    operators below treat None as a non-match so a track with no
    last-played date is silently excluded from a ``last_played`` rule
    instead of raising.
    """
    if raw is None or raw == "":
        return None
    try:
        return parse_iso_date(raw)
    except (ValueError, TypeError):
        return None


# ── Per-operator comparison helpers ──────────────────────────────────


def _equals(actual: Any, expected: Any) -> bool:
    """Equality with list-aware semantics for genre/artist."""
    if isinstance(actual, list):
        return expected in actual
    return actual == expected


def _contains(actual: Any, expected: Any) -> bool:
    """Case-insensitive substring match over strings or list-of-strings."""
    needle = str(expected or "").lower()
    if not needle:
        return False
    if isinstance(actual, list):
        return any(needle in str(s or "").lower() for s in actual)
    return needle in str(actual or "").lower()


def _starts_with(actual: Any, expected: Any) -> bool:
    """Case-insensitive prefix match over strings or list-of-strings.

    Client-side only — neither Jellyfin nor Subsonic exposes a
    server-side prefix filter, so this always runs in the refine pass.
    """
    needle = str(expected or "").lower()
    if not needle:
        return False
    if isinstance(actual, list):
        return any(str(s or "").lower().startswith(needle) for s in actual)
    return str(actual or "").lower().startswith(needle)


def _ends_with(actual: Any, expected: Any) -> bool:
    """Case-insensitive suffix match over strings or list-of-strings.
    Client-side only, same rationale as ``_starts_with``."""
    needle = str(expected or "").lower()
    if not needle:
        return False
    if isinstance(actual, list):
        return any(str(s or "").lower().endswith(needle) for s in actual)
    return str(actual or "").lower().endswith(needle)


def _favorite_equals(actual: Any, expected: Any) -> bool:
    """Boolean equality for the ``is_favorite`` field. ``actual`` is
    already a bool from ``_field_value``; coerce ``expected`` so a
    stray 1/0 from JSON still compares cleanly."""
    return bool(actual) == bool(expected)


def _in_the_last(actual: Any, expected: Any, *, now: Optional[datetime] = None) -> bool:
    """True iff ``actual`` (a datetime) falls within the last
    ``expected`` days counting back from ``now`` (default:
    ``datetime.now()``).

    The window is inclusive of the lower boundary — a track added
    exactly N days ago still matches. A None ``actual`` (item with no
    date) or a non-positive / non-int day count is a non-match;
    a future-dated ``actual`` also matches (it is trivially "within"
    the recent window).
    """
    if not isinstance(actual, datetime):
        return False
    if isinstance(expected, bool) or not isinstance(expected, int) or expected <= 0:
        return False
    ref = now or datetime.now()
    cutoff = ref - timedelta(days=expected)
    return actual >= cutoff


def _date_before(actual: Any, expected: Any) -> bool:
    """True iff ``actual`` (a datetime) is strictly earlier than the
    ISO date string ``expected``. None / unparseable inputs non-match."""
    if not isinstance(actual, datetime):
        return False
    try:
        bound = parse_iso_date(expected)
    except (ValueError, TypeError):
        return False
    return actual < bound


def _date_after(actual: Any, expected: Any) -> bool:
    """True iff ``actual`` (a datetime) is strictly later than the
    ISO date string ``expected``. None / unparseable inputs non-match."""
    if not isinstance(actual, datetime):
        return False
    try:
        bound = parse_iso_date(expected)
    except (ValueError, TypeError):
        return False
    return actual > bound


def _numeric(actual: Any) -> Optional[float]:
    """Coerce to float; return None on non-numeric so comparisons skip."""
    if actual is None:
        return None
    if isinstance(actual, bool):
        return None
    try:
        return float(actual)
    except (TypeError, ValueError):
        return None


# ── Single-rule predicate ────────────────────────────────────────────


def matches_rule(item: Dict[str, Any], rule: Dict[str, Any]) -> bool:
    """Return True iff ``item`` satisfies ``rule``.

    ``rule`` is one entry from a validated rule set:
    ``{"field": ..., "op": ..., "value": ...}``. Unknown ops return
    False rather than raising — the schema validator runs upstream
    so an unknown op here is a programmer error worth surfacing in
    refinement logs, not user input worth crashing on.
    """
    field = rule.get("field", "")
    op = rule.get("op", "")
    expected = rule.get("value")
    actual = _field_value(item, field)

    if op == "equals":
        if field == "is_favorite":
            return _favorite_equals(actual, expected)
        return _equals(actual, expected)
    if op == "not_equals":
        return not _equals(actual, expected)
    if op == "contains":
        return _contains(actual, expected)
    if op == "starts_with":
        return _starts_with(actual, expected)
    if op == "ends_with":
        return _ends_with(actual, expected)

    if op == "in_the_last":
        return _in_the_last(actual, expected)
    if op == "before":
        return _date_before(actual, expected)
    if op == "after":
        return _date_after(actual, expected)

    if op == "between":
        if not isinstance(expected, (list, tuple)) or len(expected) != 2:
            return False
        lo, hi = expected
        lo_f, hi_f = _numeric(lo), _numeric(hi)
        a_f = _numeric(actual)
        if lo_f is None or hi_f is None or a_f is None:
            return False
        if lo_f > hi_f:
            lo_f, hi_f = hi_f, lo_f
        return lo_f <= a_f <= hi_f

    a_f = _numeric(actual)
    e_f = _numeric(expected)
    if a_f is None or e_f is None:
        return False
    if op == "greater_than":
        return a_f > e_f
    if op == "less_than":
        return a_f < e_f
    return False


# ── Sort + limit ─────────────────────────────────────────────────────


def sort_items(
    items: List[Dict[str, Any]],
    sort: Optional[str],
    sort_desc: bool,
    *,
    rng: Optional[random.Random] = None,
) -> List[Dict[str, Any]]:
    """Stable sort by a schema field. Missing/None values sort last
    regardless of direction so a partially-tagged library doesn't push
    untagged tracks to the top of a -descending list.

    Date fields (``date_added`` / ``last_played``) sort by their
    parsed ``datetime`` — ``sort_desc=True`` puts the most recent
    first. Items with no / unparseable date sort last in either
    direction (the missing-flag is direction-aware — see below).

    ``sort == "random"`` is a special token (see
    ``smart_rule_schema.SPECIAL_SORTS``): the items are returned in a
    fresh shuffled order. ``sort_desc`` is ignored for random — there
    is no "descending shuffle". Pass ``rng`` (a ``random.Random``) for
    a reproducible order in tests; the default draws from the global
    module RNG so each evaluation reshuffles.
    """
    if not sort:
        return items

    if sort == "random":
        source = rng or random
        return source.sample(list(items), len(items))

    # The leading element of every sort key is a "missing?" flag.
    # ``sorted(reverse=...)`` flips the *whole* tuple, so to keep
    # None-bearing items last in BOTH directions the flag is inverted
    # when sorting descending — that way, after the reverse, missing
    # items still land at the bottom.
    none_flag_low, none_flag_high = (True, False) if sort_desc else (False, True)

    def _key(it: Dict[str, Any]):
        v = _field_value(it, sort)
        # Lists (genre / artist) sort by their first element.
        if isinstance(v, list):
            v = v[0] if v else None
        # The (missing?, value) pair keeps None-bearing items at the
        # end of the list regardless of sort direction.
        missing = none_flag_high if v is None else none_flag_low
        return (missing, v if v is not None else "")

    try:
        return sorted(items, key=_key, reverse=sort_desc)
    except TypeError:
        # Mixed types in the sort column (a server returning
        # ProductionYear as a string for one album, int for another).
        # Coerce to string and try again rather than crashing.
        def _str_key(it: Dict[str, Any]):
            v = _field_value(it, sort)
            if isinstance(v, list):
                v = v[0] if v else None
            missing = none_flag_high if v is None else none_flag_low
            return (missing, str(v) if v is not None else "")

        return sorted(items, key=_str_key, reverse=sort_desc)


# ── Public entry point ───────────────────────────────────────────────


def refine_items(items: List[Dict[str, Any]], rules: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Apply a full rule set to ``items`` in Python and return the
    matched + sorted + limited subset.

    ``rules`` is the schema-validated dict (see
    ``modules.providers.smart_rule_schema``). Callers fetch a
    candidate set from the server via the most selective query they
    can build, then hand the result here to enforce the parts the
    server didn't.

    Semantics:

    * Empty ``rules`` list → items pass through unfiltered.
    * ``match: "all"`` (default) → AND across rules.
    * ``match: "any"`` → OR across rules.
    * ``sort`` / ``sort_desc`` → stable sort over a schema field.
    * ``limit: 0`` → empty list; ``limit > 0`` truncates after sort;
      ``limit`` absent / None → no cap.
    """
    raw_rules = rules.get("rules") or []
    match = rules.get("match", "all")

    if raw_rules:
        if match == "any":
            filtered = [it for it in items if any(matches_rule(it, r) for r in raw_rules)]
        else:
            filtered = [it for it in items if all(matches_rule(it, r) for r in raw_rules)]
    else:
        # Empty rule list — engines treat this as "no filtering" so
        # the sort + limit pipeline still runs on the input set.
        filtered = list(items)

    filtered = sort_items(
        filtered,
        rules.get("sort"),
        bool(rules.get("sort_desc", False)),
    )

    limit = rules.get("limit")
    if isinstance(limit, int) and not isinstance(limit, bool):
        if limit == 0:
            return []
        if limit > 0:
            filtered = filtered[:limit]

    return filtered
