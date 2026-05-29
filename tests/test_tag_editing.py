"""Tests for the tag-editing backend.

Covers the capability boolean on the provider abstraction, the
Jellyfin GET-merge-POST-with-LockedFields workaround for
jellyfin/jellyfin#10724, and the explicit Subsonic refusal.

No network: ``JellyfinAPI._get`` and ``session.post`` are stubbed so
the test inspects the exact body we'd send to the server.
"""

from __future__ import annotations

from typing import Any, Dict
from unittest.mock import MagicMock

import pytest

from modules.jellyfin_api import JellyfinAPI
from modules.providers.base import MediaProvider
from modules.providers.jellyfin import JellyfinProvider
from modules.providers.subsonic import SubsonicProvider

# ── Capability boolean ─────────────────────────────────────────────


class TestCapabilityFlag:
    def test_base_default_is_false(self):
        assert MediaProvider.can_edit_metadata is False

    def test_jellyfin_overrides_true(self):
        assert JellyfinProvider.can_edit_metadata is True

    def test_subsonic_keeps_false(self):
        assert SubsonicProvider.can_edit_metadata is False


# ── Subsonic refuses ───────────────────────────────────────────────


class TestSubsonicRefuses:
    def test_update_raises_with_descriptive_message(self):
        # A bare instance is fine — we only call the method, no network.
        p = SubsonicProvider.__new__(SubsonicProvider)
        with pytest.raises(NotImplementedError) as exc:
            p.update_track_metadata("any-id", {"Name": "x"})
        msg = str(exc.value).lower()
        assert "subsonic" in msg or "edit" in msg

    def test_base_default_raises(self):
        # The base impl is what any future provider inherits until it
        # opts in — confirm the message names the editing-unsupported
        # case so log triage isn't a guessing game.
        class _Stub(MediaProvider):
            # Minimal stub: every abstract attribute filled in with
            # something callable / property-shaped enough to instantiate.
            kind = "stub"
            server_url = ""
            user_id = ""
            access_token = ""
            device_id = ""
            is_authenticated = False

            def probe(self, *a, **kw):
                return None

            def authenticate(self, *a, **kw): ...
            def verify_session(self):
                return False

            def server_logout(self):
                return False

            def with_url(self, *a, **kw):
                return self

            def get_libraries(self):
                return []

            def get_items(self, *a, **kw):
                return {}

            def get_item(self, *a, **kw):
                return {}

            def get_album_tracks(self, *a, **kw):
                return []

            def get_artist_albums(self, *a, **kw):
                return []

            def get_artists(self, *a, **kw):
                return []

            def get_playlist_items(self, *a, **kw):
                return []

            def get_genres(self):
                return []

            def get_resume_items(self, *a, **kw):
                return []

            def get_latest_media(self, *a, **kw):
                return []

            def search(self, *a, **kw):
                return []

            def search_all(self, *a, **kw):
                return {}

            def get_random_audio_items(self, *a, **kw):
                return []

            def get_audio_stream_url(self, *a, **kw):
                return ""

            def get_video_stream_url(self, *a, **kw):
                return ""

            def get_audio_transcode_url(self, *a, **kw):
                return ""

            def get_image_url(self, *a, **kw):
                return ""

            def report_playback_start(self, *a, **kw): ...
            def report_playback_progress(self, *a, **kw): ...
            def report_playback_stopped(self, *a, **kw): ...
            def mark_played(self, *a, **kw): ...
            def mark_unplayed(self, *a, **kw): ...
            def toggle_favorite(self, *a, **kw): ...
            def get_lyrics(self, *a, **kw):
                return None

            def invalidate_meta_cache(self, *a, **kw): ...

        with pytest.raises(NotImplementedError):
            _Stub().update_track_metadata("x", {"Name": "y"})


# ── Cover-art stub ─────────────────────────────────────────────────


