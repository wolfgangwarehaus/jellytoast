"""Tests for the JellyfinAPI metadata cache.

We don't hit the network — these tests exercise the `_cached` helper
directly with a fake fetch function, so cache hit/miss, deep-copy, LRU
eviction, and invalidation can all be verified deterministically.
"""

import pytest

from modules.jellyfin_api import JellyfinAPI


def _fresh_api():
    """A JellyfinAPI instance with the cache cleared.

    The Settings singleton makes a few QSettings reads in __init__; those
    return defaults under conftest's QStandardPaths test mode and don't
    touch the user's real config.
    """
    api = JellyfinAPI()
    api._meta_cache.clear()
    return api


class _FakeResp:
    """A requests-like response whose body the test controls."""

    def __init__(self, body):
        self._body = body

    def raise_for_status(self):
        pass

    def json(self):
        return self._body


class TestAuthenticateMalformedResponse:
    """A 200 whose body lacks AccessToken / User.Id (captive portal,
    proxy, or a non-Jellyfin server answering the auth POST) must raise a
    clear ValueError — not a cryptic KeyError/TypeError that the login
    view can't translate — and must persist nothing."""

    def _api_returning(self, monkeypatch, body):
        api = _fresh_api()
        monkeypatch.setattr(api.session, "post", lambda *a, **k: _FakeResp(body))
        return api

    @pytest.mark.parametrize(
        "body",
        [
            {},  # nothing
            {"AccessToken": "tok"},  # no User
            {"AccessToken": "tok", "User": {}},  # User without Id
            {"User": {"Id": "u"}},  # no AccessToken
            {"AccessToken": "", "User": {"Id": "u"}},  # empty token
            [],  # non-dict body (JSON array)
            "captive-portal-login-page",  # non-dict body (string)
        ],
    )
    def test_malformed_response_raises_valueerror(self, monkeypatch, body):
        api = self._api_returning(monkeypatch, body)
        with pytest.raises(ValueError):
            api.authenticate("http://server", "user", "pw")

    def test_valid_response_sets_token_and_user(self, isolated_settings, monkeypatch):
        api = self._api_returning(
            monkeypatch, {"AccessToken": "tok", "User": {"Id": "u123"}}
        )
        api.authenticate("http://server", "user", "pw")
        assert api.token == "tok"
        assert api.user_id == "u123"


class TestCacheHitMiss:
    def test_hit_skips_fetch(self):
        api = _fresh_api()
        calls = {"n": 0}

        def fetch():
            calls["n"] += 1
            return [{"Id": "t1"}]

        a = api._cached("album_tracks", "alb1", fetch)
        b = api._cached("album_tracks", "alb1", fetch)
        assert a == b == [{"Id": "t1"}]
        assert calls["n"] == 1

    def test_miss_per_distinct_key(self):
        api = _fresh_api()
        calls = {"n": 0}

        def fetch():
            calls["n"] += 1
            return [{"Id": f"t{calls['n']}"}]

        api._cached("album_tracks", "alb1", fetch)
        api._cached("album_tracks", "alb2", fetch)
        api._cached("artist_albums", "alb1", fetch)
        # Same item_id but different op = different cache entry
        assert calls["n"] == 3


class TestDeepCopySemantics:
    def test_caller_mutation_does_not_pollute_cache(self):
        api = _fresh_api()
        first = api._cached("album_tracks", "alb1", lambda: [{"Id": "t1"}])
        first[0]["AlbumId"] = "INJECTED"  # caller mutates returned list

        second = api._cached("album_tracks", "alb1", lambda: [{"Id": "DIFFERENT"}])
        # If deep copy works, the cached value is intact; the lambda
        # never runs because the cache hit returns a fresh copy.
        assert "AlbumId" not in second[0]
        assert second[0]["Id"] == "t1"

    def test_each_caller_gets_distinct_objects(self):
        api = _fresh_api()
        a = api._cached("album_tracks", "alb1", lambda: [{"Id": "t1"}])
        b = api._cached("album_tracks", "alb1", lambda: [{"Id": "WRONG"}])
        assert a is not b
        assert a[0] is not b[0]


