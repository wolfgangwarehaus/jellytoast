"""Tests for the ICY-title → cover-art pipeline.

Pure-function pieces (parse_icy_title) are exercised directly. The
network-driven helpers (MusicBrainz + Cover Art Archive) are tested
with a mocked ``requests`` so the suite never reaches the wire.
"""

from __future__ import annotations

from unittest import mock

import pytest

from modules import radio_art


@pytest.fixture(autouse=True)
def _clean_cache():
    """Module-level cache leaks across tests if not reset — each test
    starts from a clean LRU + a zeroed rate-limit timestamp."""
    radio_art._clear_cache_for_tests()
    radio_art._mb_last_call_t = 0.0
    yield
    radio_art._clear_cache_for_tests()
    radio_art._mb_last_call_t = 0.0


# ── parse_icy_title ────────────────────────────────────────────────────────


class TestParseIcyTitle:
    def test_artist_dash_title(self):
        assert radio_art.parse_icy_title("Aphex Twin - Xtal") == ("Aphex Twin", "Xtal")

    def test_strips_whitespace(self):
        assert radio_art.parse_icy_title("  Aphex Twin  -  Xtal  ") == ("Aphex Twin", "Xtal")

    def test_three_part_takes_last_two(self):
        # Show - Artist - Title (NTS / SomaFM specials)
        assert radio_art.parse_icy_title("NTS Breakfast Show - Floating Points - Vocoder") == (
            "Floating Points",
            "Vocoder",
        )

    def test_no_separator_returns_title_only(self):
        # Some stations broadcast just a title, esp. during station IDs.
        assert radio_art.parse_icy_title("Some Track Name") == ("", "Some Track Name")

    def test_empty_returns_empty(self):
        assert radio_art.parse_icy_title("") == ("", "")
        assert radio_art.parse_icy_title("   ") == ("", "")

    def test_dash_inside_title_word_not_split(self):
        # Plain "-" without surrounding spaces shouldn't split — song
        # titles like "Re-Animator" must stay intact.
        assert radio_art.parse_icy_title("Aphex Twin - Re-Animator") == (
            "Aphex Twin",
            "Re-Animator",
        )


# ── lookup_art_url ─────────────────────────────────────────────────────────


def _mb_response(recordings):
    """Build a fake MusicBrainz JSON payload."""
    resp = mock.MagicMock()
    resp.status_code = 200
    resp.content = b"{}"
    resp.json.return_value = {"recordings": recordings}
    return resp


def _caa_response(status: int, url: str = ""):
    resp = mock.MagicMock()
    resp.status_code = status
    resp.url = url
    return resp


class TestLookupArtUrl:
    def test_missing_fields_short_circuit(self):
        # No network call should happen — patch requests.get to assert
        # it isn't reached.
        with mock.patch.object(radio_art.requests, "get") as get:
            assert radio_art.lookup_art_url("", "Title") is None
            assert radio_art.lookup_art_url("Artist", "") is None
            assert radio_art.lookup_art_url("  ", "  ") is None
            get.assert_not_called()

    def test_hit_returns_caa_url(self):
        caa_url = "https://archive.org/coverart/release/abc/front-500.jpg"
        with mock.patch.object(radio_art.requests, "get") as get:
            get.side_effect = [
                _mb_response(
                    [
                        {
                            "score": 95,
                            "releases": [{"id": "release-abc"}],
                        }
                    ]
                ),
                _caa_response(200, url=caa_url),
            ]
            assert radio_art.lookup_art_url("Aphex Twin", "Xtal") == caa_url
            # Second call returns the cached value without hitting the
            # network again.
            assert radio_art.lookup_art_url("Aphex Twin", "Xtal") == caa_url
            assert get.call_count == 2

    def test_low_score_skipped(self):
        with mock.patch.object(radio_art.requests, "get") as get:
            get.return_value = _mb_response(
                [
                    {"score": 30, "releases": [{"id": "weak-match"}]},
                ]
            )
            assert radio_art.lookup_art_url("Obscure", "Track") is None
            # MB call happened; CAA didn't.
            assert get.call_count == 1

    def test_no_releases_skipped(self):
        with mock.patch.object(radio_art.requests, "get") as get:
            get.return_value = _mb_response(
                [
                    {"score": 95, "releases": []},
                ]
            )
            assert radio_art.lookup_art_url("Artist", "Title") is None

    def test_caa_404_returns_none(self):
        with mock.patch.object(radio_art.requests, "get") as get:
            get.side_effect = [
                _mb_response(
                    [{"score": 92, "releases": [{"id": "rel-1"}]}]
                ),
                _caa_response(404),
            ]
            assert radio_art.lookup_art_url("Artist", "Title") is None

    def test_mb_network_failure_caches_none(self):
        # An exception during MB search should NOT raise to the caller;
        # the GUI hook treats None as "use station logo".
        with mock.patch.object(radio_art.requests, "get") as get:
            get.side_effect = OSError("network down")
            assert radio_art.lookup_art_url("Artist", "Title") is None
            # Re-querying the same track does NOT retry — the negative
            # result is cached. Saves repeated thrash when the user is
            # actually offline.
            assert radio_art.lookup_art_url("Artist", "Title") is None
            assert get.call_count == 1

    def test_caching_is_case_insensitive(self):
        caa_url = "https://archive.org/cov/x.jpg"
        with mock.patch.object(radio_art.requests, "get") as get:
            get.side_effect = [
                _mb_response([{"score": 99, "releases": [{"id": "x"}]}]),
                _caa_response(200, url=caa_url),
            ]
            assert radio_art.lookup_art_url("APHEX TWIN", "Xtal") == caa_url
            # Differently-cased reads hit the cache.
            assert radio_art.lookup_art_url("aphex twin", "XTAL") == caa_url
            assert get.call_count == 2


# ── Preset logo helper ─────────────────────────────────────────────────────


class TestLogoUrlForStream:
    def test_known_stream_returns_logo(self):
        from modules.radio_presets import POPULAR_STATIONS, logo_url_for_stream

        # Find one preset that has a non-empty logoUrl.
        with_logo = [p for p in POPULAR_STATIONS if p.get("logoUrl")]
        assert with_logo, "fixture: at least one preset should ship a logo"
        preset = with_logo[0]
        assert logo_url_for_stream(preset["streamUrl"]) == preset["logoUrl"]

    def test_unknown_stream_returns_empty(self):
        from modules.radio_presets import logo_url_for_stream

        assert logo_url_for_stream("https://example.com/not-a-preset") == ""

    def test_empty_input_returns_empty(self):
        from modules.radio_presets import logo_url_for_stream

        assert logo_url_for_stream("") == ""