class TestCoverArtSubsonicRefuses:
    def test_subsonic_cover_upload_raises(self):
        # Subsonic's API has no clean cover-upload endpoint; it inherits
        # the base stub and raises NotImplementedError.
        p = SubsonicProvider.__new__(SubsonicProvider)
        with pytest.raises(NotImplementedError):
            p.upload_cover_art("id", b"\x89PNG", "image/png")

    def test_base_default_cover_upload_raises(self):
        provider = JellyfinProvider.__new__(JellyfinProvider)
        # Sanity: the base provides a stub; only Jellyfin overrides it.
        assert MediaProvider.upload_cover_art is not type(provider).upload_cover_art


def _stub_image_api() -> tuple[JellyfinAPI, MagicMock]:
    """A JellyfinAPI wired for image-upload tests: ``session.post``
    stubbed to return a 204-style success. Returns the post-mock so
    the test can assert on URL / headers / body."""
    api = JellyfinAPI()
    api._meta_cache.clear()
    api.server_url = "http://example.test"
    api.user_id = "u1"
    api.token = "tok"

    fake_response = MagicMock()
    fake_response.status_code = 204
    fake_response.content = b""
    fake_response.raise_for_status = MagicMock()
    post_mock = MagicMock(return_value=fake_response)
    api.session.post = post_mock  # type: ignore[assignment]
    return api, post_mock


class TestJellyfinCoverUpload:
    def test_posts_to_primary_image_endpoint(self):
        api, post_mock = _stub_image_api()
        provider = JellyfinProvider.__new__(JellyfinProvider)
        provider.api = api

        provider.upload_cover_art("abc123", b"\x89PNGbody", "image/png")

        post_mock.assert_called_once()
        url = post_mock.call_args.args[0]
        assert url == "http://example.test/Items/abc123/Images/Primary"

    def test_body_is_base64_encoded(self):
        import base64

        api, post_mock = _stub_image_api()
        raw = b"\xff\xd8\xff\xe0rawjpegbytes"
        api.upload_primary_image("trk", raw, "image/jpeg")

        body = post_mock.call_args.kwargs["data"]
        # Body is the base64 encoding of the raw image bytes — NOT
        # multipart form data, NOT the raw bytes themselves.
        assert body == base64.b64encode(raw)
        assert base64.b64decode(body) == raw
        assert "files" not in post_mock.call_args.kwargs

    def test_content_type_is_image_mime(self):
        api, post_mock = _stub_image_api()
        api.upload_primary_image("trk", b"img", "image/jpeg")

        headers = post_mock.call_args.kwargs["headers"]
        # The image endpoint wants the picture's own mime type, never
        # the JSON Content-Type _headers() defaults to.
        assert headers["Content-Type"] == "image/jpeg"
        assert "X-Emby-Authorization" in headers

    def test_non_2xx_response_raises(self):
        import requests

        api, post_mock = _stub_image_api()
        fail = MagicMock()
        fail.status_code = 500
        fail.content = b"server error"
        fail.raise_for_status = MagicMock(
            side_effect=requests.exceptions.HTTPError("500")
        )
        post_mock.return_value = fail

        with pytest.raises(requests.exceptions.HTTPError):
            api.upload_primary_image("trk", b"img", "image/png")

    def test_network_error_propagates(self):
        import requests

        api, post_mock = _stub_image_api()
        post_mock.side_effect = requests.exceptions.ConnectionError("down")

        with pytest.raises(requests.exceptions.RequestException):
            api.upload_primary_image("trk", b"img", "image/png")

    def test_meta_cache_invalidated_on_success(self):
        api, _post = _stub_image_api()
        api._meta_cache[("op", "trk")] = {"cached": True}
        api.upload_primary_image("trk", b"img", "image/png")
        assert ("op", "trk") not in api._meta_cache


# ── Jellyfin happy path + LockedFields ────────────────────────────


