"""Tests for the article-stripping sort helpers (jellytoast/sort_utils.py).

Covers leading-article removal, the diacritic-folded sort key, and the
alphabet-index first-letter derivation — including the empty / None /
whitespace edge cases every browse surface relies on.
"""

from __future__ import annotations

from jellytoast.sort_utils import (
    article_stripped_key,
    first_letter,
    strip_leading_article,
)


class TestStripLeadingArticle:
    def test_strips_the(self):
        assert strip_leading_article("The Antlers") == "Antlers"

    def test_strips_a(self):
        assert strip_leading_article("A Tribe Called Quest") == "Tribe Called Quest"

    def test_strips_an(self):
        assert strip_leading_article("An Horse") == "Horse"

    def test_case_insensitive(self):
        assert strip_leading_article("THE Beatles") == "Beatles"
        assert strip_leading_article("the xx") == "xx"

    def test_preserves_remaining_casing(self):
        # Only the article goes; the rest keeps its original case.
        assert strip_leading_article("The KLF") == "KLF"

    def test_no_article_unchanged(self):
        assert strip_leading_article("Radiohead") == "Radiohead"

    def test_article_word_not_a_prefix(self):
        # "Theory" starts with "the" but is not the article "The ".
        assert strip_leading_article("Theory of a Deadman") == "Theory of a Deadman"
        # "Anthrax" starts with "an" but isn't "An ".
        assert strip_leading_article("Anthrax") == "Anthrax"

    def test_extra_space_after_article_trimmed(self):
        assert strip_leading_article("The  Strokes") == "Strokes"

    def test_empty_and_none(self):
        assert strip_leading_article("") == ""
        assert strip_leading_article(None) == ""

    def test_article_only(self):
        # Nothing but the article leaves an empty string.
        assert strip_leading_article("The ") == ""


class TestArticleStrippedKey:
    def test_lowercased(self):
        assert article_stripped_key("Radiohead") == "radiohead"

    def test_strips_article_and_lowercases(self):
        assert article_stripped_key("The Antlers") == "antlers"

    def test_folds_diacritics(self):
        # "Ásgeir" clusters with unaccented A-names, not after Z.
        assert article_stripped_key("Ásgeir") == "asgeir"
        assert article_stripped_key("Beyoncé") == "beyonce"

    def test_surrounding_whitespace_trimmed(self):
        assert article_stripped_key("  The Strokes  ") == "strokes"

    def test_empty_and_none(self):
        assert article_stripped_key("") == ""
        assert article_stripped_key(None) == ""

    def test_accented_sorts_with_plain(self):
        # The whole point: "Ásgeir" must sort between "Arcade" and "Beach".
        keys = sorted(
            article_stripped_key(n)
            for n in ("Beach House", "Ásgeir", "Arcade Fire")
        )
        assert keys == ["arcade fire", "asgeir", "beach house"]


class TestFirstLetter:
    def test_plain_name(self):
        assert first_letter("Radiohead") == "R"

    def test_article_stripped(self):
        assert first_letter("The Antlers") == "A"

    def test_uppercased(self):
        assert first_letter("the xx") == "X"

    def test_diacritic_folded(self):
        assert first_letter("Ásgeir") == "A"

    def test_empty_and_none(self):
        assert first_letter("") == ""
        assert first_letter(None) == ""

    def test_whitespace_only_yields_empty(self):
        assert first_letter("   ") == ""

    def test_numeric_leading_char(self):
        # A name that starts with a digit reports the digit — the rail
        # groups those under a "#" bucket itself; this just passes the
        # raw first char through.
        assert first_letter("65daysofstatic") == "6"
