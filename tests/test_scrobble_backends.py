"""Wire-shape tests for the two scrobble HTTP backends.

``modules/scrobble/lastfm.py`` and ``modules/scrobble/listenbrainz.py``
had zero direct coverage despite carrying the security-sensitive
Last.fm request-signing (``api_sig``) and the ListenBrainz payload
shapes that the scrobble manager depends on.

No network: the ``requests`` entry points (``requests.post`` /
``requests.get``) and/or the module-private ``_post`` are stubbed with
recording mocks, so we inspect exactly what *would* have gone out. The
Last.fm half also needs ``API_KEY`` / ``API_SECRET`` populated because
the real functions short-circuit on ``is_configured()`` — each fixture
pins concrete test credentials via monkeypatch.

The ``api_sig`` assertion deliberately recomputes the expected digest
with an INDEPENDENT reference implementation of the documented Last.fm
algorithm so the test catches a regression in ``_sign`` rather than
re-deriving the same (possibly wrong) value.
"""

from __future__ import annotations

import hashlib
from unittest.mock import MagicMock

import pytest

from modules.scrobble import lastfm, listenbrainz

# Test-only Last.fm credentials. The real constants ship empty (DEFERRED
# until august registers an app); we pin concrete values so the
# ``is_configured()`` short-circuit doesn't swallow every call.
_TEST_KEY = "test_api_key"
_TEST_SECRET = "test_shared_secret"


@pytest.fixture
def lastfm_configured(monkeypatch):
    """Populate the module-level Last.fm credentials so the backend's
    ``is_configured()`` gate opens. ``_sign`` reads ``API_SECRET`` and
    ``_signed`` reads ``API_KEY`` straight off the module at call time,
    so monkeypatching the globals is sufficient."""
    monkeypatch.setattr(lastfm, "API_KEY", _TEST_KEY)
    monkeypatch.setattr(lastfm, "API_SECRET", _TEST_SECRET)
    return lastfm


def _reference_api_sig(params: dict, secret: str) -> str:
    """Independent reference implementation of Last.fm's signature
    algorithm (per the documented spec): drop ``format``/``callback``,
    sort the remaining params by key, concatenate ``name+value`` for
    each, append the shared secret, MD5 the UTF-8 bytes."""
    base = ""
    for key in sorted(params):
        if key in ("format", "callback"):
            continue
        base += f"{key}{params[key]}"
    base += secret
    return hashlib.md5(base.encode("utf-8")).hexdigest()


# ── Last.fm: signing ───────────────────────────────────────────────


class TestLastfmSign:
    def test_matches_independent_reference_digest(self, lastfm_configured):
        # Representative param dict; api_key is part of the signed base.
        params = {
            "method": "track.scrobble",
            "api_key": _TEST_KEY,
            "sk": "session123",
            "artist[0]": "Daft Punk",
            "track[0]": "Aerodynamic",
            "timestamp[0]": "1700000000",
        }
        expected = _reference_api_sig(params, _TEST_SECRET)
        assert lastfm_configured._sign(params) == expected
        # And it's a real 32-hex-char MD5 digest.
        assert len(lastfm_configured._sign(params)) == 32
        int(lastfm_configured._sign(params), 16)  # raises if not hex

    def test_excludes_format_and_callback_from_base(self, lastfm_configured):
        # Adding format/callback must NOT change the signature — they are
        # explicitly excluded from the signing base per the Last.fm docs.
        without = {"method": "auth.getToken", "api_key": _TEST_KEY}
        with_extras = dict(without, format="json", callback="cb")
        assert lastfm_configured._sign(without) == lastfm_configured._sign(with_extras)

    def test_ordering_is_by_sorted_key_not_insertion(self, lastfm_configured):
        # Insertion order is deliberately the reverse of sorted order. If
        # _sign honoured insertion order the digest would differ from the
        # sorted-key reference, proving the sort actually happens.
        params = {"zebra": "1", "method": "track.scrobble", "alpha": "9"}
        assert list(params.keys()) != sorted(params.keys())
        assert lastfm_configured._sign(params) == _reference_api_sig(params, _TEST_SECRET)

    def test_secret_is_appended(self, lastfm_configured, monkeypatch):
        # Changing the shared secret must change the digest — guards
        # against the secret being dropped from the base entirely.
        params = {"method": "auth.getToken", "api_key": _TEST_KEY}
        sig_a = lastfm_configured._sign(params)
        monkeypatch.setattr(lastfm_configured, "API_SECRET", "a_different_secret")
        sig_b = lastfm_configured._sign(params)
        assert sig_a != sig_b