def _stub_api(server_item: Dict[str, Any]) -> tuple[JellyfinAPI, MagicMock]:
    """A JellyfinAPI wired with stubbed ``_get`` and ``session.post``.

    ``server_item`` is what the GET returns; the returned MagicMock is
    the post-stub so the test can assert on the body that would have
    been sent. The stub returns a 204-style empty 200 response.
    """
    api = JellyfinAPI()
    api._meta_cache.clear()
    # Pretend we're authenticated so server_url / user_id are valid for
    # path construction (the URLs are never actually fetched).
    api.server_url = "http://example.test"
    api.user_id = "u1"
    api.token = "tok"

    api._get = MagicMock(return_value=server_item)  # type: ignore[assignment]

    fake_response = MagicMock()
    fake_response.status_code = 200
    fake_response.content = b""
    fake_response.raise_for_status = MagicMock()
    post_mock = MagicMock(return_value=fake_response)
    api.session.post = post_mock  # type: ignore[assignment]
    return api, post_mock


class TestJellyfinHappyPath:
    def test_single_edit_merges_and_locks(self):
        server_item = {
            "Id": "abc123",
            "Name": "Old Name",
            "Album": "Some Album",
            "ProductionYear": 1999,
        }
        api, post_mock = _stub_api(server_item)

        result = api.update_item_metadata("abc123", {"Name": "New Name"})

        # The GET hit the per-user item endpoint.
        api._get.assert_called_once_with("/Users/u1/Items/abc123")

        # The POST hit /Items/{id} (no /Users prefix — this is the
        # update endpoint, not the read endpoint).
        post_mock.assert_called_once()
        url = post_mock.call_args.args[0]
        assert url == "http://example.test/Items/abc123"

        body = post_mock.call_args.kwargs["json"]
        # Merge preserved unrelated fields.
        assert body["Album"] == "Some Album"
        assert body["ProductionYear"] == 1999
        # The edit was applied.
        assert body["Name"] == "New Name"
        # Name's LockedFields entry was added.
        assert "Name" in body["LockedFields"]

        # Returned dict reflects the same merged body.
        assert result["Name"] == "New Name"
        assert "Name" in result["LockedFields"]

    def test_multiple_edits_each_lock(self):
        server_item = {
            "Id": "abc123",
            "Name": "Old",
            "Artists": ["Old Artist"],
            "Genres": ["Old Genre"],
            "IndexNumber": 1,
            "ProductionYear": 2000,
        }
        api, post_mock = _stub_api(server_item)

        edits = {
            "Name": "New",
            "Artists": ["A1", "A2"],
            "Genres": ["G1"],
            "IndexNumber": 7,
            "ProductionYear": 2010,
            "Album": "New Album",
            "AlbumArtist": "Various",
        }
        api.update_item_metadata("abc123", edits)

        body = post_mock.call_args.kwargs["json"]
        locked = body["LockedFields"]

        # Every editable field has its lock-name in LockedFields. We
        # check membership rather than equality because some fields
        # collapse onto the same lock-name (Artists + AlbumArtist
        # both lock via "Cast", per Jellyfin's MetadataField enum).
        for lock_name in ("Name", "Cast", "Genres", "IndexNumber", "ProductionYear"):
            assert lock_name in locked, f"Expected {lock_name!r} in LockedFields, got {locked!r}"

        # Every edit value made it into the body.
        for key, value in edits.items():
            assert body[key] == value

    def test_existing_locked_fields_preserved(self):
        # The user had previously locked Overview + Tags; our edit
        # touches Name. Those pre-existing locks must survive.
        server_item = {
            "Id": "abc",
            "Name": "Old",
            "LockedFields": ["Overview", "Tags"],
        }
        api, post_mock = _stub_api(server_item)

        api.update_item_metadata("abc", {"Name": "New"})

        body = post_mock.call_args.kwargs["json"]
        locked = body["LockedFields"]
        assert "Overview" in locked
        assert "Tags" in locked
        assert "Name" in locked

    def test_lock_name_not_duplicated_on_re_edit(self):
        # Re-editing a field that's already locked must not push a
        # duplicate entry into LockedFields.
        server_item = {
            "Id": "abc",
            "Name": "Old",
            "LockedFields": ["Name"],
        }
        api, post_mock = _stub_api(server_item)

        api.update_item_metadata("abc", {"Name": "New"})

        body = post_mock.call_args.kwargs["json"]
        assert body["LockedFields"].count("Name") == 1


