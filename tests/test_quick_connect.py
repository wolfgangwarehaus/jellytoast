"""Jellyfin Quick Connect — API calls and the login-view flow.

API: Initiate is POST (10.9+) with a GET fallback for 10.8 (12.0 removed
the GET); handshakes carry the client identity but never a stale token;
a successful AuthenticateWithQuickConnect commits the session exactly
like a password sign-in.

LoginView: start → code shown → poll until approved → finish → signed_in;
cancel fences off late replies; disabled / expired / unreachable each get
a plain message.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
import requests

from jellytoast.jellyfin_api import JellyfinAPI
from jellytoast.providers.base import AuthResult

# ── API ───────────────────────────────────────────────────────────


def _resp(status=200, body=None):
    r = MagicMock(status_code=status, ok=200 <= status < 300)
    r.json.return_value = body
    r.content = b"x"
    if status >= 400:
        r.raise_for_status.side_effect = requests.HTTPError(str(status), response=r)
    return r


@pytest.fixture
def api(isolated_settings):
    a = JellyfinAPI()
    a.token = "stale-token"
    a.server_url = "http://old.example"
    a.settings.jellyfin_server_version = ""
    a.server_version = None
    return a


class TestApi:
    def test_enabled(self, api):
        api.session.get = MagicMock(return_value=_resp(200, True))
        assert api.quick_connect_enabled("http://jf.test/") is True
        assert api.session.get.call_args.args[0] == "http://jf.test/QuickConnect/Enabled"
        api.session.get = MagicMock(return_value=_resp(200, False))
        assert api.quick_connect_enabled("http://jf.test") is False
        api.session.get = MagicMock(side_effect=requests.ConnectionError())
        assert api.quick_connect_enabled("http://jf.test") is False

    def test_initiate_posts_without_the_stale_token(self, api):
        api.session.post = MagicMock(return_value=_resp(200, {"Secret": "s", "Code": "123456"}))
        assert api.quick_connect_initiate("http://jf.test")["Code"] == "123456"
        auth = api.session.post.call_args.kwargs["headers"]["Authorization"]
        assert "Token=" not in auth and 'DeviceId="' in auth

    def test_initiate_falls_back_to_get_on_10_8(self, api):
        api.session.post = MagicMock(return_value=_resp(405))
        api.session.get = MagicMock(return_value=_resp(200, {"Secret": "s", "Code": "1"}))
        assert api.quick_connect_initiate("http://jf.test")["Secret"] == "s"
        assert api.session.get.call_args.args[0] == "http://jf.test/QuickConnect/Initiate"

    def test_initiate_rejects_garbage(self, api):
        api.session.post = MagicMock(return_value=_resp(200, {"Code": "1"}))
        with pytest.raises(ValueError):
            api.quick_connect_initiate("http://jf.test")

    def test_approved_polls_connect(self, api):
        api.session.get = MagicMock(return_value=_resp(200, {"Authenticated": True}))
        assert api.quick_connect_approved("http://jf.test", "sec") is True
        assert api.session.get.call_args.kwargs["params"] == {"secret": "sec"}
        api.session.get = MagicMock(return_value=_resp(404))
        with pytest.raises(requests.HTTPError):
            api.quick_connect_approved("http://jf.test", "sec")

    def test_authenticate_commits_the_session(self, api, monkeypatch):
        monkeypatch.setattr(api, "_refresh_server_version", lambda: None)
        api.session.post = MagicMock(
            return_value=_resp(
                200,
                {
                    "AccessToken": "new-tok",
                    "User": {"Id": "u9", "Name": "alice", "Policy": {"IsAdministrator": False}},
                },
            )
        )
        api.quick_connect_authenticate("http://jf.test/", "sec")
        assert api.session.post.call_args.kwargs["json"] == {"Secret": "sec"}
        assert (api.server_url, api.token, api.user_id) == ("http://jf.test", "new-tok", "u9")
        assert api.settings.username == "alice"
        assert api.settings.server_url == "http://jf.test"

    def test_nothing_changes_until_authenticated(self, api):
        api.session.post = MagicMock(return_value=_resp(200, {"Secret": "s", "Code": "1"}))
        api.quick_connect_initiate("http://jf.test")
        assert (api.server_url, api.token) == ("http://old.example", "stale-token")


# ── LoginView flow ────────────────────────────────────────────────


class _FakeProvider:
    kind = "jellyfin"

    def __init__(self, enabled=True, reachable=True):
        self.enabled, self.reachable = enabled, reachable
        self.approved = False
        self.poll_error = None
        self.finished = False

    def probe(self, url):
        return object() if self.reachable else None

    def quick_connect_available(self, url):
        return self.enabled

    def quick_connect_start(self, url):
        return "secret-1", "654321"

    def quick_connect_approved(self, url, secret):
        if self.poll_error:
            raise self.poll_error
        return self.approved

    def quick_connect_finish(self, url, secret):
        self.finished = True
        return AuthResult(server_url=url, user_id="u1", username="alice", access_token="t")


def _sync_run_async(fn, *args, on_result=None, on_error=None, **_kw):
    try:
        result = fn(*args)
    except Exception as e:  # noqa: BLE001 — mirror run_async's contract
        if on_error:
            on_error(e)
    else:
        if on_result:
            on_result(result)


@pytest.fixture
def view(qapp, isolated_settings, monkeypatch):
    from jellytoast import login_view

    monkeypatch.setattr(login_view, "run_async", _sync_run_async)
    v = login_view.LoginView()
    v._kind_combo.setCurrentIndex(v._kind_combo.findData("jellyfin"))
    v._server_field.setText("jf.test:8096")
    v.provider = _FakeProvider()
    monkeypatch.setattr(v, "_sync_server_scrobble_flags", lambda *a: None)
    v._signed = []
    v.signed_in.connect(lambda: v._signed.append(True))
    return v


class TestLoginFlow:
    def test_link_only_for_jellyfin(self, view):
        assert not view._qc_btn.isHidden()
        view._kind_combo.setCurrentIndex(view._kind_combo.findData("subsonic"))
        assert view._qc_btn.isHidden()

    def test_happy_path(self, view):
        view._start_quick_connect()
        assert view._server_field.text() == "http://jf.test:8096"
        assert not view._qc_panel.isHidden()
        assert view._qc_code.text() == "654321"
        assert view._qc_timer.isActive()
        # Credential rows make way for the code panel while waiting.
        assert view._password_field.isHidden() and view._username_field.isHidden()
        view._qc_poll()  # not yet approved
        assert not view._signed and view._qc_timer.isActive()
        view.provider.approved = True
        view._qc_poll()
        assert view.provider.finished
        assert view._signed == [True]
        assert view._qc_panel.isHidden() and not view._qc_timer.isActive()
        assert view._username_field.text() == "alice"

    def test_cancel_ignores_late_replies(self, view):
        view._start_quick_connect()
        gen = view._qc_gen
        view._cancel_quick_connect()
        assert view._qc_panel.isHidden() and not view._submitting
        assert not view._password_field.isHidden()
        view._on_qc_polled(gen, True)  # a reply from the cancelled attempt
        assert not view.provider.finished and not view._signed

    def test_disabled_on_server(self, view):
        view.provider.enabled = False
        view._start_quick_connect()
        assert view._qc_panel.isHidden()
        assert "turned off" in view._error_label.text()
        assert not view._submitting

    def test_not_jellyfin(self, view):
        view.provider.reachable = False
        view._start_quick_connect()
        assert "Jellyfin server" in view._error_label.text()

    def test_expired_request(self, view):
        view._start_quick_connect()
        resp = MagicMock(status_code=404)
        view.provider.poll_error = requests.HTTPError("404", response=resp)
        view._qc_poll()
        assert "expired" in view._error_label.text()
        assert view._qc_panel.isHidden() and not view._qc_timer.isActive()

    def test_gives_up_after_the_deadline(self, view):
        view._start_quick_connect()
        view._qc_deadline = 0
        view._qc_poll()
        assert "expired" in view._error_label.text()

    def test_needs_a_server_url(self, view):
        view._server_field.setText("")
        view._start_quick_connect()
        assert view._qc_panel.isHidden()
        assert view._error_label.isVisibleTo(view)
