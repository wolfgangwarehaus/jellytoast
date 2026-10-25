"""The shared sign-in path (jellytoast/server_connect.py), against real local
HTTP servers playing Navidrome, Jellyfin, an ordinary website and a
redirecting proxy — the first-impression cases testers hit: the wrong server
type picked, a URL pasted from the web UI, no port, a typo'd host."""

from __future__ import annotations

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from jellytoast import server_connect as sc
from jellytoast.server_connect import ConnectError, candidate_urls, detect

# ── Addresses ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "typed, first",
    [
        ("192.168.1.5:4533", "http://192.168.1.5:4533"),
        ("http://nd.local:4533/app/#/album/abc", "http://nd.local:4533"),
        ("https://music.example.com/jellyfin/web/#/home.html", "https://music.example.com/jellyfin"),
        ("jf.local:8096/web/index.html", "http://jf.local:8096"),
        ("http://h:4533/rest/ping.view?u=x", "http://h:4533"),
        ("  http://h:4533/  ", "http://h:4533"),
        ("[fd00::1]:4533", "http://[fd00::1]:4533"),
    ],
)
def test_typed_addresses_are_cleaned_up(typed, first):
    assert candidate_urls(typed)[0] == first


def test_a_bare_host_also_tries_https_and_the_default_ports():
    assert candidate_urls("nas.local") == [
        "http://nas.local",
        "https://nas.local",
        "http://nas.local:8096",
        "https://nas.local:8920",
        "http://nas.local:4533",
    ]


def test_an_explicit_port_or_scheme_is_respected():
    assert candidate_urls("https://nas.local:8443") == ["https://nas.local:8443"]
    assert candidate_urls("nas.local:4533") == ["http://nas.local:4533", "https://nas.local:4533"]


@pytest.mark.parametrize("typed", ["", "   ", "ftp://x", "http://"])
def test_nothing_usable(typed):
    assert candidate_urls(typed) == []


# ── Fake servers ─────────────────────────────────────────────────────────


def _serve(handler_cls):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}"


def _json(h, obj, status=200):
    body = json.dumps(obj).encode()
    h.send_response(status)
    h.send_header("Content-Type", "application/json")
    h.send_header("Content-Length", str(len(body)))
    h.end_headers()
    h.wfile.write(body)


class _Navidrome(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):  # noqa: N802
        if self.path.startswith("/rest/ping"):
            _json(self, {"subsonic-response": {
                "status": "failed", "type": "navidrome", "serverVersion": "0.64.2",
                "error": {"code": 10, "message": "missing parameter"}}})
        else:
            self.send_response(404)
            self.end_headers()


class _Jellyfin(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):  # noqa: N802
        if self.path.startswith("/System/Info/Public"):
            _json(self, {"Id": "abc", "ProductName": "Jellyfin Server", "Version": "10.11"})
        else:
            self.send_response(404)
            self.end_headers()


class _Website(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):  # noqa: N802
        body = b"<html>hello</html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def servers():
    made = [_serve(c) for c in (_Navidrome, _Jellyfin, _Website)]
    yield {name: url for name, (_h, url) in zip(("navidrome", "jellyfin", "website"), made, strict=True)}
    for h, _u in made:
        h.shutdown()


def _closed_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# ── Detection ────────────────────────────────────────────────────────────


def test_finds_navidrome_even_with_jellyfin_picked(servers):
    found = detect(servers["navidrome"], preferred="jellyfin")
    assert (found.kind, found.url, found.product) == ("subsonic", servers["navidrome"], "Navidrome")


def test_finds_jellyfin_from_a_pasted_web_ui_url(servers):
    found = detect(servers["jellyfin"] + "/web/#/home.html", preferred="subsonic")
    assert (found.kind, found.url) == ("jellyfin", servers["jellyfin"])


def test_a_website_is_not_a_music_server(servers):
    with pytest.raises(ConnectError) as e:
        detect(servers["website"])
    assert e.value.code == ConnectError.NOT_SERVER


def test_nothing_listening_is_unreachable_not_wrong_server():
    with pytest.raises(ConnectError) as e:
        detect(f"http://127.0.0.1:{_closed_port()}")
    assert e.value.code == ConnectError.UNREACHABLE
    assert "Couldn't reach 127.0.0.1" in sc.message_for(e.value)


