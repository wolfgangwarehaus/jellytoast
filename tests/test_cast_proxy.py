"""Tests for the cast proxy's in-memory token map.

Only the parts that don't need a real cast device: token registration,
resolution, and the eviction policy. The ``_ProxyServer`` is bound to an
ephemeral localhost port (``("127.0.0.1", 0)``) so the test is fast and
firewall-independent; the HTTP handler / streaming path needs a real
renderer and is out of scope here.
"""

import http.client
import ssl
import threading
import urllib.error
import urllib.request

import pytest

from modules import cast_proxy
from modules.cast_proxy import (
    _is_cert_error,
    _ProxyHandler,
    _ProxyServer,
    _unverified_ctx,
)


@pytest.fixture
def server():
    s = _ProxyServer(("127.0.0.1", 0))  # ephemeral port, localhost only
    yield s
    s.server_close()


class TestTokenMap:
    def test_register_and_resolve(self, server):
        t = server.register("http://up/stream")
        assert server.upstream_for(t) == "http://up/stream"

    def test_unknown_token_is_none(self, server):
        assert server.upstream_for("nope") is None

    def test_eviction_is_lru_not_fifo(self, server):
        """#294: upstream_for promotes on access, so an actively-streamed
        token (re-requested on every buffer refill / seek) can't age out
        and get FIFO-evicted mid-playback. With the old non-promoting
        read, ``first`` would be the oldest entry and evicted."""
        first = server.register("http://up/0")
        for i in range(1, 256):  # fill to the 256-entry cap
            server.register(f"http://up/{i}")

        # Touch `first` so LRU promotes it off the eviction front.
        assert server.upstream_for(first) == "http://up/0"

        # One more registration trips the cap and evicts the LRU victim —
        # which must be the next-oldest, NOT the just-accessed `first`.
        server.register("http://up/new")
        assert server.upstream_for(first) == "http://up/0"


# ── Local blob serving (file:// downloads) over a live loopback server ───────
# The proxy serves downloaded blobs off disk with HTTP Range support so a cast
# device can stream + seek a downloaded track with the media server offline.
# This is the offline-cast moat AND a security boundary (it must NOT become a
# LAN-reachable arbitrary-file read). No real renderer needed — the test IS the
# HTTP client against a 127.0.0.1 ephemeral-port server.

_DATA = bytes(range(256)) * 8  # 2048 deterministic bytes


@pytest.fixture
def live_server():
    s = _ProxyServer(("127.0.0.1", 0))
    t = threading.Thread(target=s.serve_forever, daemon=True)
    t.start()
    try:
        yield s, s.server_address[1]
    finally:
        s.shutdown()
        s.server_close()


def _request(port, token, headers=None, method="GET"):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request(method, f"/s/{token}", headers=headers or {})
        resp = conn.getresponse()
        body = resp.read()
        return resp.status, {k.lower(): v for k, v in resp.getheaders()}, body
    finally:
        conn.close()


@pytest.fixture
def blob(offline_db):
    # offline_db redirects downloads_dir() to tmp_path; a blob under it passes
    # the path-containment check. Returns (path, file:// uri).
    f = offline_db / "track.mp3"
    f.write_bytes(_DATA)
    return f, f.as_uri()


