"""Tests for the library-grid year helpers (_year_text / _year_int).

_year_text is the single source of truth for the painted year; _year_int is
the clickable year-filter value. PremiereDate-derived years are now clickable
(feat/premieredate-clickable-year), so _year_int derives from _year_text.
"""

from jellytoast.library_grid import _year_int, _year_text


def test_year_text_prefers_production_year():
    assert _year_text({"ProductionYear": 1979, "PremiereDate": "1980-01-01"}) == "1979"


def test_year_text_falls_back_to_premieredate():
    assert _year_text({"PremiereDate": "1979-10-05T00:00:00Z"}) == "1979"


def test_year_text_empty_when_no_year():
    assert _year_text({}) == ""
    assert _year_text({"ProductionYear": None, "PremiereDate": ""}) == ""
    assert _year_text({"PremiereDate": "notayear"}) == ""


def test_year_int_from_production_year():
    assert _year_int({"ProductionYear": 1979}) == 1979
    assert _year_int({"ProductionYear": "1979"}) == 1979


def test_year_int_from_premieredate_is_clickable():
    # The behaviour change: a PremiereDate-only year now yields a clickable int.
    assert _year_int({"PremiereDate": "1979-10-05"}) == 1979


def test_year_int_zero_when_absent_or_bad():
    assert _year_int({}) == 0
    assert _year_int({"PremiereDate": "notayear"}) == 0
