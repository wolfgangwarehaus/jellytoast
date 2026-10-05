"""Real-implementation request-shape tests for the Jellyfin client.

Streaming + playback reporting are the other documented moat (bit-perfect
direct play, session-attributed scrobble reporting). The existing
``test_jellyfin_api.py`` only covers the metadata LRU cache; this module
drives the *real* ``JellyfinAPI`` request builders and asserts on the
observable wire shape (URL + params + body), plus the provider-level
delegation in ``jellytoast/providers/jellyfin.py``.

No network: ``session`` is stubbed (the ``test_tag_editing.py`` pattern)
or ``_get`` / ``_post`` are replaced with recording mocks so we inspect
exactly what would have gone out.
"""

from __future__ import annotations

from unittest.mock import MagicMock
from urllib.parse import parse_qs, urlparse

import pytest

from jellytoast.jellyfin_api import CLIENT_NAME, DEVICE_NAME, JellyfinAPI
from jellytoast.providers.jellyfin import JellyfinProvider


def _api() -> JellyfinAPI:
    """A JellyfinAPI authed for path construction, cache cleared.

    Mirrors ``test_tag_editing._stub_api`` but without pre-stubbing the
    HTTP layer — each test wires the mock it needs (``_get`` / ``_post``
    / ``session.*``).
    """
    api = JellyfinAPI()
    api._meta_cache.clear()
    api.server_url = "http://jf.test"
    api.user_id = "u1"
    api.token = "tok"
    # Unknown server version → modern routes; the persisted value would
    # otherwise leak between tests (and runs) via the test QSettings.
    api.settings.jellyfin_server_version = ""
    api.server_version = None
    return api


def _query(url: str) -> dict:
    return {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}


# ── Auth header shape ──────────────────────────────────────────────


class TestAuthHeader:
    def test_carries_client_device_and_token(self):
        api = _api()
        h = api.auth_header
        assert h.startswith("MediaBrowser ")
        assert f'Client="{CLIENT_NAME}"' in h
        assert f'Device="{DEVICE_NAME}"' in h
        assert f'DeviceId="{api.device_id}"' in h
        assert 'Token="tok"' in h

    def test_no_token_clause_when_unauthenticated(self):
        api = _api()
        api.token = ""
        assert "Token=" not in api.auth_header

    def test_headers_sets_standard_auth_and_json_content_type(self):
        api = _api()
        headers = api._headers()
        assert headers["Authorization"] == api.auth_header
        assert headers["Content-Type"] == "application/json"
        assert headers["Accept-Language"]

    def test_no_legacy_auth_header(self):
        # Jellyfin 12 ignores X-Emby-Authorization by default — sending it
        # instead of Authorization means every request comes back 401.
        assert not any(k.lower().startswith("x-emby") for k in _api()._headers())

    def test_built_urls_use_apikey_not_legacy_api_key(self):
        # Jellyfin 12 ignores the lowercase ``api_key`` query param by
        # default; ``ApiKey`` is the supported spelling (10.8+).
        api = _api()
        api.settings = MagicMock()
        api.settings.audio_quality = "original"
        api.settings.device_id = "dev"
        urls = [
            api.get_audio_stream_url("i1"),
            api.get_audio_stream_url("i1", quality="320"),
            api.get_video_stream_url("i1"),
            api.get_image_url("i1"),
        ]
        prov = JellyfinProvider.__new__(JellyfinProvider)
        prov.api = api
        urls.append(prov.get_audio_transcode_url("i1"))
        for url in urls:
            q = _query(url)
            assert q.get("ApiKey") == "tok", url
            assert "api_key" not in q, url


# ── get_audio_stream_url ───────────────────────────────────────────