class TestJellyfinRejectsUnknownKeys:
    def test_unknown_key_raises_value_error(self):
        api, _post = _stub_api({"Id": "x", "Name": "y"})

        with pytest.raises(ValueError) as exc:
            api.update_item_metadata("x", {"BogusField": "whatever"})
        assert "BogusField" in str(exc.value)
        # GET / POST should not have been attempted — fail-fast validation.
        api._get.assert_not_called()

    def test_partial_unknown_still_raises_before_any_request(self):
        api, post_mock = _stub_api({"Id": "x", "Name": "y"})
        with pytest.raises(ValueError) as exc:
            api.update_item_metadata("x", {"Name": "ok", "Nope": 1})
        assert "Nope" in str(exc.value)
        api._get.assert_not_called()
        post_mock.assert_not_called()


# ── Provider-level delegation ─────────────────────────────────────


class TestProviderDelegation:
    def test_jellyfin_provider_delegates_to_api(self):
        # A JellyfinProvider with a stubbed JellyfinAPI underneath —
        # confirms the provider's update_track_metadata routes through
        # the API method that owns the GET-merge-POST workaround.
        provider = JellyfinProvider.__new__(JellyfinProvider)
        fake_api = MagicMock()
        fake_api.update_item_metadata.return_value = {"Id": "x", "Name": "y"}
        provider.api = fake_api

        result = provider.update_track_metadata("x", {"Name": "y"})
        fake_api.update_item_metadata.assert_called_once_with("x", {"Name": "y"})
        assert result == {"Id": "x", "Name": "y"}


# ── Bulk album-wide tag edit ───────────────────────────────────────


class TestBulkAlbumTagEdit:
    """``update_album_track_metadata`` — applies one edit set across
    every track of an album, fault-tolerant per track."""

    @staticmethod
    def _provider(tracks, write_side_effect=None):
        """A JellyfinProvider with a MagicMock API. ``tracks`` is what
        ``get_album_tracks`` returns; ``write_side_effect`` (optional)
        is passed to the per-track ``update_item_metadata`` mock."""
        provider = JellyfinProvider.__new__(JellyfinProvider)
        fake_api = MagicMock()
        fake_api.get_album_tracks.return_value = tracks
        if write_side_effect is not None:
            fake_api.update_item_metadata.side_effect = write_side_effect
        else:
            fake_api.update_item_metadata.return_value = {"ok": True}
        provider.api = fake_api
        return provider, fake_api

    def test_all_tracks_succeed(self):
        tracks = [{"Id": "t1"}, {"Id": "t2"}, {"Id": "t3"}]
        provider, api = self._provider(tracks)

        result = provider.update_album_track_metadata("alb1", {"Genres": ["Jazz"]})

        assert result["album_id"] == "alb1"
        assert result["succeeded"] == ["t1", "t2", "t3"]
        assert result["failed"] == []
        assert result["total"] == 3
        # The edit reached every track via the single-track path.
        assert api.update_item_metadata.call_count == 3
        for tid in ("t1", "t2", "t3"):
            api.update_item_metadata.assert_any_call(tid, {"Genres": ["Jazz"]})

    def test_partial_failure_does_not_abort_batch(self):
        tracks = [{"Id": "t1"}, {"Id": "t2"}, {"Id": "t3"}]

        def write(item_id, edits):
            if item_id == "t2":
                raise RuntimeError("server rejected t2")
            return {"ok": True}

        provider, api = self._provider(tracks, write_side_effect=write)

        result = provider.update_album_track_metadata("alb1", {"Album": "New"})

        # t2 failed, but t3 was still attempted — no early abort.
        assert result["succeeded"] == ["t1", "t3"]
        assert len(result["failed"]) == 1
        assert result["failed"][0]["item_id"] == "t2"
        assert "server rejected t2" in result["failed"][0]["error"]
        assert result["total"] == 3
        assert api.update_item_metadata.call_count == 3

    def test_empty_album(self):
        provider, api = self._provider([])

        result = provider.update_album_track_metadata("empty-alb", {"Name": "x"})

        assert result == {
            "album_id": "empty-alb",
            "succeeded": [],
            "failed": [],
            "total": 0,
        }
        api.update_item_metadata.assert_not_called()

    def test_tracks_without_id_skipped(self):
        # A malformed track dict missing "Id" must not crash the run
        # and must not count toward the total.
        tracks = [{"Id": "t1"}, {"Name": "no id"}, {"Id": "t2"}]
        provider, api = self._provider(tracks)

        result = provider.update_album_track_metadata("alb1", {"Name": "x"})

        assert result["succeeded"] == ["t1", "t2"]
        assert result["total"] == 2
        assert api.update_item_metadata.call_count == 2

    def test_base_provider_raises_not_implemented(self):
        # Any provider that hasn't opted in inherits the base stub.
        with pytest.raises(NotImplementedError) as exc:
            MediaProvider.update_album_track_metadata(None, "alb", {"Name": "x"})
        assert "edit" in str(exc.value).lower()

    def test_subsonic_raises_not_implemented(self):
        # Subsonic stays unsupported, matching can_edit_metadata.
        p = SubsonicProvider.__new__(SubsonicProvider)
        with pytest.raises(NotImplementedError):
            p.update_album_track_metadata("alb", {"Name": "x"})