class TestLocalBlobServing:
    def test_full_get_returns_whole_file(self, live_server, blob):
        srv, port = live_server
        _f, uri = blob
        status, hdrs, body = _request(port, srv.register(uri))
        assert status == 200
        assert body == _DATA
        assert hdrs["content-length"] == str(len(_DATA))
        assert hdrs["accept-ranges"] == "bytes"
        assert hdrs["content-type"] == "audio/mpeg"  # from the .mp3 suffix

    def test_mid_range_returns_206_partial(self, live_server, blob):
        srv, port = live_server
        status, hdrs, body = _request(port, srv.register(blob[1]), {"Range": "bytes=100-199"})
        assert status == 206
        assert body == _DATA[100:200]
        assert hdrs["content-length"] == "100"
        assert hdrs["content-range"] == f"bytes 100-199/{len(_DATA)}"

    def test_open_ended_range_runs_to_eof(self, live_server, blob):
        srv, port = live_server
        status, hdrs, body = _request(port, srv.register(blob[1]), {"Range": "bytes=2000-"})
        assert status == 206
        assert body == _DATA[2000:]
        assert hdrs["content-range"] == f"bytes 2000-{len(_DATA) - 1}/{len(_DATA)}"

    def test_suffix_range_returns_last_n_bytes(self, live_server, blob):
        srv, port = live_server
        status, hdrs, body = _request(port, srv.register(blob[1]), {"Range": "bytes=-50"})
        assert status == 206
        assert body == _DATA[-50:]
        assert hdrs["content-range"] == f"bytes {len(_DATA) - 50}-{len(_DATA) - 1}/{len(_DATA)}"

    def test_unsatisfiable_range_returns_416(self, live_server, blob):
        # A start at/beyond EOF must be 416 (RFC 7233), not a silent whole-file
        # fall-back that restarts a renderer that sought past the end.
        srv, port = live_server
        status, hdrs, body = _request(port, srv.register(blob[1]), {"Range": f"bytes={len(_DATA)}-"})
        assert status == 416
        assert hdrs["content-range"] == f"bytes */{len(_DATA)}"
        assert body == b""

    def test_reversed_range_falls_back_to_whole_file(self, live_server, blob):
        srv, port = live_server
        status, _hdrs, body = _request(port, srv.register(blob[1]), {"Range": "bytes=5-3"})
        assert status == 200
        assert body == _DATA

    def test_head_sends_headers_but_no_body(self, live_server, blob):
        srv, port = live_server
        status, hdrs, body = _request(port, srv.register(blob[1]), method="HEAD")
        assert status == 200
        assert hdrs["content-length"] == str(len(_DATA))
        assert body == b""

    def test_path_traversal_outside_root_is_refused(self, live_server, offline_db):
        # THE security boundary: a file:// outside the downloads root must 404,
        # never be served — otherwise the proxy is a LAN-reachable arbitrary
        # file read. offline_db's downloads root is tmp_path; put the secret in
        # its parent so it's provably outside.
        secret = offline_db.parent / "secret.txt"
        secret.write_bytes(b"TOPSECRET")
        srv, port = live_server
        status, _hdrs, body = _request(port, srv.register(secret.as_uri()))
        assert status == 404
        assert b"TOPSECRET" not in body

    def test_missing_blob_under_root_is_404(self, live_server, offline_db):
        srv, port = live_server
        token = srv.register((offline_db / "ghost.mp3").as_uri())
        status, _hdrs, _body = _request(port, token)
        assert status == 404

    def test_unknown_token_is_404(self, live_server):
        srv, port = live_server
        status, _hdrs, _body = _request(port, "bogus-token")
        assert status == 404


# ── Hardening (2026-06-07): token expiry, TLS verify-first, LAN-IP bind ──────
# Logic-level tests for the cast-proxy security hardening. No real cast device
# or TLS server needed — urlopen is stubbed and the socket bind is faked.


class _Stub:
    """Minimal stand-in for a _ProxyHandler — _open_upstream only touches
    ``self.server``."""

    def __init__(self, server):
        self.server = server


def _cert_urlerror():
    return urllib.error.URLError(ssl.SSLCertVerificationError("certificate verify failed"))


class TestTokenExpiry:
    def test_clear_tokens_empties_map(self, server):
        t = server.register("http://up/x")
        assert server.upstream_for(t) == "http://up/x"
        server.clear_tokens()
        assert server.upstream_for(t) is None, "a token must not survive cast-stop"


class TestInsecureHostMemo:
    def test_mark_and_query(self, server):
        assert not server.is_insecure_host("h")
        server.mark_insecure_host("h")
        assert server.is_insecure_host("h")


class TestUnverifiedCtx:
    def test_disables_verification(self):
        ctx = _unverified_ctx()
        assert ctx.verify_mode == ssl.CERT_NONE
        assert ctx.check_hostname is False


class TestCertErrorDetection:
    def test_cert_verify_failure_detected(self):
        assert _is_cert_error(urllib.error.URLError(ssl.SSLCertVerificationError("x")))
        assert _is_cert_error(urllib.error.URLError(ssl.SSLError("CERTIFICATE_VERIFY_FAILED")))

    def test_non_cert_failures_ignored(self):
        assert not _is_cert_error(urllib.error.URLError(ConnectionRefusedError("no")))
        assert not _is_cert_error(urllib.error.URLError("timed out"))