class TestAudioStreamUrl:
    def test_original_is_static_direct_play(self):
        api = _api()
        api.settings = MagicMock()
        api.settings.audio_quality = "original"
        # device_id is a property reading settings.device_id; pin it to
        # a concrete string so the embedded value is assertable.
        api.settings.device_id = "dev-xyz"
        url = api.get_audio_stream_url("item5")
        assert urlparse(url).path == "/Audio/item5/stream"
        q = _query(url)
        # Session-binding params so the server attributes the bytes to
        # the same session our /Sessions/Playing reports use.
        assert q["ApiKey"] == "tok"
        assert q["UserId"] == "u1"
        assert q["DeviceId"] == "dev-xyz"
        assert q["MediaSourceId"] == "item5"
        assert q["static"] == "true"

    def test_quality_override_uses_transcode_endpoint(self):
        api = _api()
        api.settings = MagicMock()
        # The override arg wins over the ambient setting.
        api.settings.audio_quality = "original"
        url = api.get_audio_stream_url("item5", quality="192")
        assert urlparse(url).path == "/Audio/item5/stream.mp3"
        q = _query(url)
        # 192 kbps → 192000 bps; codec pinned to mp3.
        assert q["MaxStreamingBitrate"] == "192000"
        assert q["AudioCodec"] == "mp3"
        assert q["MediaSourceId"] == "item5"

    def test_non_numeric_quality_defaults_to_320k(self):
        api = _api()
        api.settings = MagicMock()
        api.settings.audio_quality = "garbage"
        url = api.get_audio_stream_url("item5")
        assert _query(url)["MaxStreamingBitrate"] == "320000"

    def test_empty_quality_is_direct_play_not_transcode(self):
        # An empty/unset audio_quality must normalize to "original"
        # (direct play), matching the Subsonic provider — NOT fall through
        # to int("") → ValueError → a forced 320k transcode. Cross-provider
        # parity guard: empty should never silently degrade Jellyfin audio.
        api = _api()
        api.settings = MagicMock()
        api.settings.audio_quality = ""
        api.settings.device_id = "dev-xyz"
        url = api.get_audio_stream_url("item5")
        assert urlparse(url).path == "/Audio/item5/stream"
        assert _query(url)["static"] == "true"

    def test_provider_delegates_to_api(self):
        provider = JellyfinProvider.__new__(JellyfinProvider)
        fake_api = MagicMock()
        fake_api.get_audio_stream_url.return_value = "http://stream"
        provider.api = fake_api
        out = provider.get_audio_stream_url("it1", quality="320")
        # Quality threads through to the API call by keyword.
        fake_api.get_audio_stream_url.assert_called_once_with("it1", quality="320")
        assert out == "http://stream"


# ── Playback reporting bodies ──────────────────────────────────────


class TestPlaybackReporting:
    def test_start_body_shape(self):
        api = _api()
        api._post = MagicMock()
        api.report_playback_start(
            "it1",
            position_ticks=500,
            play_session_id="ps1",
            play_method="Transcode",
        )
        path, body = api._post.call_args.args
        assert path == "/Sessions/Playing"
        assert body == {
            "ItemId": "it1",
            "MediaSourceId": "it1",  # defaults to ItemId for music
            "PlaySessionId": "ps1",
            "CanSeek": True,
            "PlayMethod": "Transcode",
            "PositionTicks": 500,
        }

    def test_progress_body_includes_paused_and_event(self):
        api = _api()
        api._post = MagicMock()
        api.report_playback_progress(
            "it1",
            1000,
            is_paused=True,
            play_session_id="ps1",
            event_name="pause",
        )
        path, body = api._post.call_args.args
        assert path == "/Sessions/Playing/Progress"
        assert body["ItemId"] == "it1"
        assert body["PositionTicks"] == 1000
        assert body["IsPaused"] is True
        assert body["PlaySessionId"] == "ps1"
        # PlayMethod defaults to DirectStream.
        assert body["PlayMethod"] == "DirectStream"
        # EventName only present when supplied.
        assert body["EventName"] == "pause"

    def test_progress_omits_event_when_blank(self):
        api = _api()
        api._post = MagicMock()
        api.report_playback_progress("it1", 0, play_session_id="ps1")
        _path, body = api._post.call_args.args
        assert "EventName" not in body

    def test_stopped_body_shape(self):
        api = _api()
        api._post = MagicMock()
        api.report_playback_stopped("it1", 2000, play_session_id="ps1")
        path, body = api._post.call_args.args
        assert path == "/Sessions/Playing/Stopped"
        assert body == {
            "ItemId": "it1",
            "MediaSourceId": "it1",
            "PlaySessionId": "ps1",
            "PlayMethod": "DirectStream",
            "PositionTicks": 2000,
        }

    def test_explicit_media_source_id_overrides_item_id(self):
        api = _api()
        api._post = MagicMock()
        api.report_playback_start("it1", media_source_id="src9")
        _path, body = api._post.call_args.args
        assert body["MediaSourceId"] == "src9"

    def test_mark_played_endpoint(self):
        api = _api()
        api._post = MagicMock()
        api.mark_played("it1")
        assert api._post.call_args.args == ("/UserPlayedItems/it1",)


