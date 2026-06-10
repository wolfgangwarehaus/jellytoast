"""Real-implementation tests for SubsonicProvider auth + stream URLs.

The provider's auth scheme is a documented moat: per-request token+salt
(``t = md5(password + salt)``) so the password never crosses the wire.
Until now that scheme was only exercised indirectly through consumer
fakes (e.g. ``test_subsonic_radio.py`` stubs ``_request`` outright).
These tests drive the *actual* ``_auth_params`` / ``_build_url`` /
``get_audio_stream_url`` / ``authenticate`` code so a regression in the
hashing, salt generation, or URL shape is caught directly.

No network: providers are built via ``__new__`` (bypassing the Settings
singleton + keyring) and any HTTP is mocked at ``session.get``.
"""

from __future__ import annotations

import hashlib
from typing import Tuple
from unittest.mock import MagicMock
from urllib.parse import parse_qs, urlparse

import pytest

from jellytoast.providers.subsonic import (
    CLIENT_NAME,
    PROTOCOL_VERSION,
    SubsonicError,
    SubsonicProvider,
)


def _provider(
    username: str = "alice",
    password: str = "secretpw",
    server_url: str = "http://example.test",
) -> SubsonicProvider:
    """A SubsonicProvider with the auth fields set directly.

    ``__new__`` skips ``__init__`` so the test never touches the real
    Settings singleton / keyring — the auth helpers only read
    ``_username`` / ``_password`` / ``_server_url``.
    """
    p = SubsonicProvider.__new__(SubsonicProvider)
    p._server_url = server_url.rstrip("/")
    p._username = username
    p._password = password
    return p


def _query(url: str) -> dict:
    """Flatten a URL's query string to a single-value dict."""
    return {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}


# ── _auth_params: token+salt scheme ────────────────────────────────


class TestAuthParamsTokenScheme:
    def test_token_is_md5_of_password_plus_salt(self):
        p = _provider(password="hunter2")
        ap = p._auth_params()
        # The whole point of the scheme: the wire carries an md5 of
        # (password + salt), never the password itself.
        expected = hashlib.md5(("hunter2" + ap["s"]).encode("utf-8")).hexdigest()
        assert ap["t"] == expected
        # And the raw password is nowhere in the params.
        assert "hunter2" not in ap.values()
        assert "p" not in ap

    def test_salt_is_freshly_generated_each_call(self):
        p = _provider()
        salts = {p._auth_params()["s"] for _ in range(8)}
        # Fresh random salt per call → 8 calls give 8 distinct salts
        # (collision odds for 16 hex chars are vanishingly small).
        assert len(salts) == 8

    def test_token_changes_with_salt(self):
        p = _provider()
        a, b = p._auth_params(), p._auth_params()
        # Different salt → different token, even though the password
        # and username are identical.
        assert a["s"] != b["s"]
        assert a["t"] != b["t"]

    def test_salt_is_reasonable_length_and_hex(self):
        p = _provider()
        salt = p._auth_params()["s"]
        # Subsonic spec requires >= 6 random chars; the impl uses
        # ``secrets.token_hex(8)`` → 16 hex chars, comfortably over.
        assert len(salt) >= 6
        assert len(salt) == 16
        assert all(c in "0123456789abcdef" for c in salt)

    def test_standard_params_present_and_correct(self):
        p = _provider(username="bob")
        ap = p._auth_params()
        assert set(ap) == {"u", "t", "s", "v", "c"}
        assert ap["u"] == "bob"
        assert ap["v"] == PROTOCOL_VERSION
        assert ap["c"] == CLIENT_NAME

    def test_no_format_param(self):
        # f=json is REST-only; including it on binary endpoints makes
        # the server return a JSON error envelope instead of bytes.
        # _auth_params must NOT carry it (the JSON _request helper adds
        # it itself).
        p = _provider()
        assert "f" not in p._auth_params()

    def test_token_verifiable_by_server_side_recompute(self):
        # Simulate the server's verification: it knows the password and
        # receives (t, s); recomputing md5(password + s) must match t.
        p = _provider(password="P@ss w0rd!")
        ap = p._auth_params()
        server_recompute = hashlib.md5(
            ("P@ss w0rd!" + ap["s"]).encode("utf-8")
        ).hexdigest()
        assert server_recompute == ap["t"]