class TestInvalidation:
    def test_invalidate_single_item(self):
        api = _fresh_api()
        calls = {"n": 0}

        def fetch():
            calls["n"] += 1
            return [{"Id": f"t{calls['n']}"}]

        api._cached("album_tracks", "alb1", fetch)
        api._cached("album_tracks", "alb2", fetch)
        api.invalidate_meta_cache("alb1")
        api._cached("album_tracks", "alb1", fetch)  # re-fetches
        api._cached("album_tracks", "alb2", fetch)  # still cached
        assert calls["n"] == 3

    def test_invalidate_all(self):
        api = _fresh_api()
        api._cached("album_tracks", "alb1", lambda: [{"Id": "t1"}])
        api._cached("album_tracks", "alb2", lambda: [{"Id": "t2"}])
        api.invalidate_meta_cache()
        assert len(api._meta_cache) == 0

    def test_invalidate_drops_all_ops_for_id(self):
        api = _fresh_api()
        api._cached("album_tracks", "alb1", lambda: [])
        api._cached("artist_albums", "alb1", lambda: [])
        api._cached("item", "alb1", lambda: {})
        api.invalidate_meta_cache("alb1")
        assert len(api._meta_cache) == 0

    def test_logout_clears_cache(self):
        api = _fresh_api()
        api._cached("album_tracks", "alb1", lambda: [{"Id": "t1"}])
        assert len(api._meta_cache) == 1
        api.logout()
        assert len(api._meta_cache) == 0


class TestLRUEviction:
    def test_evicts_oldest_when_over_max(self, monkeypatch):
        api = _fresh_api()
        # Tighten the cap so the test stays fast.
        monkeypatch.setattr(api, "_META_CACHE_MAX", 3)

        api._cached("item", "a", lambda: {"v": 1})
        api._cached("item", "b", lambda: {"v": 2})
        api._cached("item", "c", lambda: {"v": 3})
        # Fourth insert evicts the oldest (a).
        api._cached("item", "d", lambda: {"v": 4})

        keys = list(api._meta_cache.keys())
        assert ("item", "a") not in keys
        assert ("item", "d") in keys
        assert len(api._meta_cache) == 3

    def test_hit_promotes_to_mru(self, monkeypatch):
        api = _fresh_api()
        monkeypatch.setattr(api, "_META_CACHE_MAX", 3)

        api._cached("item", "a", lambda: {"v": 1})
        api._cached("item", "b", lambda: {"v": 2})
        api._cached("item", "c", lambda: {"v": 3})
        api._cached("item", "a", lambda: {"v": "WRONG"})  # hit → bumps a to MRU

        # Now insert d. 'b' should be the LRU and get evicted, not 'a'.
        api._cached("item", "d", lambda: {"v": 4})
        keys = list(api._meta_cache.keys())
        assert ("item", "a") in keys
        assert ("item", "b") not in keys


class TestGetConnectivityClassification:
    """Cross-layer (converted from a permanently-skipped connectivity
    placeholder): the 4xx-vs-network distinction lives at the provider
    call site, not in connectivity. ``_get`` records note_request_success
    on ANY completed HTTP response — even a 4xx, which still proves the
    server is reachable — and note_request_failure ONLY on a
    RequestException (timeout / DNS fail / connection refused)."""

    class _Resp:
        def __init__(self, status_code, content=b"{}"):
            self.status_code = status_code
            self.content = content

        def json(self):
            return {}

        def raise_for_status(self):
            import requests

            if self.status_code >= 400:
                raise requests.exceptions.HTTPError(str(self.status_code))

    def _spy(self, monkeypatch):
        import modules.offline as offline

        calls = {"success": 0, "failure": 0}
        monkeypatch.setattr(offline, "note_request_success", lambda: calls.__setitem__("success", calls["success"] + 1))
        monkeypatch.setattr(offline, "note_request_failure", lambda: calls.__setitem__("failure", calls["failure"] + 1))
        # auth_* fire downstream of a completed response; stub them so the
        # assertions focus on the network-vs-reachable classification.
        monkeypatch.setattr(offline, "note_auth_failure", lambda: None)
        monkeypatch.setattr(offline, "note_auth_success", lambda: None)
        return calls

    def test_4xx_is_reachable_not_a_network_failure(self, monkeypatch):
        import requests

        api = _fresh_api()
        api.server_url = "http://server"
        calls = self._spy(monkeypatch)
        monkeypatch.setattr(api.session, "get", lambda *a, **k: self._Resp(404))
        with pytest.raises(requests.exceptions.HTTPError):
            api._get("/Items")
        assert calls["success"] == 1  # server reachable
        assert calls["failure"] == 0  # NOT a network failure

    def test_requestexception_is_a_network_failure(self, monkeypatch):
        import requests

        api = _fresh_api()
        api.server_url = "http://server"
        calls = self._spy(monkeypatch)

        def _boom(*a, **k):
            raise requests.exceptions.ConnectionError("refused")

        monkeypatch.setattr(api.session, "get", _boom)
        with pytest.raises(requests.exceptions.ConnectionError):
            api._get("/Items")
        assert calls["failure"] == 1  # network failure recorded
        assert calls["success"] == 0