# ── Last.fm: _signed envelope ──────────────────────────────────────


class TestLastfmSignedEnvelope:
    def test_adds_api_sig_api_key_and_json_format(self, lastfm_configured):
        body = lastfm_configured._signed({"method": "auth.getToken"})
        # Original param preserved.
        assert body["method"] == "auth.getToken"
        # api_key injected from the module constant.
        assert body["api_key"] == _TEST_KEY
        # format forced to json (Last.fm defaults to XML otherwise).
        assert body["format"] == "json"
        # api_sig present and consistent with the signed base. The base
        # includes api_key but NOT format (added after signing).
        signed_base = {"method": "auth.getToken", "api_key": _TEST_KEY}
        assert body["api_sig"] == _reference_api_sig(signed_base, _TEST_SECRET)

    def test_does_not_mutate_caller_params(self, lastfm_configured):
        original = {"method": "auth.getToken"}
        lastfm_configured._signed(original)
        # _signed copies before adding api_sig/api_key/format.
        assert original == {"method": "auth.getToken"}

    def test_explicit_api_key_is_preserved(self, lastfm_configured):
        # setdefault means a caller-supplied api_key wins over the module
        # constant.
        body = lastfm_configured._signed({"method": "m", "api_key": "explicit"})
        assert body["api_key"] == "explicit"


# ── Last.fm: payload shapes (network stubbed) ──────────────────────


def _stub_lastfm_post(monkeypatch, ok=True, body=None):
    """Replace ``lastfm._post`` with a recorder returning a fixed
    ``(ok, body)``. Returns the MagicMock so the test can read the
    params dict that was passed."""
    mock = MagicMock(return_value=(ok, body if body is not None else {}))
    monkeypatch.setattr(lastfm, "_post", mock)
    return mock


class TestLastfmUpdateNowPlaying:
    def test_builds_expected_params(self, lastfm_configured, monkeypatch):
        post = _stub_lastfm_post(monkeypatch)
        ok = lastfm_configured.update_now_playing(
            "sk1", "Daft Punk", "Aerodynamic", album="Discovery", duration_ms=212000
        )
        assert ok is True
        params = post.call_args.args[0]
        assert params["method"] == "track.updateNowPlaying"
        assert params["sk"] == "sk1"
        assert params["artist"] == "Daft Punk"
        assert params["track"] == "Aerodynamic"
        assert params["album"] == "Discovery"
        # duration_ms → whole seconds.
        assert params["duration"] == "212"

    def test_omits_optional_fields_when_absent(self, lastfm_configured, monkeypatch):
        post = _stub_lastfm_post(monkeypatch)
        lastfm_configured.update_now_playing("sk1", "A", "B")
        params = post.call_args.args[0]
        assert "album" not in params
        assert "duration" not in params
        assert "mbid" not in params

    def test_short_circuits_without_required_fields(self, lastfm_configured, monkeypatch):
        post = _stub_lastfm_post(monkeypatch)
        assert lastfm_configured.update_now_playing("", "A", "B") is False
        assert lastfm_configured.update_now_playing("sk", "", "B") is False
        assert lastfm_configured.update_now_playing("sk", "A", "") is False
        post.assert_not_called()


class TestLastfmScrobble:
    def test_builds_indexed_params(self, lastfm_configured, monkeypatch):
        post = _stub_lastfm_post(monkeypatch)
        ok, code = lastfm_configured.scrobble(
            "sk1",
            "Daft Punk",
            "Aerodynamic",
            1700000000,
            album="Discovery",
            duration_ms=212000,
            mbid="mb-1",
        )
        assert ok is True and code is None
        params = post.call_args.args[0]
        assert params["method"] == "track.scrobble"
        assert params["sk"] == "sk1"
        # Single scrobble uses the indexed [0] batch form.
        assert params["artist[0]"] == "Daft Punk"
        assert params["track[0]"] == "Aerodynamic"
        assert params["timestamp[0]"] == "1700000000"
        assert params["album[0]"] == "Discovery"
        assert params["duration[0]"] == "212"
        assert params["mbid[0]"] == "mb-1"

    def test_short_circuits_on_bad_timestamp(self, lastfm_configured, monkeypatch):
        post = _stub_lastfm_post(monkeypatch)
        ok, code = lastfm_configured.scrobble("sk", "A", "B", 0)
        assert ok is False and code is None
        post.assert_not_called()

    def test_returns_error_code_on_failure(self, lastfm_configured, monkeypatch):
        _stub_lastfm_post(monkeypatch, ok=False, body={"error": 9})
        ok, code = lastfm_configured.scrobble("sk", "A", "B", 1700000000)
        assert ok is False
        assert code == 9

    def test_non_numeric_error_code_becomes_none(self, lastfm_configured, monkeypatch):
        _stub_lastfm_post(monkeypatch, ok=False, body={"error": "nope"})
        ok, code = lastfm_configured.scrobble("sk", "A", "B", 1700000000)
        assert ok is False and code is None