# ── Provider-level playback delegation ─────────────────────────────


class TestProviderPlaybackDelegation:
    @staticmethod
    def _provider():
        provider = JellyfinProvider.__new__(JellyfinProvider)
        provider.api = MagicMock()
        return provider

    def test_start_delegates(self):
        p = self._provider()
        p.report_playback_start(
            "it1", position_ticks=10, play_session_id="ps", play_method="Transcode"
        )
        # Provider passes item_id + position_ticks positionally and the
        # rest by keyword to the API.
        p.api.report_playback_start.assert_called_once_with(
            "it1",
            10,
            play_session_id="ps",
            play_method="Transcode",
            media_source_id="",
        )

    def test_stopped_delegates(self):
        p = self._provider()
        p.report_playback_stopped("it1", 50, play_session_id="ps")
        p.api.report_playback_stopped.assert_called_once_with(
            "it1",
            50,
            play_session_id="ps",
            play_method="DirectStream",
            media_source_id="",
        )


# ── Representative request-builder sample ──────────────────────────


class TestRequestBuilders:
    """A representative slice of the ~20 GET builders. The assertions
    pin the endpoint path and the params that drive server behavior —
    NOT the full Fields strings (those are presentation tuning that
    shifts as views evolve). Where a Fields value is load-bearing for a
    feature it gets its own targeted assertion."""

    def test_get_libraries(self):
        api = _api()
        api._get = MagicMock(return_value={"Items": [{"Id": "v"}]})
        out = api.get_libraries()
        api._get.assert_called_once_with("/UserViews")
        assert out == [{"Id": "v"}]

    def test_get_items_path_and_core_params(self):
        api = _api()
        api._get = MagicMock(return_value={"Items": []})
        api.get_items(
            parent_id="p1",
            item_type="Audio",
            genre_ids="g1",
            filters="IsFavorite",
            years="2013",
            limit=50,
            recursive=True,
        )
        path, params = api._get.call_args.args[0], api._get.call_args.args[1]
        assert path == "/Items"
        assert params["ParentId"] == "p1"
        assert params["IncludeItemTypes"] == "Audio"
        assert params["GenreIds"] == "g1"
        assert params["Filters"] == "IsFavorite"
        assert params["Years"] == "2013"
        assert params["Limit"] == 50
        assert params["Recursive"] is True
        # Genres in Fields is load-bearing for smart-playlist seeding.
        assert "Genres" in params["Fields"]

    def test_get_items_omits_optional_params_when_blank(self):
        api = _api()
        api._get = MagicMock(return_value={"Items": []})
        api.get_items()
        params = api._get.call_args.args[1]
        for opt in ("ParentId", "IncludeItemTypes", "GenreIds", "Filters", "Years"):
            assert opt not in params

    def test_get_artists(self):
        api = _api()
        api._get = MagicMock(return_value={"Items": []})
        api.get_artists(limit=10, start_index=5)
        path, params = api._get.call_args.args
        assert path == "/Artists/AlbumArtists"
        assert params["UserId"] == "u1"
        assert params["Limit"] == 10
        assert params["StartIndex"] == 5

    def test_get_artist_albums_path_and_filter(self):
        api = _api()
        api._get = MagicMock(return_value={"Items": []})
        api.get_artist_albums("art1")
        path, params = api._get.call_args.args
        assert path == "/Items"
        assert params["AlbumArtistIds"] == "art1"
        assert params["IncludeItemTypes"] == "MusicAlbum"
        assert params["Recursive"] is True

    def test_get_album_tracks_sorted_by_disc_then_track(self):
        api = _api()
        api._get = MagicMock(return_value={"Items": []})
        api.get_album_tracks("alb1")
        path, params = api._get.call_args.args
        assert path == "/Items"
        assert params["ParentId"] == "alb1"
        # Disc → track → name ordering keeps multi-disc albums in order.
        assert params["SortBy"] == "ParentIndexNumber,IndexNumber,SortName"

    def test_get_playlist_items_includes_album_id_field(self):
        api = _api()
        api._get = MagicMock(return_value={"Items": []})
        api.get_playlist_items("pl1")
        path, params = api._get.call_args.args
        assert path == "/Playlists/pl1/Items"
        # AlbumId is required so per-track cover art resolves from the
        # native album (playlist tracks span many albums).
        assert "AlbumId" in params["Fields"]

    def test_get_random_audio_items_random_sort(self):
        api = _api()
        api._get = MagicMock(return_value={"Items": []})
        api.get_random_audio_items("lib", limit=300)
        path, params = api._get.call_args.args
        assert path == "/Items"
        assert params["ParentId"] == "lib"
        assert params["IncludeItemTypes"] == "Audio"
        assert params["SortBy"] == "Random"
        assert params["Limit"] == 300

    def test_get_genres(self):
        api = _api()
        api._get = MagicMock(return_value={"Items": []})
        api.get_genres()
        path, params = api._get.call_args.args
        assert path == "/MusicGenres"
        assert params["IncludeItemTypes"] == "Audio,MusicAlbum"
        assert params["Recursive"] is True

    def test_search_uses_search_term(self):
        api = _api()
        api._get = MagicMock(return_value={"Items": []})
        api.search("daft punk", limit=20, item_types="Audio")
        path, params = api._get.call_args.args
        assert path == "/Items"
        assert params["SearchTerm"] == "daft punk"
        assert params["IncludeItemTypes"] == "Audio"
        assert params["Recursive"] is True
        assert params["Limit"] == 20

    def test_get_resume_items_media_type_filter(self):
        api = _api()
        api._get = MagicMock(return_value={"Items": []})
        api.get_resume_items(media_type="Audio")
        path, params = api._get.call_args.args
        assert path == "/UserItems/Resume"
        assert params["MediaTypes"] == "Audio"

    def test_get_latest_media_parent_id(self):
        api = _api()
        api._get = MagicMock(return_value=[])
        api.get_latest_media("lib1", limit=8)
        path, params = api._get.call_args.args
        assert path == "/Items/Latest"
        assert params["ParentId"] == "lib1"
        assert params["Limit"] == 8

    def test_get_item_uses_per_user_endpoint_and_caches(self):
        api = _api()
        api._get = MagicMock(return_value={"Id": "it1"})
        api.get_item("it1")
        api._get.assert_called_once_with("/Items/it1")
        # Second call is served from the meta cache, not the network.
        api.get_item("it1")
        assert api._get.call_count == 1

    def test_get_lyrics_swallows_errors(self):
        api = _api()
        api._get = MagicMock(side_effect=RuntimeError("no lyrics"))
        # get_lyrics returns None rather than propagating.
        assert api.get_lyrics("it1") is None


