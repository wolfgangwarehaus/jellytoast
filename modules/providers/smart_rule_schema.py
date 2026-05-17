"""Smart-playlist rule schema — the canonical jellytoast format.

Smart playlists are rule sets stored client-side (see
``docs/research/smart_playlists.md``) and evaluated by translating the
rule set into a single provider call plus a Python refine pass. This
module owns the **shape** of those rule sets: the validator, the field
catalogue, and the per-field operator whitelist.

The shape is intentionally smaller than Navidrome's `.nsp` format —
v1 supports a flat list of conditions with a single top-level
``match`` toggle (AND vs OR). Nested any/all groups and the long tail
of operators (`startsWith`, `inTheLast`, …) ship in a follow-up.

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

Initial field subset (v1)::

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
"""

from __future__ import annotations

from typing import Any, Dict, List


# ── Field catalogue ──────────────────────────────────────────────────
#
# Each entry: ``{"type": <python type>, "ops": [<allowed op>, ...]}``.
# The validator consults this; provider translators consult it to
# emit the right server-side query and to know which client-side
# fallback to apply.

FIELDS: Dict[str, Dict[str, Any]] = {
    "genre": {"type": str, "ops": ["equals", "not_equals"]},
    "artist": {"type": str, "ops": ["equals", "contains"]},
    "album": {"type": str, "ops": ["equals", "contains"]},
    "year": {"type": int, "ops": ["equals", "greater_than", "less_than", "between"]},
    "play_count": {"type": int, "ops": ["greater_than", "less_than", "equals"]},
    "rating": {"type": int, "ops": ["greater_than", "less_than", "equals"]},
}

VALID_MATCH = ("all", "any")


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
        elif sort not in FIELDS:
            errors.append(f"'sort' references unknown field {sort!r}; valid: {sorted(FIELDS)}")

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
    if op == "between":
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

    return errors


def _is_type(value: Any, expected: type) -> bool:
    """Type predicate that rejects ``bool`` for ``int`` checks.

    ``isinstance(True, int)`` is ``True`` in Python — convenient but
    surprising in a rule schema where ``True`` should never be a valid
    value for a numeric ``year`` / ``play_count`` / ``rating`` field.
    """
    if expected is int and isinstance(value, bool):
        return False
    return isinstance(value, expected)
