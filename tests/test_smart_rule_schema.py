"""Tests for the smart-playlist rule schema validator.

Pure data tests — no Qt, no network, no provider instances. The
validator's job is to surface every shape error in one pass; these
tests assert that contract field by field.
"""

from __future__ import annotations

from jellytoast.providers.smart_rule_schema import (
    FIELDS,
    SPECIAL_SORTS,
    validate_rules,
)


class TestValidInput:
    def test_minimal_valid_rule_set(self):
        rules = {
            "match": "all",
            "rules": [{"field": "genre", "op": "equals", "value": "Rock"}],
        }
        assert validate_rules(rules) == []

    def test_all_supported_fields_validate(self):
        # Sanity: one rule per field with a representative op + value.
        cases = [
            ("genre", "equals", "Rock"),
            ("genre", "not_equals", "Pop"),
            ("artist", "equals", "Feist"),
            ("artist", "contains", "Bjö"),
            ("album", "equals", "Reminder"),
            ("album", "contains", "Reminder"),
            ("year", "equals", 2007),
            ("year", "greater_than", 2000),
            ("year", "less_than", 2010),
            ("year", "between", [2000, 2010]),
            ("play_count", "greater_than", 5),
            ("rating", "equals", 4),
        ]
        for field, op, value in cases:
            errors = validate_rules(
                {
                    "match": "all",
                    "rules": [{"field": field, "op": op, "value": value}],
                }
            )
            assert errors == [], f"{field} {op} {value!r}: {errors}"

    def test_empty_rules_list_is_valid_shape(self):
        # Engines treat empty rules as "no matches" but the *shape*
        # is well-formed.
        assert validate_rules({"match": "all", "rules": []}) == []

    def test_optional_sort_and_limit(self):
        rules = {
            "match": "any",
            "rules": [{"field": "year", "op": "greater_than", "value": 2020}],
            "sort": "year",
            "sort_desc": True,
            "limit": 100,
        }
        assert validate_rules(rules) == []


class TestStructuralErrors:
    def test_top_level_not_dict(self):
        errors = validate_rules(["not", "a", "dict"])
        assert len(errors) == 1
        assert "must be a dict" in errors[0]

    def test_missing_rules_key(self):
        errors = validate_rules({"match": "all"})
        assert any("'rules' key is required" in e for e in errors)

    def test_rules_not_a_list(self):
        errors = validate_rules({"match": "all", "rules": "oops"})
        assert any("'rules' must be a list" in e for e in errors)

    def test_bad_match_value(self):
        errors = validate_rules({"match": "either", "rules": []})
        assert any("'match'" in e for e in errors)

    def test_rule_missing_field(self):
        errors = validate_rules(
            {
                "match": "all",
                "rules": [{"op": "equals", "value": "Rock"}],
            }
        )
        assert any("missing 'field'" in e for e in errors)

    def test_rule_missing_op_and_value(self):
        errors = validate_rules(
            {
                "match": "all",
                "rules": [{"field": "genre"}],
            }
        )
        # Both keys should be flagged.
        joined = " ".join(errors)
        assert "missing 'op'" in joined
        assert "missing 'value'" in joined


class TestUnknownFieldAndOp:
    def test_unknown_field(self):
        errors = validate_rules(
            {
                "match": "all",
                "rules": [{"field": "bpm", "op": "equals", "value": 120}],
            }
        )
        assert any("unknown field 'bpm'" in e for e in errors)

    def test_unknown_op_for_known_field(self):
        errors = validate_rules(
            {
                "match": "all",
                "rules": [{"field": "genre", "op": "regex", "value": "Rock"}],
            }
        )
        assert any("not valid for field 'genre'" in e for e in errors)

    def test_op_valid_only_for_other_field(self):
        # `between` is valid for `year` but not for `genre`.
        errors = validate_rules(
            {
                "match": "all",
                "rules": [{"field": "genre", "op": "between", "value": ["a", "z"]}],
            }
        )
        assert any("not valid for field 'genre'" in e for e in errors)


class TestValueTypes:
    def test_year_value_must_be_int(self):
        errors = validate_rules(
            {
                "match": "all",
                "rules": [{"field": "year", "op": "equals", "value": "2007"}],
            }
        )
        assert any("must be int" in e for e in errors)

    def test_year_bool_rejected(self):
        # bool is int in Python — schema explicitly rejects it.
        errors = validate_rules(
            {
                "match": "all",
                "rules": [{"field": "year", "op": "equals", "value": True}],
            }
        )
        assert any("must be int" in e for e in errors)

    def test_between_requires_pair(self):
        errors = validate_rules(
            {
                "match": "all",
                "rules": [{"field": "year", "op": "between", "value": 2007}],
            }
        )
        assert any("requires a [lo, hi] pair" in e for e in errors)

    def test_between_pair_element_type_checked(self):
        errors = validate_rules(
            {
                "match": "all",
                "rules": [
                    {"field": "year", "op": "between", "value": [2000, "x"]},
                ],
            }
        )
        assert any("element 1 must be int" in e for e in errors)

    def test_genre_value_must_be_str(self):
        errors = validate_rules(
            {
                "match": "all",
                "rules": [{"field": "genre", "op": "equals", "value": 5}],
            }
        )
        assert any("must be str" in e for e in errors)