# ── Favorite / unplayed mutations (mixed POST + DELETE) ────────────


class TestMutations:
    def test_favorite_on_posts(self):
        api = _api()
        api._post = MagicMock()
        api.toggle_favorite("it1", True)
        assert api._post.call_args.args[0] == "/UserFavoriteItems/it1"

    def test_favorite_off_deletes(self):
        api = _api()
        api._post = MagicMock()
        # strict=True (#234 finding 8) checks the response status — give
        # the mocked DELETE a real 2xx so the raise path stays quiet.
        api.session.delete = MagicMock(return_value=MagicMock(status_code=204))
        api.toggle_favorite("it1", False)
        url = api.session.delete.call_args.args[0]
        assert url == "http://jf.test/UserFavoriteItems/it1"

    def test_favorite_toggle_invalidates_cache(self):
        api = _api()
        api._post = MagicMock()
        api._meta_cache[("item", "it1")] = {"cached": True}
        api.toggle_favorite("it1", True)
        assert ("item", "it1") not in api._meta_cache

    def test_mark_played_invalidates_cache(self):
        api = _api()
        api._post = MagicMock()
        api._meta_cache[("item", "it1")] = {"cached": True}
        api.mark_played("it1")
        # The cached snapshot's UserData.Played/PlayCount is now stale.
        assert ("item", "it1") not in api._meta_cache

    def test_mark_unplayed_invalidates_cache(self):
        api = _api()
        api.session.delete = MagicMock()
        api._meta_cache[("item", "it1")] = {"cached": True}
        api.mark_unplayed("it1")
        assert ("item", "it1") not in api._meta_cache

    def test_mark_unplayed_deletes_played_item(self):
        api = _api()
        api.session.delete = MagicMock()
        api.mark_unplayed("it1")
        url = api.session.delete.call_args.args[0]
        assert url == "http://jf.test/UserPlayedItems/it1"

    def test_mark_unplayed_swallows_network_error(self):
        api = _api()
        api.session.delete = MagicMock(
            side_effect=__import__("requests").exceptions.ConnectionError("down")
        )
        # mark_unplayed is best-effort — a network failure must not raise.
        api.mark_unplayed("it1")