class TestLastfmScrobbleBatch:
    def test_builds_indexed_params_per_listen(self, lastfm_configured, monkeypatch):
        post = _stub_lastfm_post(monkeypatch)
        listens = [
            {"artist": "A1", "track": "T1", "timestamp": 100, "album": "Al1", "duration_ms": 60000},
            {"artist": "A2", "track": "T2", "timestamp": 200, "mbid": "mb2"},
        ]
        ok, code = lastfm_configured.scrobble_batch("sk1", listens)
        assert ok is True and code is None
        params = post.call_args.args[0]
        assert params["method"] == "track.scrobble"
        assert params["sk"] == "sk1"
        # Entry 0 — full enrichment.
        assert params["artist[0]"] == "A1"
        assert params["track[0]"] == "T1"
        assert params["timestamp[0]"] == "100"
        assert params["album[0]"] == "Al1"
        assert params["duration[0]"] == "60"
        # Entry 1 — minimal; album/duration omitted, mbid present.
        assert params["artist[1]"] == "A2"
        assert params["track[1]"] == "T2"
        assert params["timestamp[1]"] == "200"
        assert "album[1]" not in params
        assert "duration[1]" not in params
        assert params["mbid[1]"] == "mb2"

    def test_caps_at_max_batch(self, lastfm_configured, monkeypatch):
        post = _stub_lastfm_post(monkeypatch)
        n = lastfm_configured.MAX_LISTENS_PER_BATCH + 5
        listens = [{"artist": f"A{i}", "track": f"T{i}", "timestamp": i + 1} for i in range(n)]
        lastfm_configured.scrobble_batch("sk1", listens)
        params = post.call_args.args[0]
        last = lastfm_configured.MAX_LISTENS_PER_BATCH - 1
        assert f"artist[{last}]" in params
        # The (cap)th index was truncated away.
        assert f"artist[{lastfm_configured.MAX_LISTENS_PER_BATCH}]" not in params

    def test_short_circuits_on_empty_inputs(self, lastfm_configured, monkeypatch):
        post = _stub_lastfm_post(monkeypatch)
        assert lastfm_configured.scrobble_batch("", [{"artist": "A"}]) == (False, None)
        assert lastfm_configured.scrobble_batch("sk", []) == (False, None)
        post.assert_not_called()


class TestLastfmConfigGate:
    def test_unconfigured_post_is_noop(self, monkeypatch):
        # With empty credentials, is_configured() is False so _post never
        # touches the network — guards against a partial-config leak.
        monkeypatch.setattr(lastfm, "API_KEY", "")
        monkeypatch.setattr(lastfm, "API_SECRET", "")
        sent = MagicMock()
        monkeypatch.setattr(lastfm.requests, "post", sent)
        ok, body = lastfm._post({"method": "track.scrobble"})
        assert ok is False and body == {}
        sent.assert_not_called()

    def test_configured_post_signs_and_sends(self, lastfm_configured, monkeypatch):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"scrobbles": {"@attr": {"accepted": 1}}}
        sent = MagicMock(return_value=resp)
        monkeypatch.setattr(lastfm_configured.requests, "post", sent)
        ok, body = lastfm_configured._post({"method": "track.scrobble", "sk": "sk1"})
        assert ok is True
        # Posted to the API root with a signed body carrying api_sig.
        assert sent.call_args.args[0] == lastfm_configured.API_ROOT
        data = sent.call_args.kwargs["data"]
        assert data["api_sig"]
        assert data["format"] == "json"


# ── ListenBrainz: payload shapes ───────────────────────────────────


