"""SubsonicProvider.get_lyrics — picking among several structuredLyrics.

Navidrome 0.63+ reads sidecar lyrics (.lrc/.ttml/.elrc/.srt/…) next to the
embedded tag, so getLyricsBySongId commonly returns two ``main`` entries:
one plain, one synced, in either order. Taking ``[0]`` blindly could hide
the synced one and leave the rail static. Pure logic — stubs ``_request``.
"""

from __future__ import annotations

from jellytoast.providers.subsonic import SubsonicProvider


def _provider(entries):
    p = SubsonicProvider()
    p._request = lambda method, params=None: {"lyricsList": {"structuredLyrics": entries}}
    return p


_PLAIN = {"synced": False, "line": [{"value": "plain one"}, {"value": "plain two"}]}
_SYNCED = {
    "synced": True,
    "line": [{"start": 1000, "value": "timed one"}, {"start": 2500, "value": "timed two"}],
}


def test_prefers_synced_entry_even_when_listed_second():
    out = _provider([_PLAIN, _SYNCED]).get_lyrics("s1")
    assert [ln["Text"] for ln in out["Lyrics"]] == ["timed one", "timed two"]
    assert out["Lyrics"][1]["Start"] == 2500 * 10_000


def test_falls_back_to_first_when_none_synced():
    out = _provider([_PLAIN, dict(_PLAIN, line=[{"value": "other"}])]).get_lyrics("s1")
    assert out["Lyrics"][0]["Text"] == "plain one"


def test_skips_synced_entry_with_no_lines():
    out = _provider([_PLAIN, {"synced": True, "line": []}]).get_lyrics("s1")
    assert out["Lyrics"][0]["Text"] == "plain one"


def test_empty_list_is_none():
    assert _provider([]).get_lyrics("s1") is None