# ── _get / _post connectivity classification ───────────────────────


class TestGetPostReachability:
    def _resp(self, status: int, content: bytes = b"{}"):
        r = MagicMock()
        r.status_code = status
        r.content = content
        r.json.return_value = {}
        r.raise_for_status = MagicMock()
        return r

    def test_get_sends_auth_headers_and_params(self):
        api = _api()
        api.session.get = MagicMock(return_value=self._resp(200))
        api._get("/Users/u1/Views", {"Limit": 5})
        kw = api.session.get.call_args
        assert kw.args[0] == "http://jf.test/Users/u1/Views"
        assert kw.kwargs["params"] == {"Limit": 5}
        assert kw.kwargs["headers"]["Authorization"] == api.auth_header

    def test_get_401_feeds_auth_failure(self, monkeypatch):
        import jellytoast.offline as _offline

        calls = {"auth_fail": 0, "req_ok": 0}
        monkeypatch.setattr(
            _offline, "note_auth_failure", lambda: calls.__setitem__("auth_fail", 1)
        )
        monkeypatch.setattr(
            _offline, "note_request_success", lambda: calls.__setitem__("req_ok", 1)
        )
        monkeypatch.setattr(_offline, "note_auth_success", lambda: None)
        api = _api()
        resp = self._resp(401)
        # 401 is still a reachable server → raise_for_status throws but
        # the auth-failure tracker is fed first.
        import requests

        resp.raise_for_status = MagicMock(
            side_effect=requests.exceptions.HTTPError("401")
        )
        api.session.get = MagicMock(return_value=resp)
        with pytest.raises(requests.exceptions.HTTPError):
            api._get("/x")
        assert calls["auth_fail"] == 1
        assert calls["req_ok"] == 1  # server WAS reached

    def test_post_network_error_returns_none(self, monkeypatch):
        import jellytoast.offline as _offline

        monkeypatch.setattr(_offline, "note_request_failure", lambda: None)
        api = _api()
        api.session.post = MagicMock(
            side_effect=__import__("requests").exceptions.ConnectionError("down")
        )
        # _post swallows network errors and returns None (no-throw contract).
        assert api._post("/x", {"a": 1}) is None


# ── Route family (10.9+ user-implicit vs legacy /Users/{id}/…) ─────


class TestRouteFamily:
    @pytest.mark.parametrize("version", [None, (10, 9, 0), (10, 11, 6), (12, 1, 0)])
    def test_modern_paths_pass_through(self, version):
        api = _api()
        api.server_version = version
        for method, path in [
            ("GET", "/UserViews"),
            ("GET", "/Items"),
            ("GET", "/Items/it1"),
            ("POST", "/UserFavoriteItems/it1"),
            ("DELETE", "/UserPlayedItems/it1"),
        ]:
            assert api._route(method, path) == path

    @pytest.mark.parametrize(
        "method,modern,legacy",
        [
            ("GET", "/UserViews", "/Users/u1/Views"),
            ("GET", "/UserItems/Resume", "/Users/u1/Items/Resume"),
            ("GET", "/Items/Latest", "/Users/u1/Items/Latest"),
            ("GET", "/Items", "/Users/u1/Items"),
            ("GET", "/Items/it1", "/Users/u1/Items/it1"),
            ("POST", "/UserPlayedItems/it1", "/Users/u1/PlayedItems/it1"),
            ("DELETE", "/UserPlayedItems/it1", "/Users/u1/PlayedItems/it1"),
            ("POST", "/UserFavoriteItems/it1", "/Users/u1/FavoriteItems/it1"),
            ("DELETE", "/UserFavoriteItems/it1", "/Users/u1/FavoriteItems/it1"),
        ],
    )
    def test_pre_10_9_maps_back_to_legacy(self, method, modern, legacy):
        api = _api()
        api.server_version = (10, 8, 13)
        assert api._route(method, modern) == legacy

    def test_pre_10_9_leaves_non_user_routes_alone(self):
        api = _api()
        api.server_version = (10, 8, 13)
        # POST /Items/{id} is the metadata update — exists on every version.
        assert api._route("POST", "/Items/it1") == "/Items/it1"
        assert api._route("GET", "/Items/it1/Similar") == "/Items/it1/Similar"
        assert api._route("GET", "/Audio/it1/Lyrics") == "/Audio/it1/Lyrics"

    def test_get_wrapper_applies_route(self):
        api = _api()
        api.server_version = (10, 8, 13)
        resp = MagicMock(status_code=200, content=b"{}")
        resp.json.return_value = {}
        api.session.get = MagicMock(return_value=resp)
        api._get("/UserViews")
        assert api.session.get.call_args.args[0] == "http://jf.test/Users/u1/Views"

    def test_server_info_sets_and_persists_version(self):
        api = _api()
        api._note_server_info({"Version": "10.8.13"})
        assert api.server_version == (10, 8, 13)
        assert api.settings.jellyfin_server_version == "10.8.13"
        try:
            api._note_server_info({})  # garbage keeps the known version
            assert api.server_version == (10, 8, 13)
        finally:
            api.settings.jellyfin_server_version = ""

    def test_logout_forgets_version(self):
        api = _api()
        api._note_server_info({"Version": "10.8.13"})
        api.logout()
        assert api.server_version is None
        assert api.settings.jellyfin_server_version == ""


