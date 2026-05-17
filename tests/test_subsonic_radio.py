"""Tests for SubsonicProvider's internet-radio CRUD methods.

We mock the HTTP layer at ``SubsonicProvider._request`` rather than at
``requests.Session.get`` — the test scope is "do the radio methods call
the right Subsonic endpoints with the right params, and do they project
the response into the documented return shape?". Auth-param construction
(token / salt / version / client name) is covered by the provider's
existing auth tests; replaying it here would couple radio coverage to
unrelated transport details.

A tiny ``_FakeRequest`` records every call so each test can assert on
the endpoint name and parameter dict. Canned response payloads mirror
Navidrome's actual JSON, modulo the outer ``subsonic-response`` wrapper
(which ``_request`` already unwraps in production).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

import pytest

from modules.providers.subsonic import SubsonicProvider


# Canonical Navidrome shape — what /rest/getInternetRadioStations.view
# returns under ``subsonic-response.internetRadioStations``. The two
# entries cover the homepage-present and homepage-absent cases.
_STATIONS_FIXTURE: List[Dict[str, Any]] = [
    {
        "id": "rs-1",
        "name": "BBC Radio 6 Music",
        "streamUrl": "http://stream.live.vc.bbcmedia.co.uk/bbc_6music",
        "homePageUrl": "https://www.bbc.co.uk/6music",
    },
    {
        "id": "rs-2",
        "name": "SomaFM Groove Salad",
        "streamUrl": "https://ice1.somafm.com/groovesalad-128-mp3",
    },
]


class _FakeRequest:
    """Stand-in for ``SubsonicProvider._request`` that records calls.

    The provider's real method does auth-token construction, JSON
    decoding, and error translation; none of that matters for radio-
    method coverage. We bypass it entirely and assert on the (path,
    params) tuples the radio methods produced.
    """

    def __init__(self, response_for: Optional[Dict[str, Dict[str, Any]]] = None):
        self.calls: List[Tuple[str, Dict[str, Any]]] = []
        self._response_for = response_for or {}

    def __call__(
        self, path: str, params: Optional[dict] = None, server_url: Optional[str] = None
    ) -> Dict[str, Any]:
        self.calls.append((path, dict(params or {})))
        return self._response_for.get(path, {})


@pytest.fixture
def provider(monkeypatch):
    """A SubsonicProvider whose ``_request`` is a recording stub.

    We bypass ``__init__`` so the test doesn't touch the real
    Settings singleton / keyring at all — the only fields the radio
    methods read are ``_server_url`` (unused in the recorded path)
    and the result of ``_request``.
    """
    p = SubsonicProvider.__new__(SubsonicProvider)
    p._server_url = "http://example.test"
    p._username = "u"
    p._password = "p"
    return p


# ── get_internet_radio_stations ──────────────────────────────────────


class TestGetInternetRadioStations:
    def test_returns_stations_from_response(self, provider):
        fake = _FakeRequest(
            {
                "getInternetRadioStations": {
                    "internetRadioStations": {
                        "internetRadioStation": _STATIONS_FIXTURE,
                    },
                },
            }
        )
        provider._request = fake
        result = provider.get_internet_radio_stations()
        assert result == _STATIONS_FIXTURE
        # No params for the GET — server returns the whole list.
        assert fake.calls == [("getInternetRadioStations", {})]

    def test_empty_when_server_has_no_stations(self, provider):
        # Navidrome with zero stations returns the response without
        # an ``internetRadioStation`` array (just the empty
        # ``internetRadioStations`` container).
        provider._request = _FakeRequest(
            {
                "getInternetRadioStations": {"internetRadioStations": {}},
            }
        )
        assert provider.get_internet_radio_stations() == []

    def test_empty_when_container_missing(self, provider):
        # Defensive: some Subsonic flavors omit the wrapper key entirely
        # when there's no data. Still return [] rather than KeyError.
        provider._request = _FakeRequest({"getInternetRadioStations": {}})
        assert provider.get_internet_radio_stations() == []

    def test_empty_on_request_failure(self, provider):
        # Network blip / SubsonicError must not propagate — the UI's
        # empty state is the same as "request failed", and the
        # connectivity tracker already records the failure inside
        # _request itself.
        def _boom(*_a, **_kw):
            raise RuntimeError("network down")

        provider._request = _boom
        assert provider.get_internet_radio_stations() == []


# ── create_internet_radio_station ────────────────────────────────────


class TestCreateInternetRadioStation:
    def test_sends_required_params(self, provider):
        new_station = {
            "id": "rs-9",
            "name": "Test FM",
            "streamUrl": "http://example.test/stream.mp3",
            "homePageUrl": "https://example.test",
        }
        # The create endpoint returns empty; the method re-fetches the
        # list to find the new row and hand it back.
        fake = _FakeRequest(
            {
                "createInternetRadioStation": {},
                "getInternetRadioStations": {
                    "internetRadioStations": {
                        "internetRadioStation": [new_station],
                    },
                },
            }
        )
        provider._request = fake
        result = provider.create_internet_radio_station(
            "Test FM",
            "http://example.test/stream.mp3",
            home_page_url="https://example.test",
        )
        # First call: the create itself with all three params.
        path, params = fake.calls[0]
        assert path == "createInternetRadioStation"
        assert params == {
            "name": "Test FM",
            "streamUrl": "http://example.test/stream.mp3",
            "homepageUrl": "https://example.test",
        }
        # Second call: the read-back to resolve the server-assigned id.
        assert fake.calls[1][0] == "getInternetRadioStations"
        assert result == new_station

    def test_omits_homepage_when_none(self, provider):
        fake = _FakeRequest(
            {
                "createInternetRadioStation": {},
                "getInternetRadioStations": {"internetRadioStations": {}},
            }
        )
        provider._request = fake
        provider.create_internet_radio_station(
            "No Homepage",
            "http://example.test/stream",
        )
        assert fake.calls[0] == (
            "createInternetRadioStation",
            {"name": "No Homepage", "streamUrl": "http://example.test/stream"},
        )

    def test_fallback_shape_when_readback_misses(self, provider):
        # If the read-back doesn't find the row (race / cache) the
        # method still returns the caller-known shape so the UI can
        # render optimistically rather than seeing a None / empty dict.
        provider._request = _FakeRequest(
            {
                "createInternetRadioStation": {},
                "getInternetRadioStations": {"internetRadioStations": {}},
            }
        )
        out = provider.create_internet_radio_station(
            "Phantom",
            "http://example.test/p",
            home_page_url="https://p",
        )
        assert out == {
            "id": "",
            "name": "Phantom",
            "streamUrl": "http://example.test/p",
            "homePageUrl": "https://p",
        }


# ── update_internet_radio_station ────────────────────────────────────


class TestUpdateInternetRadioStation:
    def test_sends_id_and_updated_fields(self, provider):
        updated = {
            "id": "rs-1",
            "name": "BBC 6 (new)",
            "streamUrl": "http://stream.live.vc.bbcmedia.co.uk/bbc_6music_v2",
            "homePageUrl": "https://www.bbc.co.uk/6music",
        }
        fake = _FakeRequest(
            {
                "updateInternetRadioStation": {},
                "getInternetRadioStations": {
                    "internetRadioStations": {
                        "internetRadioStation": [updated],
                    },
                },
            }
        )
        provider._request = fake
        result = provider.update_internet_radio_station(
            "rs-1",
            "BBC 6 (new)",
            "http://stream.live.vc.bbcmedia.co.uk/bbc_6music_v2",
            home_page_url="https://www.bbc.co.uk/6music",
        )
        assert fake.calls[0] == (
            "updateInternetRadioStation",
            {
                "id": "rs-1",
                "name": "BBC 6 (new)",
                "streamUrl": ("http://stream.live.vc.bbcmedia.co.uk/bbc_6music_v2"),
                "homepageUrl": "https://www.bbc.co.uk/6music",
            },
        )
        assert result == updated

    def test_empty_homepage_clears(self, provider):
        # Empty string is explicit-clear (different from None which
        # means "leave alone"). Make sure the param is still sent.
        fake = _FakeRequest(
            {
                "updateInternetRadioStation": {},
                "getInternetRadioStations": {"internetRadioStations": {}},
            }
        )
        provider._request = fake
        provider.update_internet_radio_station(
            "rs-2",
            "SomaFM",
            "http://somafm/stream",
            home_page_url="",
        )
        assert fake.calls[0][1]["homepageUrl"] == ""

    def test_omits_homepage_when_none(self, provider):
        fake = _FakeRequest(
            {
                "updateInternetRadioStation": {},
                "getInternetRadioStations": {"internetRadioStations": {}},
            }
        )
        provider._request = fake
        provider.update_internet_radio_station(
            "rs-2",
            "SomaFM",
            "http://somafm/stream",
        )
        # No homepageUrl key at all when caller passes None.
        assert "homepageUrl" not in fake.calls[0][1]


# ── delete_internet_radio_station ────────────────────────────────────


class TestDeleteInternetRadioStation:
    def test_sends_id_only(self, provider):
        fake = _FakeRequest({"deleteInternetRadioStation": {}})
        provider._request = fake
        result = provider.delete_internet_radio_station("rs-1")
        assert result is None
        assert fake.calls == [("deleteInternetRadioStation", {"id": "rs-1"})]

    def test_propagates_request_errors(self, provider):
        # Non-admin users get a SubsonicError on delete — the caller
        # needs to see it (so the UI can surface "admin required").
        def _boom(*_a, **_kw):
            raise RuntimeError("admin required")

        provider._request = _boom
        with pytest.raises(RuntimeError):
            provider.delete_internet_radio_station("rs-1")
