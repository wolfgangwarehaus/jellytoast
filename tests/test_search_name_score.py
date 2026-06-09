"""Unit coverage for search_view._name_score — the pure relevance-tier
helper that reorders SearchView result sections by how strongly the item's
own name matches the query (exact / prefix / word-start / substring / none).

Normalization goes through article_stripped_key, so casing and leading
articles ("The Beatles") collapse — which is why a naive substring example
like "The Beekeeper"/"bee" actually scores 80 (article-strip yields
"beekeeper", and "bee" is a prefix), not 40. "Greenhouse"/"house" is the
genuine substring-only case.
"""

import pytest

from modules.search_view import _name_score


@pytest.mark.parametrize(
    "name, query, expected",
    [
        ("feist", "feist", 100),            # exact
        ("The Beatles", "beatles", 100),    # exact after article-strip
        ("Sufjan Stevens", "sufjan", 80),   # prefix
        ("Let It Die", "it", 60),           # word-start on a non-first word
        ("Greenhouse", "house", 40),        # substring only (not prefix/word-start)
        ("xyz", "abc", 0),                  # no match
        ("", "die", 0),                     # empty name -> 0
        ("Let It Die", "", 0),              # empty query -> 0
    ],
)
def test_name_score_tiers(name, query, expected):
    assert _name_score(name, query) == expected