class TestSortAndLimit:
    def test_sort_unknown_field(self):
        errors = validate_rules(
            {
                "match": "all",
                "rules": [],
                "sort": "bogus",
            }
        )
        assert any("references unknown field" in e for e in errors)

    def test_limit_negative_rejected(self):
        errors = validate_rules(
            {
                "match": "all",
                "rules": [],
                "limit": -1,
            }
        )
        assert any("must be >= 0" in e for e in errors)

    def test_limit_wrong_type(self):
        errors = validate_rules(
            {
                "match": "all",
                "rules": [],
                "limit": "100",
            }
        )
        assert any("'limit' must be an int" in e for e in errors)

    def test_sort_desc_must_be_bool(self):
        errors = validate_rules(
            {
                "match": "all",
                "rules": [],
                "sort_desc": "yes",
            }
        )
        assert any("'sort_desc' must be a bool" in e for e in errors)


class TestFieldsLookup:
    def test_fields_constant_shape(self):
        # The constant is what the UI / providers consult to enumerate
        # options. Each entry must have the expected sub-keys.
        for name, spec in FIELDS.items():
            assert "type" in spec, f"{name} missing 'type'"
            assert "ops" in spec, f"{name} missing 'ops'"
            assert isinstance(spec["ops"], list)
            assert spec["ops"], f"{name} ops list is empty"


class TestIsFavoriteField:
    def test_is_favorite_equals_true_validates(self):
        errors = validate_rules(
            {
                "match": "all",
                "rules": [{"field": "is_favorite", "op": "equals", "value": True}],
            }
        )
        assert errors == []

    def test_is_favorite_equals_false_validates(self):
        errors = validate_rules(
            {
                "match": "all",
                "rules": [{"field": "is_favorite", "op": "equals", "value": False}],
            }
        )
        assert errors == []

    def test_is_favorite_non_bool_value_rejected(self):
        errors = validate_rules(
            {
                "match": "all",
                "rules": [{"field": "is_favorite", "op": "equals", "value": "yes"}],
            }
        )
        assert any("must be bool" in e for e in errors)

    def test_is_favorite_int_value_rejected(self):
        # An int is not a bool — schema demands the real type.
        errors = validate_rules(
            {
                "match": "all",
                "rules": [{"field": "is_favorite", "op": "equals", "value": 1}],
            }
        )
        assert any("must be bool" in e for e in errors)

    def test_is_favorite_rejects_contains_op(self):
        errors = validate_rules(
            {
                "match": "all",
                "rules": [{"field": "is_favorite", "op": "contains", "value": True}],
            }
        )
        assert any("not valid for field 'is_favorite'" in e for e in errors)

    def test_is_favorite_in_fields_catalogue(self):
        assert "is_favorite" in FIELDS
        assert FIELDS["is_favorite"]["type"] is bool
        assert FIELDS["is_favorite"]["ops"] == ["equals"]


class TestStartsEndsWithOps:
    def test_artist_starts_with_validates(self):
        errors = validate_rules(
            {
                "match": "all",
                "rules": [{"field": "artist", "op": "starts_with", "value": "The"}],
            }
        )
        assert errors == []

    def test_artist_ends_with_validates(self):
        errors = validate_rules(
            {
                "match": "all",
                "rules": [{"field": "artist", "op": "ends_with", "value": "Band"}],
            }
        )
        assert errors == []

    def test_album_starts_with_validates(self):
        errors = validate_rules(
            {
                "match": "all",
                "rules": [{"field": "album", "op": "starts_with", "value": "A"}],
            }
        )
        assert errors == []

    def test_album_ends_with_validates(self):
        errors = validate_rules(
            {
                "match": "all",
                "rules": [{"field": "album", "op": "ends_with", "value": "Live"}],
            }
        )
        assert errors == []

    def test_starts_with_value_must_be_str(self):
        errors = validate_rules(
            {
                "match": "all",
                "rules": [{"field": "artist", "op": "starts_with", "value": 5}],
            }
        )
        assert any("must be str" in e for e in errors)

    def test_genre_rejects_starts_with(self):
        # starts_with is only whitelisted for artist / album.
        errors = validate_rules(
            {
                "match": "all",
                "rules": [{"field": "genre", "op": "starts_with", "value": "Ro"}],
            }
        )
        assert any("not valid for field 'genre'" in e for e in errors)

    def test_year_rejects_ends_with(self):
        errors = validate_rules(
            {
                "match": "all",
                "rules": [{"field": "year", "op": "ends_with", "value": "07"}],
            }
        )
        assert any("not valid for field 'year'" in e for e in errors)