# ── _build_url: binary endpoint URL shape ──────────────────────────


class TestAuthParamsPlainScheme:
    """#4: LDAP-backed servers reject token+salt (Subsonic error 41); the
    provider must switch to plain-password (``p=``) auth for EVERY request,
    not just the verification ping — else post-login requests revert to
    token auth, the server keeps returning 41, and the user is dropped
    into a login -> logout loop."""

    def test_plain_mode_emits_password_not_token(self):
        p = _provider(username="bob", password="hunter2")
        p._auth_mode_plain = True
        ap = p._auth_params()
        assert ap["p"] == "hunter2"
        assert "t" not in ap and "s" not in ap
        assert set(ap) == {"u", "p", "v", "c"}

    def test_token_mode_is_default(self):
        # No plain flag (token-backed account) → scheme stays token+salt.
        p = _provider()
        ap = p._auth_params()
        assert "p" not in ap
        assert "t" in ap and "s" in ap

    def test_init_restores_plain_mode_from_settings(self, isolated_settings):
        # Persistence: a provider rebuilt on next launch must keep using
        # plain auth (the flag is stored), not revert to token + loop.
        isolated_settings.subsonic_auth_mode_plain = True
        isolated_settings.username = "bob"
        isolated_settings.access_token = "pw"
        isolated_settings.server_url = "http://x.example"
        from jellytoast.providers.subsonic import SubsonicProvider

        p = SubsonicProvider()
        assert p._auth_mode_plain is True
        assert p._auth_params().get("p") == "pw"


class TestBuildUrl:
    def test_includes_auth_and_extra_params(self):
        p = _provider()
        url = p._build_url("getCoverArt", {"id": "art1", "size": 300})
        assert url.startswith("http://example.test/rest/getCoverArt?")
        q = _query(url)
        # Auth params merged with the endpoint params.
        assert q["id"] == "art1"
        assert q["size"] == "300"
        assert q["u"] == "alice"
        assert q["v"] == PROTOCOL_VERSION
        assert q["c"] == CLIENT_NAME
        assert "t" in q and "s" in q

    def test_no_format_json_on_binary_url(self):
        # Binary endpoints must not carry f=json.
        p = _provider()
        url = p._build_url("stream", {"id": "x"})
        assert "f" not in _query(url)

    def test_server_url_override(self):
        p = _provider()
        url = p._build_url("stream", {"id": "x"}, server_url="http://other.host/")
        # Trailing slash on the override is stripped before /rest/.
        assert url.startswith("http://other.host/rest/stream?")


# ── get_audio_stream_url ───────────────────────────────────────────


class TestGetAudioStreamUrl:
    def test_empty_item_id_returns_empty(self):
        p = _provider()
        assert p.get_audio_stream_url("") == ""

    def test_original_quality_is_raw(self, monkeypatch):
        p = _provider()
        # Force the original (bit-perfect) branch regardless of the
        # ambient default the Settings singleton would report.
        from jellytoast.providers import subsonic as _sub

        fake_settings = MagicMock()
        fake_settings.audio_quality = "original"
        monkeypatch.setattr(_sub, "get_settings", lambda: fake_settings)

        url = p.get_audio_stream_url("song42")
        assert urlparse(url).path == "/rest/stream"
        q = _query(url)
        assert q["id"] == "song42"
        assert q["format"] == "raw"
        # Raw means no transcode → no maxBitRate (it would force a
        # transcode on older Navidrome builds).
        assert "maxBitRate" not in q

    def test_quality_override_transcodes_to_mp3(self):
        # The ``quality`` arg overrides the setting without any
        # monkeypatching — exercises the transcode branch directly.
        p = _provider()
        url = p.get_audio_stream_url("song42", quality="320")
        assert urlparse(url).path == "/rest/stream"
        q = _query(url)
        assert q["id"] == "song42"
        assert q["format"] == "mp3"
        assert q["maxBitRate"] == "320"

    def test_quality_clamped_to_min_32(self):
        p = _provider()
        url = p.get_audio_stream_url("s", quality="8")
        assert _query(url)["maxBitRate"] == "32"

    def test_non_numeric_quality_falls_back_to_320(self):
        p = _provider()
        url = p.get_audio_stream_url("s", quality="garbage")
        assert _query(url)["maxBitRate"] == "320"

    def test_stream_url_carries_fresh_auth(self):
        # Every stream URL is independently authed (Subsonic is stateless).
        p = _provider(password="pw")
        q = _query(p.get_audio_stream_url("s", quality="256"))
        assert q["t"] == hashlib.md5(("pw" + q["s"]).encode()).hexdigest()


