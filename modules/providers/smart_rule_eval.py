"""Smart-playlist Python refinement layer.

The provider abstraction pushes as much of a smart-playlist rule set
as it can into a single server call (Subsonic ``getSongsByGenre`` /
``getAlbumList2``, Jellyfin ``/Items`` with ``Genres=``/``Years=``
filters). Whatever the server can't filter — ``play_count`` on
Subsonic, ``contains`` operators, ``not_equals``, multi-rule ``any``
unions — runs through this module as a pure Python pass over the
already-fetched item list.

The functions here operate on the adapted (Jellyfin-shape) item
dicts both providers emit (PascalCase ``Id`` / ``Name`` /
``ProductionYear`` / ``Genres`` / ``UserData`` / etc.) so the same
refinement code applies regardless of backend.

Public surface::

    refine_items(items, rules)          — full pipeline: filter + sort + limit
    matches_rule(item, rule)            — single-rule predicate
    sort_items(items, sort, sort_desc)  — stable sort helper

See ``modules.providers.smart_rule_schema`` for the rule schema and
field catalogue. The schema is the contract — any (field, op) pair
the validator accepts must be evaluable here.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


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
        return _equals(actual, expected)
    if op == "not_equals":
        return not _equals(actual, expected)
    if op == "contains":
        return _contains(actual, expected)

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
    items: List[Dict[str, Any]], sort: Optional[str], sort_desc: bool
) -> List[Dict[str, Any]]:
    """Stable sort by a schema field. Missing/None values sort last
    regardless of direction so a partially-tagged library doesn't push
    untagged tracks to the top of a -descending list."""
    if not sort:
        return items

    def _key(it: Dict[str, Any]):
        v = _field_value(it, sort)
        # Lists (genre / artist) sort by their first element.
        if isinstance(v, list):
            v = v[0] if v else None
        # None always sorts last — the (is_none, value) pair makes
        # None-bearing items the bigger of every comparison.
        return (v is None, v if v is not None else "")

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
            return (v is None, str(v) if v is not None else "")

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