class TestArtistsFallback:
    def _http_error(self, status):
        import requests

        resp = MagicMock(status_code=status)
        return requests.HTTPError(f"{status}", response=resp)

    def test_uses_album_artists_route_while_it_exists(self):
        api = _api()
        api._get = MagicMock(return_value={"Items": [{"Id": "a1"}]})
        assert api.get_artists(parent_id="lib") == [{"Id": "a1"}]
        assert api._get.call_args.args[0] == "/Artists/AlbumArtists"

    def test_falls_back_to_items_once_route_is_gone_and_latches(self):
        api = _api()
        calls = []

        def fake_get(path, params=None, **_kw):
            calls.append((path, dict(params or {})))
            if path == "/Artists/AlbumArtists":
                raise self._http_error(404)
            return {"Items": [{"Id": "a1"}]}

        api._get = fake_get
        assert api.get_artists(parent_id="lib") == [{"Id": "a1"}]
        path, params = calls[-1]
        assert path == "/Items"
        assert params["IncludeItemTypes"] == "MusicArtist"
        assert params["Recursive"] is True
        assert params["ParentId"] == "lib"
        calls.clear()
        api.get_artists()
        assert [p for p, _ in calls] == ["/Items"]  # no second probe

    def test_other_http_errors_propagate(self):
        api = _api()
        api._get = MagicMock(side_effect=self._http_error(500))
        with pytest.raises(__import__("requests").HTTPError):
            api.get_artists()
        assert api._album_artists_gone is False


class TestGenres:
    def test_queries_genres_per_music_library_and_merges(self):
        api = _api()
        views = {"Items": [
            {"Id": "m1", "CollectionType": "music"},
            {"Id": "mv", "CollectionType": "movies"},
            {"Id": "m2", "CollectionType": "music"},
        ]}
        per_lib = {
            "m1": [{"Id": "g-rock", "Name": "Rock"}, {"Id": "g-jazz", "Name": "Jazz"}],
            "m2": [{"Id": "g-rock", "Name": "Rock"}, {"Id": "g-amb", "Name": "Ambient"}],
        }
        seen = []

        def fake_get(path, params=None, **_kw):
            seen.append((path, (params or {}).get("ParentId")))
            if path == "/UserViews":
                return views
            assert path == "/Genres"
            return {"Items": per_lib[params["ParentId"]]}

        api._get = fake_get
        names = [g["Name"] for g in api.get_genres()]
        assert names == ["Ambient", "Jazz", "Rock"]  # merged, deduped, sorted
        assert ("/Genres", "mv") not in seen

    def test_falls_back_to_music_genres_without_music_views(self):
        api = _api()

        def fake_get(path, params=None, **_kw):
            if path == "/UserViews":
                return {"Items": [{"Id": "x"}]}
            assert path == "/MusicGenres"
            return {"Items": [{"Id": "g1"}]}

        api._get = fake_get
        assert api.get_genres() == [{"Id": "g1"}]