class TestRandomSort:
    def test_random_sort_accepted(self):
        errors = validate_rules(
            {
                "match": "all",
                "rules": [],
                "sort": "random",
            }
        )
        assert errors == []

    def test_random_in_special_sorts(self):
        assert "random" in SPECIAL_SORTS

    def test_random_is_not_a_field(self):
        # random must NOT leak into FIELDS — it's a sort-only token.
        assert "random" not in FIELDS

    def test_still_rejects_unknown_sort(self):
        errors = validate_rules(
            {
                "match": "all",
                "rules": [],
                "sort": "shuffleish",
            }
        )
        assert any("references unknown field" in e for e in errors)

    def test_random_sort_with_rules_and_limit(self):
        errors = validate_rules(
            {
                "match": "all",
                "rules": [{"field": "genre", "op": "equals", "value": "Jazz"}],
                "sort": "random",
                "limit": 25,
            }
        )
        assert errors == []


# ─────────────────────────────────────────────────────────────────────
# Schema v2 — date fields (date_added / last_played)
# ─────────────────────────────────────────────────────────────────────


def _one(field, op, value):
    return validate_rules({"match": "all", "rules": [{"field": field, "op": op, "value": value}]})


class TestDateFieldCatalogue:
    def test_date_fields_present(self):
        assert "date_added" in FIELDS
        assert "last_played" in FIELDS

    def test_date_fields_share_the_date_ops(self):
        for field in ("date_added", "last_played"):
            assert set(FIELDS[field]["ops"]) == {"in_the_last", "before", "after"}

    def test_date_fields_accepted_as_sort(self):
        for field in ("date_added", "last_played"):
            errors = validate_rules({"match": "all", "rules": [], "sort": field})
            assert errors == [], f"{field}: {errors}"


class TestInTheLastOperator:
    def test_valid_positive_int(self):
        for field in ("date_added", "last_played"):
            assert _one(field, "in_the_last", 30) == []

    def test_rejects_zero_days(self):
        errors = _one("date_added", "in_the_last", 0)
        assert any("must be > 0" in e for e in errors)

    def test_rejects_negative_days(self):
        errors = _one("last_played", "in_the_last", -7)
        assert any("must be > 0" in e for e in errors)

    def test_rejects_string_value(self):
        errors = _one("date_added", "in_the_last", "30")
        assert any("must be an int" in e for e in errors)

    def test_rejects_bool_value(self):
        # bool is an int subclass — must not slip through as a day count.
        errors = _one("date_added", "in_the_last", True)
        assert any("must be an int" in e for e in errors)

    def test_rejects_float_value(self):
        errors = _one("last_played", "in_the_last", 30.5)
        assert any("must be an int" in e for e in errors)


class TestBeforeAfterOperators:
    def test_valid_iso_date(self):
        for op in ("before", "after"):
            assert _one("date_added", op, "2026-01-01") == []

    def test_valid_iso_datetime(self):
        assert _one("last_played", "after", "2026-05-20T12:30:00") == []

    def test_valid_iso_datetime_with_zulu(self):
        # A trailing Z (UTC) must parse cleanly.
        assert _one("date_added", "before", "2026-05-20T00:00:00Z") == []

    def test_rejects_malformed_date_string(self):
        errors = _one("date_added", "before", "not-a-date")
        assert any("ISO date string" in e for e in errors)


class TestEmptyTextValueRejected:
    """An empty / blank text value is structurally a str but a
    semantically incomplete rule that resolves INCONSISTENTLY — a
    server-side query matches everything, the client eval matches
    nothing — so validate_rules rejects it. (2026-05-28 audit #11.)"""

    def test_empty_string_rejected(self):
        assert any("must not be empty" in e for e in _one("genre", "equals", ""))

    def test_whitespace_only_rejected(self):
        assert any("must not be empty" in e for e in _one("genre", "equals", "   "))

    def test_empty_rejected_for_contains_op(self):
        assert any("must not be empty" in e for e in _one("artist", "contains", ""))

    def test_nonempty_value_ok(self):
        assert _one("genre", "equals", "rock") == []

    def test_numeric_zero_not_treated_as_empty(self):
        # play_count == 0 is a real value; the empty gate is str-fields-only.
        assert _one("play_count", "equals", 0) == []

    def test_is_favorite_false_not_treated_as_empty(self):
        assert _one("is_favorite", "equals", False) == []

    def test_rejects_non_string_value(self):
        errors = _one("last_played", "after", 20260101)
        assert any("ISO date string" in e for e in errors)

    def test_rejects_empty_string(self):
        errors = _one("date_added", "before", "")
        assert any("ISO date string" in e for e in errors)


class TestDateFieldOpRejection:
    def test_date_field_rejects_numeric_ops(self):
        for op in ("equals", "greater_than", "less_than", "between"):
            errors = _one("date_added", op, "2026-01-01")
            assert any("not valid for field 'date_added'" in e for e in errors)

    def test_in_the_last_rejected_on_non_date_field(self):
        errors = _one("year", "in_the_last", 30)
        assert any("not valid for field 'year'" in e for e in errors)

    def test_before_rejected_on_non_date_field(self):
        errors = _one("play_count", "before", "2026-01-01")
        assert any("not valid for field 'play_count'" in e for e in errors)
