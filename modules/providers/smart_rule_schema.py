"""Smart-playlist rule schema — the canonical jellytoast format.

Smart playlists are rule sets stored client-side (see
``docs/research/smart_playlists.md``) and evaluated by translating the
rule set into a single provider call plus a Python refine pass. This
module owns the **shape** of those rule sets: the validator, the field
catalogue, and the per-field operator whitelist.

The shape is intentionally smaller than Navidrome's `.nsp` format —
the schema supports a flat list of conditions with a single top-level
``match`` toggle (AND vs OR). Nested any/all groups remain a
follow-up; the date operators (``in_the_last`` / ``before`` /
``after``) and the date fields below landed in schema **v2**.

Schema::

    {
        "match": "all" | "any",     # AND across rules, OR across rules
        "rules": [                  # list of conditions
            {"field": <str>, "op": <str>, "value": <any>},
            ...
        ],
        "limit": <int> | None,      # max items returned (None = no cap)
        "sort": <str> | None,       # field to sort by (None = default)
        "sort_desc": <bool>,        # descending order
    }

The provider's ``query_items`` method takes this dict and returns the
matched items as a list of provider-native item dicts. Providers
translate as much of the rule set as they can to a server call and do
the remaining filtering / sorting in Python.

Field catalogue::

    genre       (str)   ops: equals, not_equals
    artist      (str)   ops: equals, contains
    album       (str)   ops: equals, contains
    year        (int)   ops: equals, greater_than, less_than, between
    play_count  (int)   ops: greater_than, less_than, equals
                        — Jellyfin native via MinUserPlayCount;
                          Subsonic does not expose play_count as a
                          server-side filter, so it's skipped or done
                          client-side in the SubsonicProvider stub.
    rating      (int)   ops: greater_than, less_than, equals
                        — Jellyfin native via MinCommunityRating;
                          Subsonic exposes only the binary "starred"
                          flag (toggle_favorite), so numeric rating
                          comparisons aren't supported there in v1.
    is_favorite (bool)  ops: equals
                        — Jellyfin native via IsFavorite=true;
                          Subsonic native via getStarred2. Both
                          providers carry UserData.IsFavorite on the
                          adapted item so the Python refine layer can
                          always re-check it.
    date_added  (date)  ops: in_the_last, before, after
                        — when the track entered the library. Jellyfin
                          carries it as ``DateCreated`` (ISO 8601);
                          Subsonic carries the song's ``created``
                          timestamp (surfaced on adapted items as
                          ``DateCreated``). Neither backend has a
                          server-side "added in the last N days"
                          filter, so all three ops always refine
                          client-side in the Python pass.
    last_played (date)  ops: in_the_last, before, after
                        — when the track was last played. Jellyfin
                          carries it as ``UserData.LastPlayedDate``
                          (ISO 8601). Subsonic has **no** per-track
                          last-played timestamp, so on Subsonic a
                          ``last_played`` rule cannot match anything
                          and is documented as unsupported there.

The ``artist`` / ``album`` string fields additionally accept
``starts_with`` / ``ends_with`` operators. Neither Jellyfin nor
Subsonic exposes a server-side filter for prefix/suffix matching, so
both ops are always evaluated client-side in the Python refine pass.

Date operators (schema v2)::

    in_the_last   value = int   — number of days; matches items whose
                                  date is within the last N days from
                                  "now" (inclusive of the boundary).
                                  ``value`` must be a positive int.
    before        value = str  — ISO 8601 date ("YYYY-MM-DD") or
                                  datetime; matches items whose date is
                                  strictly earlier than the value.
    after         value = str  — ISO 8601 date / datetime; matches
                                  items whose date is strictly later
                                  than the value.

The validator type-checks ``in_the_last`` as an ``int`` and
``before`` / ``after`` as ISO-parseable strings — a malformed date
string is rejected with a clear message. ``in_the_last`` rejects
zero / negative day counts.

``sort`` accepts the schema field names *and* the special token
``"random"`` (shuffle order). ``"random"`` is not a real field —
the validator whitelists it explicitly and ``smart_rule_eval``
handles it in ``sort_items``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List

# ── Date-field sentinel ──────────────────────────────────────────────
#
# Date fields don't carry a single Python ``type`` — the validated
# value type depends on the operator (``int`` days for
# ``in_the_last``, an ISO-string for ``before`` / ``after``). FIELDS
# entries for date fields use this sentinel as their ``"type"`` so the
# validator knows to dispatch into ``_validate_date_value`` instead of
# the plain ``_is_type`` check.


class _DateField:
    """Marker for schema fields whose validated value type is
    operator-dependent (see ``_validate_date_value``)."""

    __name__ = "date"


DATE = _DateField()

# Operators valid only on date fields. Provider translators consult
# this to know a rule is a date rule even before they look up FIELDS.
DATE_OPS = ("in_the_last", "before", "after")


# ── Field catalogue ──────────────────────────────────────────────────
#
# Each entry: ``{"type": <python type>, "ops": [<allowed op>, ...]}``.
# The validator consults this; provider translators consult it to
# emit the right server-side query and to know which client-side
# fallback to apply. Date fields use the ``DATE`` sentinel as their
# type because the validated value type varies by operator.

FIELDS: Dict[str, Dict[str, Any]] = {
    "genre": {"type": str, "ops": ["equals", "not_equals"]},
    "artist": {"type": str, "ops": ["equals", "contains", "starts_with", "ends_with"]},
    "album": {"type": str, "ops": ["equals", "not_equals", "contains", "starts_with", "ends_with"]},
    "year": {"type": int, "ops": ["equals", "greater_than", "less_than", "between"]},
    "play_count": {"type": int, "ops": ["greater_than", "less_than", "equals"]},
    "rating": {"type": int, "ops": ["greater_than", "less_than", "equals"]},
    "is_favorite": {"type": bool, "ops": ["equals"]},
    "date_added": {"type": DATE, "ops": list(DATE_OPS)},
    "last_played": {"type": DATE, "ops": list(DATE_OPS)},
}

VALID_MATCH = ("all", "any")


def parse_iso_date(value: Any) -> datetime:
    """Parse an ISO 8601 date or datetime string into a naive
    ``datetime``.

    Accepts plain ``"YYYY-MM-DD"`` dates and full datetimes; a
    trailing ``Z`` (Zulu / UTC) is normalized to ``+00:00`` so
    ``datetime.fromisoformat`` accepts it on every supported Python.
    Any timezone offset is dropped (converted to a naive value) so the
    rule engine compares apples to apples against ``datetime.now()``.

    Raises ``ValueError`` on a non-string or unparseable input — the
    schema validator catches this to emit a friendly message.
    """
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"expected an ISO date string (got {value!r})")
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is not None:
        # Drop the offset — the engine works in naive local time.
        dt = dt.replace(tzinfo=None)
    return dt

# Special ``sort`` tokens accepted alongside the schema field names.
# These are not entries in FIELDS — they map onto behaviour in
# ``smart_rule_eval.sort_items`` rather than an item attribute.
SPECIAL_SORTS = ("random",)


# ── Validator ────────────────────────────────────────────────────────


def validate_rules(rules: Any) -> List[str]:
    """Check a rule-set dict for shape errors.

    Returns a list of human-readable validation messages — empty list
    means the input is valid. The check is *structural*; a rule set
    can be valid but still resolve to zero results when evaluated.

    Errors collected (rather than fail-fast) so the UI can surface
    every problem in one preview pass.
    """
    errors: List[str] = []

    if not isinstance(rules, dict):
        return [f"rule set must be a dict (got {type(rules).__name__})"]

    match = rules.get("match", "all")
    if match not in VALID_MATCH:
        errors.append(f"'match' must be one of {VALID_MATCH!r} (got {match!r})")

    raw_rules = rules.get("rules")
    if raw_rules is None:
        errors.append("'rules' key is required")
    elif not isinstance(raw_rules, list):
        errors.append(f"'rules' must be a list (got {type(raw_rules).__name__})")
    else:
        for i, rule in enumerate(raw_rules):
            errors.extend(_validate_one(rule, i))

    limit = rules.get("limit")
    if limit is not None and not isinstance(limit, int):
        errors.append(f"'limit' must be an int or None (got {type(limit).__name__})")
    elif isinstance(limit, int) and limit < 0:
        # bool is an int subclass — gate the negative-limit message on
        # plain ints so a stray True/False produces a clearer error
        # from the type check above instead of "limit must be ≥ 0".
        if not isinstance(limit, bool):
            errors.append(f"'limit' must be >= 0 (got {limit})")

    sort = rules.get("sort")
    if sort is not None:
        if not isinstance(sort, str):
            errors.append(f"'sort' must be a str or None (got {type(sort).__name__})")
        elif sort not in FIELDS and sort not in SPECIAL_SORTS:
            valid = sorted(FIELDS) + list(SPECIAL_SORTS)
            errors.append(f"'sort' references unknown field {sort!r}; valid: {valid}")

    sort_desc = rules.get("sort_desc", False)
    if not isinstance(sort_desc, bool):
        errors.append(f"'sort_desc' must be a bool (got {type(sort_desc).__name__})")

    return errors


def _validate_one(rule: Any, idx: int) -> List[str]:
    """Per-rule structural check. Idx is the position in the rules
    list, used only for error-message clarity."""
    errors: List[str] = []
    prefix = f"rules[{idx}]"

    if not isinstance(rule, dict):
        return [f"{prefix} must be a dict (got {type(rule).__name__})"]

    field = rule.get("field")
    op = rule.get("op")
    if "field" not in rule:
        errors.append(f"{prefix} missing 'field'")
    if "op" not in rule:
        errors.append(f"{prefix} missing 'op'")
    if "value" not in rule:
        errors.append(f"{prefix} missing 'value'")
    if errors:
        # No point checking field/op compatibility if the basic keys
        # are absent — surface those first.
        return errors

    if field not in FIELDS:
        return [f"{prefix} unknown field {field!r}; valid: {sorted(FIELDS)}"]

    spec = FIELDS[field]
    if op not in spec["ops"]:
        errors.append(f"{prefix} op {op!r} not valid for field {field!r}; valid: {spec['ops']}")

    value = rule["value"]
    expected_type = spec["type"]
    if expected_type is DATE:
        # Date fields: the value type is operator-dependent, so the
        # plain _is_type path doesn't apply. Only check the value if
        # the op itself is valid — an unknown op already errored above.
        if op in DATE_OPS:
            errors.extend(_validate_date_value(value, op, prefix))
    elif op == "between":
        # Between takes a two-element sequence (lo, hi) of the field's
        # type. Tuples and lists both fine for portability across JSON.
        if not isinstance(value, (list, tuple)) or len(value) != 2:
            errors.append(f"{prefix} op 'between' requires a [lo, hi] pair")
        else:
            for j, sub in enumerate(value):
                if not _is_type(sub, expected_type):
                    errors.append(
                        f"{prefix} 'between' element {j} must be "
                        f"{expected_type.__name__} (got "
                        f"{type(sub).__name__})"
                    )
    else:
        if not _is_type(value, expected_type):
            errors.append(
                f"{prefix} value must be {expected_type.__name__} (got {type(value).__name__})"
            )
        elif expected_type is str and not str(value).strip():
            # An empty / blank text value is structurally a str but a
            # semantically incomplete rule, and it resolves
            # INCONSISTENTLY: a server-side query (Jellyfin) ignores the
            # empty filter and matches everything, while the client-side
            # eval matches nothing — so the editor preview and play-time
            # disagree. Reject it so the rule has to be filled in. (0 /
            # False for numeric / favorite fields are real values and are
            # unaffected — this gate is str-fields-only.)
            errors.append(f"{prefix} value for op {op!r} must not be empty")

    return errors


def _validate_date_value(value: Any, op: str, prefix: str) -> List[str]:
    """Validate the ``value`` of a date-field rule for operator ``op``.

    * ``in_the_last`` — value must be a positive ``int`` number of
      days. ``bool`` is rejected (it's an ``int`` subclass but never a
      sensible day count), as are zero and negatives.
    * ``before`` / ``after`` — value must be an ISO 8601 date or
      datetime string parseable by :func:`parse_iso_date`.

    Returns a list of error messages (empty when valid).
    """
    if op == "in_the_last":
        if isinstance(value, bool) or not isinstance(value, int):
            return [
                f"{prefix} op 'in_the_last' value must be an int "
                f"number of days (got {type(value).__name__})"
            ]
        if value <= 0:
            return [f"{prefix} op 'in_the_last' value must be > 0 (got {value})"]
        return []
    # before / after — ISO string.
    try:
        parse_iso_date(value)
    except ValueError as exc:
        return [f"{prefix} op {op!r} value must be an ISO date string: {exc}"]
    return []


def _is_type(value: Any, expected: type) -> bool:
    """Type predicate that rejects ``bool`` for ``int`` checks.

    ``isinstance(True, int)`` is ``True`` in Python — convenient but
    surprising in a rule schema where ``True`` should never be a valid
    value for a numeric ``year`` / ``play_count`` / ``rating`` field.
    """
    if expected is int and isinstance(value, bool):
        return False
    return isinstance(value, expected)
