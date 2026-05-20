"""Tests for the smart-playlist rule schema validator.

Pure data tests — no Qt, no network, no provider instances. The
validator's job is to surface every shape error in one pass; these
tests assert that contract field by field.
"""

from __future__ import annotations

from modules.providers.smart_rule_schema import (
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