class TestGetAudioTranscodeUrl:
    def test_builds_stream_with_codec_and_bitrate(self):
        p = _provider()
        url = p.get_audio_transcode_url("s1", max_bitrate_kbps=256, codec="opus")
        assert urlparse(url).path == "/rest/stream"
        q = _query(url)
        assert q["id"] == "s1"
        assert q["format"] == "opus"
        assert q["maxBitRate"] == "256"

    def test_empty_item_id_returns_empty(self):
        p = _provider()
        assert p.get_audio_transcode_url("") == ""


# ── authenticate: end-to-end credential test via ping ──────────────


def _ok_response() -> MagicMock:
    r = MagicMock()
    r.status_code = 200
    r.content = b'{"subsonic-response": {"status": "ok"}}'
    r.json.return_value = {"subsonic-response": {"status": "ok"}}
    r.raise_for_status = MagicMock()
    return r


def _failed_response(code: int, message: str = "boom") -> MagicMock:
    body = {
        "subsonic-response": {
            "status": "failed",
            "error": {"code": code, "message": message},
        }
    }
    r = MagicMock()
    r.status_code = 200
    r.content = b"{...}"
    r.json.return_value = body
    r.raise_for_status = MagicMock()
    return r


def _auth_provider(monkeypatch) -> Tuple[SubsonicProvider, MagicMock]:
    """A provider whose ``settings`` + ``session`` are mocked so
    ``authenticate`` runs end-to-end without disk / network."""
    p = SubsonicProvider.__new__(SubsonicProvider)
    p._server_url = ""
    p._username = ""
    p._password = ""
    p.settings = MagicMock()
    p.session = MagicMock()
    # Keep the offline tracker inert during the test.
    import jellytoast.offline as _offline

    for fn in (
        "note_request_success",
        "note_request_failure",
        "note_auth_success",
        "note_auth_failure",
    ):
        monkeypatch.setattr(_offline, fn, lambda *a, **k: None, raising=False)
    return p, p.session


class TestAuthenticate:
    def test_pings_with_token_auth_and_persists(self, monkeypatch):
        p, session = _auth_provider(monkeypatch)
        session.get.return_value = _ok_response()

        result = p.authenticate("http://nav.test/", "alice", "secretpw")

        # The credential test went to /rest/ping with token auth.
        session.get.assert_called_once()
        url = session.get.call_args.args[0]
        assert url.startswith("http://nav.test/rest/ping?")
        q = _query(url)
        assert q["u"] == "alice"
        assert q["f"] == "json"  # JSON endpoint carries f=json
        assert q["t"] == hashlib.md5(("secretpw" + q["s"]).encode()).hexdigest()
        assert "p" not in q  # plain password is never sent in token mode

        # AuthResult mirrors the verified credentials.
        assert result.user_id == "alice"
        assert result.username == "alice"
        assert result.access_token == "secretpw"
        assert result.server_url == "http://nav.test"

        # Credentials were persisted + force-flushed.
        assert p.settings.provider_kind == "subsonic"
        p.settings.flush.assert_called_once()

    def test_wrong_password_clears_creds_and_raises(self, monkeypatch):
        p, session = _auth_provider(monkeypatch)
        session.get.return_value = _failed_response(40, "Wrong username or password")

        with pytest.raises(SubsonicError) as exc:
            p.authenticate("http://nav.test", "alice", "badpw")
        assert exc.value.code == 40
        # A definitive reject wipes the in-memory creds.
        assert p._username == ""
        assert p._password == ""