# ── Per-account permission gate (Jellyfin admin check) ─────────────


class TestAccountPermissionGate:
    def test_base_default_false(self):
        # MediaProvider is abstract; the base impl ignores self, so call
        # the unbound method directly.
        assert MediaProvider.can_edit_metadata_on_account(None) is False

    def test_jellyfin_reflects_api_is_admin(self):
        provider = JellyfinProvider.__new__(JellyfinProvider)
        provider.api = MagicMock()
        provider.api.is_admin = True
        assert provider.can_edit_metadata_on_account() is True
        provider.api.is_admin = False
        assert provider.can_edit_metadata_on_account() is False

    def test_authenticate_captures_admin_flag(self):
        api = JellyfinAPI()
        fake_resp = MagicMock()
        fake_resp.raise_for_status = MagicMock()
        fake_resp.json.return_value = {
            "AccessToken": "tok",
            "User": {"Id": "u1", "Policy": {"IsAdministrator": True}},
        }
        api.session.post = MagicMock(return_value=fake_resp)
        api.authenticate("http://example.test", "u", "p")
        assert api.is_admin is True


# ── Tag editor dialog — collect_edits ──────────────────────────────


class TestTagEditorDialog:
    _TRACK = {
        "Id": "t1",
        "Name": "Old Title",
        "Artists": ["Air"],
        "Album": "Moon Safari",
        "AlbumArtist": "Air",
        "Genres": ["Electronic"],
        "IndexNumber": 3,
        "ProductionYear": 1998,
    }

    def test_no_changes_yields_empty_edits(self, qapp):
        from modules.tag_editor import TagEditorDialog

        dlg = TagEditorDialog(self._TRACK)
        try:
            assert dlg.collect_edits() == {}
        finally:
            dlg.deleteLater()

    def test_only_changed_fields_collected(self, qapp):
        from modules.tag_editor import TagEditorDialog

        dlg = TagEditorDialog(self._TRACK)
        try:
            dlg._name.setText("New Title")
            dlg._year.setValue(1999)
            assert dlg.collect_edits() == {"Name": "New Title", "ProductionYear": 1999}
        finally:
            dlg.deleteLater()

    def test_list_fields_parse_from_csv(self, qapp):
        from modules.tag_editor import TagEditorDialog

        dlg = TagEditorDialog(self._TRACK)
        try:
            dlg._genres.setText("Electronic, Ambient ,  Downtempo")
            assert dlg.collect_edits() == {
                "Genres": ["Electronic", "Ambient", "Downtempo"]
            }
        finally:
            dlg.deleteLater()
