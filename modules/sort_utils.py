"""Sort/index helpers — primarily article-stripping for music browse.

Mirrors the behavior most music apps (iTunes, Apple Music, Spotify)
default to: leading "The ", "A ", and "An " are ignored when sorting
or indexing by name. "The Antlers" sorts under A, "The Decemberists"
under D, "The Flaming Lips" under F. Display strings keep the article;
only the sort key drops it.

Jellyfin's server has an opt-in IgnoreArticles feature but it depends
on per-server admin config and only applies to certain fields. This
module provides a consistent client-side fallback applied at every
native browse surface.
"""

import unicodedata

# Tested in lowercase; comparison is case-insensitive.
LEADING_ARTICLES = ("the ", "a ", "an ")


def strip_leading_article(s: str) -> str:
    """Drop a leading 'The ', 'A ', or 'An ' from `s` (case-
    insensitive). Empty / None passes through. Returns the
    article-stripped string, preserving the rest of the casing."""
    if not s:
        return s or ""
    low = s.lower()
    for art in LEADING_ARTICLES:
        if low.startswith(art):
            return s[len(art):].lstrip()
    return s


def _fold_diacritics(s: str) -> str:
    # NFKD splits accented chars into base + combining mark; dropping
    # combining marks folds "Ásgeir" → "Asgeir" and "Beyoncé" → "Beyonce"
    # so they cluster with their unaccented neighbors. Without this
    # step Python's lexicographic sort on lowercase strings places "á"
    # (U+00E1) after "z" (U+007A), exiling accented artists to the end.
    return "".join(
        c for c in unicodedata.normalize("NFKD", s)
        if not unicodedata.combining(c)
    )


def article_stripped_key(s: str) -> str:
    """Lowercased, article-stripped, diacritic-folded sort key. Use
    for tuple sorts:
    `sorted(items, key=lambda it: article_stripped_key(it["AlbumArtist"]))`."""
    return _fold_diacritics(strip_leading_article((s or "").strip())).lower()


def first_letter(s: str) -> str:
    """Uppercase first character of the article-stripped name. Used
    by alphabet-index columns so the section headers match the
    article-stripped sort. Empty string when no meaningful first
    letter is available. Diacritics are folded so "Ásgeir" indexes
    under A, matching `article_stripped_key`."""
    stripped = _fold_diacritics(strip_leading_article((s or "").strip()))
    return stripped[0].upper() if stripped else ""
