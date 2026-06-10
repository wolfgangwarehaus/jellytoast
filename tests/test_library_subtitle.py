"""Regression for the album subtitle / artist-line crash (review P1
library-grid-1): when an album's ``AlbumArtist`` (singular string) is empty
but ``AlbumArtists`` is populated with the ``{Id, Name}`` dicts both providers
emit, a bare ``", ".join(AlbumArtists)`` raised TypeError mid delegate-paint.
``_compute_subtitle`` now extracts Name and skips non-dict entries.
"""

from jellytoast.library_grid import _compute_subtitle


def test_album_subtitle_prefers_singular_albumartist():
    item = {"AlbumArtist": "Radiohead", "AlbumArtists": [{"Id": "1", "Name": "Radiohead"}]}
    assert _compute_subtitle(item, "album") == "Radiohead"


def test_album_subtitle_from_dict_list_when_singular_empty():
    # The crash case: empty AlbumArtist + populated dict list.
    item = {
        "AlbumArtist": "",
        "AlbumArtists": [{"Id": "1", "Name": "Sufjan Stevens"}, {"Id": "2", "Name": "Bryce"}],
    }
    assert _compute_subtitle(item, "album") == "Sufjan Stevens, Bryce"


def test_album_subtitle_skips_malformed_entries():
    item = {
        "AlbumArtist": None,
        "AlbumArtists": [{"Id": "1", "Name": "Real"}, {"Id": "2"}, None, "stringy"],
    }
    # Only the well-formed {Id, Name} dict survives; no TypeError.
    assert _compute_subtitle(item, "album") == "Real"


def test_album_subtitle_empty_when_no_artist():
    assert _compute_subtitle({"AlbumArtist": "", "AlbumArtists": []}, "album") == ""
    assert _compute_subtitle({}, "album") == ""