def test_the_picked_kind_wins_when_a_host_runs_both(monkeypatch, servers):
    # A host with Jellyfin on one default port and Navidrome on another:
    # map the bare host's candidates onto the two fake servers.
    monkeypatch.setattr(sc, "candidate_urls", lambda raw: [servers["jellyfin"], servers["navidrome"]])
    assert detect("nas", preferred="subsonic").kind == "subsonic"
    assert detect("nas", preferred="jellyfin").kind == "jellyfin"
    assert detect("nas").kind == "jellyfin"  # no pick: the earlier address


def test_redirects_are_followed_to_the_address_that_answered(servers):
    target = servers["navidrome"]

    class _Proxy(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):  # noqa: N802
            self.send_response(301)
            self.send_header("Location", target + self.path)
            self.end_headers()

    httpd, url = _serve(_Proxy)
    try:
        assert detect(url, preferred="subsonic").url == target
    finally:
        httpd.shutdown()


# ── Signing in ───────────────────────────────────────────────────────────


class _Prov:
    def __init__(self, kind, fail=None):
        self.kind = kind
        self.fail = fail
        self.calls = []

    def authenticate(self, url, user, pw):
        self.calls.append((url, user))
        if self.fail:
            raise self.fail


@pytest.fixture
def signin(monkeypatch, isolated_settings, servers):
    state = {"prov": _Prov("jellyfin"), "resets": 0}

    def reset():
        state["resets"] += 1
        from jellytoast.settings import get_settings

        state["prov"] = _Prov(get_settings().provider_kind, state["prov"].fail)

    monkeypatch.setattr("jellytoast.providers.get_provider", lambda: state["prov"])
    monkeypatch.setattr("jellytoast.providers.reset_provider", reset)
    return state


def test_sign_in_switches_to_the_detected_kind(signin, servers):
    from jellytoast.settings import get_settings

    found = sc.sign_in(servers["navidrome"], "august", "pw", preferred="jellyfin")
    assert found.kind == "subsonic"
    assert get_settings().provider_kind == "subsonic"
    assert signin["prov"].kind == "subsonic"
    assert signin["prov"].calls == [(servers["navidrome"], "august")]
    assert get_settings().server_is_navidrome is True


@pytest.mark.parametrize(
    "exc, code",
    [
        (lambda: __import__("jellytoast.providers.subsonic", fromlist=["x"]).SubsonicError(
            40, "Wrong username or password"), ConnectError.WRONG_CREDENTIALS),
        (lambda: RuntimeError("401 Client Error: Unauthorized"), ConnectError.WRONG_CREDENTIALS),
        (lambda: __import__("jellytoast.providers.subsonic", fromlist=["x"]).SubsonicError(
            50, "not authorized for this server"), ConnectError.SERVER_ERROR),
    ],
)
def test_sign_in_failures_are_classified(signin, servers, exc, code):
    signin["prov"].fail = exc()
    with pytest.raises(ConnectError) as e:
        sc.sign_in(servers["jellyfin"], "august", "bad")
    assert e.value.code == code


def test_http_401_from_jellyfin_is_wrong_credentials(signin, servers):
    import requests

    resp = requests.Response()
    resp.status_code = 401
    signin["prov"].fail = requests.exceptions.HTTPError(response=resp)
    with pytest.raises(ConnectError) as e:
        sc.sign_in(servers["jellyfin"], "august", "bad")
    assert e.value.code == ConnectError.WRONG_CREDENTIALS
    assert sc.message_for(e.value) == "Wrong username or password."


def test_missing_fields(signin):
    with pytest.raises(ConnectError) as e:
        sc.sign_in("", "august", "pw")
    assert e.value.code == ConnectError.MISSING_FIELDS


@pytest.mark.parametrize("code", [
    ConnectError.UNREACHABLE, ConnectError.TIMEOUT, ConnectError.TLS, ConnectError.NOT_SERVER,
    ConnectError.WRONG_CREDENTIALS, ConnectError.SERVER_ERROR, ConnectError.MISSING_FIELDS,
])
def test_every_failure_has_a_sentence(code):
    msg = sc.message_for(ConnectError(code, "nas.local", "boom"))
    assert msg and "{" not in msg