class TestListenBrainzTrackMetadata:
    def test_build_track_metadata_core_shape(self):
        md = listenbrainz.build_track_metadata(
            "Daft Punk", "Aerodynamic", release_name="Discovery", duration_ms=212000, recording_mbid="mb-1"
        )
        assert md["artist_name"] == "Daft Punk"
        assert md["track_name"] == "Aerodynamic"
        assert md["release_name"] == "Discovery"
        add = md["additional_info"]
        assert add["submission_client"] == "jellytoast"
        assert add["submission_client_version"] == "0.1.0"
        assert add["duration_ms"] == 212000
        assert add["recording_mbid"] == "mb-1"

    def test_omits_release_and_optional_enrichment(self):
        md = listenbrainz.build_track_metadata("A", "B")
        assert "release_name" not in md
        add = md["additional_info"]
        assert "duration_ms" not in add
        assert "recording_mbid" not in add
        # submission_client always present so LB attributes the listen to us.
        assert add["submission_client"] == "jellytoast"


def _stub_post_listens(monkeypatch):
    """Capture the body handed to ``listenbrainz._post_listens`` without
    touching the network. Returns the recorder mock."""
    mock = MagicMock(return_value=True)
    monkeypatch.setattr(listenbrainz, "_post_listens", mock)
    return mock


class TestListenBrainzNowPlaying:
    def test_playing_now_payload_shape(self, monkeypatch):
        post = _stub_post_listens(monkeypatch)
        md = listenbrainz.build_track_metadata("A", "B")
        assert listenbrainz.send_now_playing("tok", md) is True
        token, body, base = post.call_args.args
        assert token == "tok"
        assert body["listen_type"] == "playing_now"
        # No listened_at on a now-playing ping.
        entry = body["payload"][0]
        assert "listened_at" not in entry
        assert entry["track_metadata"] is md

    def test_no_token_short_circuits(self, monkeypatch):
        post = _stub_post_listens(monkeypatch)
        assert listenbrainz.send_now_playing("", {}) is False
        post.assert_not_called()


class TestListenBrainzSingleListen:
    def test_single_payload_shape(self, monkeypatch):
        post = _stub_post_listens(monkeypatch)
        md = listenbrainz.build_track_metadata("A", "B")
        assert listenbrainz.send_single_listen("tok", md, 1700000000) is True
        _token, body, _base = post.call_args.args
        assert body["listen_type"] == "single"
        entry = body["payload"][0]
        assert entry["listened_at"] == 1700000000
        assert entry["track_metadata"] is md

    def test_nonpositive_listened_at_defaults_to_now(self, monkeypatch):
        post = _stub_post_listens(monkeypatch)
        monkeypatch.setattr(listenbrainz.time, "time", lambda: 1234567890.0)
        listenbrainz.send_single_listen("tok", {"artist_name": "A", "track_name": "B"}, 0)
        entry = post.call_args.args[1]["payload"][0]
        assert entry["listened_at"] == 1234567890

    def test_no_token_short_circuits(self, monkeypatch):
        post = _stub_post_listens(monkeypatch)
        assert listenbrainz.send_single_listen("", {}, 1700000000) is False
        post.assert_not_called()


class TestListenBrainzBatch:
    def test_import_payload_passthrough(self, monkeypatch):
        post = _stub_post_listens(monkeypatch)
        listens = [
            {"listened_at": 100, "track_metadata": {"artist_name": "A", "track_name": "T"}},
            {"listened_at": 200, "track_metadata": {"artist_name": "B", "track_name": "U"}},
        ]
        assert listenbrainz.send_listens_batch("tok", listens) is True
        _token, body, _base = post.call_args.args
        assert body["listen_type"] == "import"
        # Entries pass through untouched (queue stores them in submit shape).
        assert body["payload"] == listens

    def test_caps_at_max_batch(self, monkeypatch):
        post = _stub_post_listens(monkeypatch)
        n = listenbrainz.MAX_LISTENS_PER_BATCH + 10
        listens = [
            {"listened_at": i + 1, "track_metadata": {"artist_name": "A", "track_name": "T"}}
            for i in range(n)
        ]
        listenbrainz.send_listens_batch("tok", listens)
        body = post.call_args.args[1]
        assert len(body["payload"]) == listenbrainz.MAX_LISTENS_PER_BATCH

    def test_empty_or_no_token_short_circuits(self, monkeypatch):
        post = _stub_post_listens(monkeypatch)
        assert listenbrainz.send_listens_batch("", [{"x": 1}]) is False
        assert listenbrainz.send_listens_batch("tok", []) is False
        post.assert_not_called()