class TestTlsVerifyFirst:
    """Upstream TLS is verified by default; only a host whose cert genuinely
    fails verification is downgraded to an unverified relay (and remembered),
    so a valid cert is never silently accepted-without-checking the way a
    blanket CERT_NONE did."""

    def test_verify_first_then_fallback_on_cert_error(self, server, monkeypatch):
        calls = []
        sentinel = object()

        def fake_urlopen(req, timeout=None, context=None):
            calls.append(context)
            if context is None:  # the verifying first attempt
                raise _cert_urlerror()
            return sentinel  # the unverified retry succeeds

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        req = urllib.request.Request("https://selfsigned.example/x")
        out = _ProxyHandler._open_upstream(_Stub(server), req, "selfsigned.example")
        assert out is sentinel
        assert calls[0] is None  # verified first
        assert calls[1] is not None and calls[1].verify_mode == ssl.CERT_NONE
        assert server.is_insecure_host("selfsigned.example")  # remembered

    def test_known_insecure_host_skips_straight_to_unverified(self, server, monkeypatch):
        server.mark_insecure_host("selfsigned.example")
        calls = []
        sentinel = object()

        def fake_urlopen(req, timeout=None, context=None):
            calls.append(context)
            return sentinel

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        req = urllib.request.Request("https://selfsigned.example/x")
        out = _ProxyHandler._open_upstream(_Stub(server), req, "selfsigned.example")
        assert out is sentinel
        assert len(calls) == 1 and calls[0].verify_mode == ssl.CERT_NONE

    def test_connection_refused_is_not_downgraded(self, server, monkeypatch):
        calls = []

        def fake_urlopen(req, timeout=None, context=None):
            calls.append(context)
            raise urllib.error.URLError(ConnectionRefusedError("refused"))

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        req = urllib.request.Request("https://valid.example/x")
        with pytest.raises(urllib.error.URLError):
            _ProxyHandler._open_upstream(_Stub(server), req, "valid.example")
        assert len(calls) == 1  # no insecure retry on a non-cert error
        assert not server.is_insecure_host("valid.example")

    def test_http_error_is_forwarded_not_downgraded(self, server, monkeypatch):
        calls = []

        def fake_urlopen(req, timeout=None, context=None):
            calls.append(context)
            raise urllib.error.HTTPError("https://valid.example/x", 401, "Unauthorized", {}, None)

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        req = urllib.request.Request("https://valid.example/x")
        with pytest.raises(urllib.error.HTTPError):
            _ProxyHandler._open_upstream(_Stub(server), req, "valid.example")
        assert len(calls) == 1  # a real HTTP status is forwarded, never retried

    def test_plain_http_never_uses_a_context(self, server, monkeypatch):
        calls = []
        sentinel = object()

        def fake_urlopen(req, timeout=None, context=None):
            calls.append(context)
            return sentinel

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        req = urllib.request.Request("http://plain.example/x")
        out = _ProxyHandler._open_upstream(_Stub(server), req, "plain.example")
        assert out is sentinel
        assert calls == [None]  # no TLS context for plain http


class TestLanIpBind:
    """The relay binds the resolved LAN IP, not 0.0.0.0 — but never regresses
    reachability: a failed specific bind falls back to all interfaces."""

    def test_start_falls_back_to_all_interfaces(self, monkeypatch):
        p = cast_proxy.CastProxy()
        p._lan_ip = "192.168.1.50"
        made = []

        class _FakeSrv:
            server_address = ("0.0.0.0", cast_proxy._PROXY_PORT)

            def serve_forever(self):  # runs once on the daemon thread, exits
                return

        def fake_make(bind_ip):
            made.append(bind_ip)
            return _FakeSrv() if bind_ip == "0.0.0.0" else None  # specific bind fails

        monkeypatch.setattr(p, "_make_server", fake_make)
        assert p._start_locked("192.168.1.50") is True
        assert made == ["192.168.1.50", "0.0.0.0"], "must try the LAN IP, then fall back"
        assert p._bound_ip == "0.0.0.0"

    def test_binds_lan_ip_when_available(self, monkeypatch):
        p = cast_proxy.CastProxy()
        p._lan_ip = "192.168.1.50"
        made = []

        class _FakeSrv:
            server_address = ("192.168.1.50", cast_proxy._PROXY_PORT)

            def serve_forever(self):
                return

        def fake_make(bind_ip):
            made.append(bind_ip)
            return _FakeSrv()

        monkeypatch.setattr(p, "_make_server", fake_make)
        assert p._start_locked("192.168.1.50") is True
        assert made == ["192.168.1.50"]  # no fallback needed
        assert p._bound_ip == "192.168.1.50"