# ── ListenBrainz: helpers + network classification ─────────────────


class TestListenBrainzHelpers:
    def test_normalize_base_default_and_strip(self):
        assert listenbrainz._normalize_base("") == listenbrainz.DEFAULT_BASE_URL
        assert listenbrainz._normalize_base("https://lb.example/") == "https://lb.example"

    def test_auth_headers_carry_token_and_agent(self):
        h = listenbrainz._auth_headers("mytoken")
        assert h["Authorization"] == "Token mytoken"
        assert h["Content-Type"] == "application/json"
        assert "jellytoast" in h["User-Agent"]

    def test_post_listens_url_and_success_classification(self, monkeypatch):
        resp = MagicMock()
        resp.status_code = 200
        sent = MagicMock(return_value=resp)
        monkeypatch.setattr(listenbrainz.requests, "post", sent)
        ok = listenbrainz._post_listens("tok", {"listen_type": "single"}, "https://lb.example")
        assert ok is True
        assert sent.call_args.args[0] == "https://lb.example/1/submit-listens"
        assert sent.call_args.kwargs["json"] == {"listen_type": "single"}
        assert sent.call_args.kwargs["headers"]["Authorization"] == "Token tok"

    def test_post_listens_transient_non_2xx_is_failure(self, monkeypatch):
        # A transient non-2xx (5xx / 429) returns False so the manager retries.
        # Permanent 4xx (400/422) now returns True (drop) — see
        # TestPostListensFailureClassification.
        resp = MagicMock()
        resp.status_code = 503
        monkeypatch.setattr(listenbrainz.requests, "post", MagicMock(return_value=resp))
        assert listenbrainz._post_listens("tok", {}, "") is False

    def test_post_listens_network_error_is_failure(self, monkeypatch):
        def boom(*a, **k):
            raise listenbrainz.requests.ConnectionError("down")

        monkeypatch.setattr(listenbrainz.requests, "post", boom)
        assert listenbrainz._post_listens("tok", {}, "") is False


class TestListenBrainzValidateToken:
    def test_valid_returns_username(self, monkeypatch):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"valid": True, "user_name": "august"}
        monkeypatch.setattr(listenbrainz.requests, "get", MagicMock(return_value=resp))
        assert listenbrainz.validate_token("tok") == "august"

    def test_invalid_returns_none(self, monkeypatch):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"valid": False}
        monkeypatch.setattr(listenbrainz.requests, "get", MagicMock(return_value=resp))
        assert listenbrainz.validate_token("tok") is None

    def test_empty_token_short_circuits(self, monkeypatch):
        sent = MagicMock()
        monkeypatch.setattr(listenbrainz.requests, "get", sent)
        assert listenbrainz.validate_token("") is None
        sent.assert_not_called()


class TestPostListensFailureClassification:
    """Regression (review #17): permanent client errors (400/422) report
    'consumed' (True → the manager drops the batch) so a poisoned entry can't
    retry forever; transient failures (429/5xx/network) stay False (retry)."""

    @staticmethod
    def _resp(code):
        r = MagicMock()
        r.status_code = code
        return r

    def test_success_returns_true(self, monkeypatch):
        monkeypatch.setattr(
            listenbrainz.requests, "post", MagicMock(return_value=self._resp(200))
        )
        assert listenbrainz._post_listens("tok", {"x": 1}, "https://api.listenbrainz.org") is True

    def test_permanent_4xx_returns_true_to_drop(self, monkeypatch):
        for code in (400, 422):
            monkeypatch.setattr(
                listenbrainz.requests, "post", MagicMock(return_value=self._resp(code))
            )
            assert listenbrainz._post_listens("tok", {"x": 1}, "u") is True, code

    def test_transient_failures_return_false_to_retry(self, monkeypatch):
        for code in (429, 500, 503):
            monkeypatch.setattr(
                listenbrainz.requests, "post", MagicMock(return_value=self._resp(code))
            )
            assert listenbrainz._post_listens("tok", {"x": 1}, "u") is False, code

    def test_network_error_returns_false(self, monkeypatch):
        def _raise(*a, **k):
            raise listenbrainz.requests.RequestException("boom")

        monkeypatch.setattr(listenbrainz.requests, "post", _raise)
        assert listenbrainz._post_listens("tok", {"x": 1}, "u") is False
