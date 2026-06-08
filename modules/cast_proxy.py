"""
Local cast proxy — bridges a cast device to a media server it can't
reach directly.

The cast device (Chromecast / AirPlay receiver) and this machine share
a LAN; this machine *also* reaches the media server — possibly over
Tailscale, a public domain, or a self-signed-cert host the speaker
could never load. So when the server isn't directly LAN-reachable we
hand the speaker a URL pointing at a small HTTP server *here* and relay
the bytes through.

Routing is governed by the ``cast_stream_routing`` setting:
  auto   — direct when the server is a private LAN IP, proxy otherwise
  proxy  — always relay through this machine (max compatibility)
  direct — never relay; hand the speaker the server URL verbatim

Stdlib only — no extra deps. Range requests are forwarded both ways so
the cast device can still seek.

Security / threat model
-----------------------
This is a LAN-exposed HTTP relay whose job is to reach hosts the speaker
can't; the posture is permissive where it must be and tightened where it
can be without breaking that:
  * Bound to the **resolved LAN IP**, not ``0.0.0.0`` — so the relay isn't
    reachable over Tailscale / other attached networks, only the LAN the
    cast device shares. Re-binds on a network roam; falls back to all
    interfaces only if the specific bind fails (reachability never
    regresses). The advertised URL uses this same IP.
  * Stream URLs are gated by an unguessable per-stream bearer token
    (``secrets.token_urlsafe``), capped at a 256-entry LRU, and **expired
    when the cast session ends** so a token can't be replayed afterwards.
  * Upstream TLS is **verified by default**; a host whose certificate
    fails strict verification (a self-signed / internal-CA / Tailscale
    media server — the proxy's reason to exist) is relayed unverified and
    remembered for the session, so a valid cert is never silently
    accepted-without-checking the way a blanket ``CERT_NONE`` did.
  * ``file://`` blob serving is path-contained to the downloads root
    (resolved-path check + opening the resolved path — no traversal/TOCTOU).
Still hardware-gated (needs a real cast to confirm): none of the above
changes the happy path, but the LAN-IP bind + TLS-verify-first should be
eyeballed against a real device + a self-signed server once.
"""

import http.server
import logging
import os
import secrets
import socket
import ssl
import threading
import urllib.error
import urllib.request
from collections import OrderedDict
from ipaddress import ip_address, ip_network
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse
from urllib.request import url2pathname

logger = logging.getLogger(__name__)

# Fixed listening port for the relay. A *stable* port matters: it lets
# the user add a one-time firewall rule (e.g. `ufw allow 8943/tcp`)
# that survives across launches. If it's already taken we fall back to
# an ephemeral port — casting still works, but only if the firewall
# isn't in the way (logged loudly so the user knows).
_PROXY_PORT = 8943

# Private IPv4 ranges a cast device on the same LAN can fetch directly.
# 100.64.0.0/10 (Tailscale / CGNAT) is deliberately *excluded* — that's
# precisely the kind of address a speaker cannot route to, and the
# reason this proxy exists.
_DIRECT_NETS = (
    ip_network("192.168.0.0/16"),
    ip_network("10.0.0.0/8"),
    ip_network("172.16.0.0/12"),
)

# Response headers worth copying from upstream back to the cast device.
# Framing headers (Transfer-Encoding, Connection) are managed by
# http.server itself; auth echoes / cookies are intentionally dropped.
_PASS_RESPONSE_HEADERS = (
    "Content-Type",
    "Content-Length",
    "Content-Range",
    "Accept-Ranges",
    "Last-Modified",
    "ETag",
    "Cache-Control",
)


def _lan_ip() -> Optional[str]:
    """Best-effort IPv4 address of the interface carrying the default
    route — on a normal box that's the LAN address the cast device
    shares, not the Tailscale one (Tailscale only routes 100.64/10 +
    advertised subnets, so the default route stays on the physical
    NIC). Returns None if it can't be determined."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # No packet is actually sent for a UDP connect() — it just makes
        # the kernel pick the source address it *would* use.
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()
    if not ip or ip.startswith("127."):
        return None
    return ip


def _is_lan_direct_host(host: str) -> bool:
    """True when ``host`` is a private LAN IPv4 a same-LAN cast device
    can fetch directly. Hostnames and non-private / CGNAT IPs return
    False — in 'auto' mode those route through the proxy."""
    try:
        ip = ip_address(host)
    except ValueError:
        return False
    return any(ip in net for net in _DIRECT_NETS)


def _unverified_ctx() -> ssl.SSLContext:
    """An SSL context with verification + hostname checking OFF — the
    fallback for relaying from a self-signed / internal-CA media server.
    Used only after a host's cert has already failed strict verification
    (see ``_ProxyHandler._open_upstream``), never as the default."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _is_cert_error(err: BaseException) -> bool:
    """True when ``err`` (typically a ``URLError``) was caused by TLS
    certificate verification failing — the only case we downgrade to an
    unverified relay for. A plain connection refusal / timeout / DNS error
    is NOT a cert error and must keep propagating."""
    reason = getattr(err, "reason", err)
    if isinstance(reason, ssl.SSLCertVerificationError):
        return True
    if isinstance(reason, ssl.SSLError):
        return "CERTIFICATE" in str(reason).upper()
    return False


class _ProxyHandler(http.server.BaseHTTPRequestHandler):
    server_version = "jellytoast-cast-proxy/1"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        # Quieter than the stdlib default's stderr spew — one tagged line.
        logger.debug("%s %s", self.client_address[0], fmt % args)

    def _resolve(self) -> Optional[str]:
        # Path is /s/<token>; map it back to the registered upstream URL.
        parts = self.path.split("?", 1)[0].strip("/").split("/")
        if len(parts) != 2 or parts[0] != "s":
            return None
        return self.server.upstream_for(parts[1])  # type: ignore[attr-defined]

    def do_HEAD(self):
        self._proxy("HEAD")

    def do_GET(self):
        self._proxy("GET")

    def _proxy(self, method: str):
        upstream = self._resolve()
        if not upstream:
            self.send_error(404, "Unknown stream token")
            return
        # A downloaded track is a file:// blob — serve it straight off
        # disk (with Range support) rather than handing it to urllib's
        # file handler, which has no status and no Range. This is what
        # lets a downloaded track cast even with the server offline:
        # the bytes go this machine → speaker, server uninvolved.
        if upstream.startswith("file:"):
            self._serve_local_file(upstream, method)
            return
        req = urllib.request.Request(upstream, method=method)
        # Forward Range so the cast device can still seek.
        rng = self.headers.get("Range")
        if rng:
            req.add_header("Range", rng)
        req.add_header("User-Agent", "jellytoast-cast-proxy/1")
        host = urlparse(upstream).hostname or ""
        try:
            resp = self._open_upstream(req, host)
        except urllib.error.HTTPError as e:
            # Upstream answered with an error — forward the real status
            # so the cast device and our logs see the actual cause.
            logger.warning("upstream HTTP %s for %s", e.code, upstream)
            self.send_error(e.code, f"Upstream: {e.reason}")
            return
        except (urllib.error.URLError, OSError) as e:
            logger.warning("upstream unreachable: %s (%s)", e, upstream)
            self.send_error(502, "Upstream unreachable")
            return
        try:
            with resp:
                # Some response objects (e.g. urllib's file:// handler)
                # carry no HTTP status — default to 200 rather than
                # feeding None into send_response's %d formatting.
                self.send_response(getattr(resp, "status", None) or 200)
                for h in _PASS_RESPONSE_HEADERS:
                    v = resp.headers.get(h)
                    if v is not None:
                        self.send_header(h, v)
                # No length and not a range reply → fall back to
                # connection-close framing so the client knows where
                # the body ends under HTTP/1.1.
                if (
                    resp.headers.get("Content-Length") is None
                    and resp.headers.get("Content-Range") is None
                ):
                    self.send_header("Connection", "close")
                    self.close_connection = True
                self.end_headers()
                if method == "HEAD":
                    return
                while True:
                    chunk = resp.read(64 * 1024)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            # The cast device closed the connection (seek / skip / stop).
            # Normal — don't dump a traceback.
            self.close_connection = True
        except Exception as e:  # noqa: BLE001 - last-resort guard
            logger.warning("stream error: %s", e)
            self.close_connection = True

    def _open_upstream(self, req: urllib.request.Request, host: str):
        """``urlopen`` with TLS verification ON by default, falling back to
        UNVERIFIED only for a host whose certificate actually fails to verify.

        The proxy exists to reach the user's own media server — which may run a
        self-signed / internal-CA / Tailscale cert a strict client would
        reject. Rather than disable verification globally (the old posture),
        we verify first; on a genuine certificate-verification error we mark
        that host self-signed for the rest of the session and retry unverified.
        A real HTTP status (401/404/…) is re-raised, never retried. The
        per-host memo (``server.is_insecure_host``) means a known self-signed
        server skips straight to the unverified path on every later chunk/seek
        request — no doubled round-trip mid-stream. Raises like ``urlopen``."""
        if not req.full_url.lower().startswith("https"):
            return urllib.request.urlopen(req, timeout=15)
        server = self.server  # type: ignore[attr-defined]
        if server.is_insecure_host(host):
            return urllib.request.urlopen(req, timeout=15, context=_unverified_ctx())
        try:
            # No context → urllib's default verifying context (CERT_REQUIRED
            # + hostname check).
            return urllib.request.urlopen(req, timeout=15)
        except urllib.error.HTTPError:
            raise  # a real upstream status — forward it, don't downgrade TLS
        except urllib.error.URLError as e:
            if not _is_cert_error(e):
                raise
            server.mark_insecure_host(host)
            logger.info(
                "TLS verify failed for %s — relaying unverified (self-signed?)",
                host,
            )
            return urllib.request.urlopen(req, timeout=15, context=_unverified_ctx())

    def _serve_local_file(self, file_url: str, method: str):
        """Serve a downloaded blob off disk with HTTP Range support, so
        a cast device can stream *and seek* a downloaded track with the
        media server offline. Bytes go this machine → speaker; the
        server is never touched."""
        from modules.cast_manager import CastManager
        from modules.offline.locations import downloads_dir

        path = Path(url2pathname(urlparse(file_url).path))
        # Defense-in-depth: only serve files under the downloads root.
        # Today every caller composes file:// from `Blob.as_uri()` which
        # is rooted there; a future bug that registers any other path
        # would otherwise turn this into LAN-reachable arbitrary file
        # read.
        try:
            resolved = path.resolve(strict=True)
            resolved.relative_to(downloads_dir().resolve())
        except (OSError, ValueError):
            logger.warning("refusing path outside downloads: %s", path)
            self.send_error(404, "Not found")
            return
        try:
            # Open the *validated resolved* path, not the unresolved one:
            # opening `path` after validating `resolved` is a TOCTOU gap (a
            # symlink swapped between check and open could escape the root).
            f = open(resolved, "rb")
        except OSError as e:
            logger.warning("open failed: %s", e)
            self.send_error(404, "Local blob missing")
            return
        with f:
            size = os.fstat(f.fileno()).st_size
            ctype = (
                CastManager.chromecast_audio_mime_for(path.suffix.lstrip("."))
                or "application/octet-stream"
            )
            # Parse a single byte-range: "bytes=start-end" / "bytes=start-"
            # / "bytes=-suffix". Anything malformed → serve the whole file.
            start, end, partial = 0, size - 1, False
            unsatisfiable = False
            rng = self.headers.get("Range", "")
            if rng.startswith("bytes="):
                try:
                    s, _, e = rng[6:].partition("-")
                    if s == "" and e:  # suffix range ("bytes=-N")
                        start = max(0, size - int(e))
                    else:
                        start = int(s)
                        end = int(e) if e else size - 1
                        # A start at/beyond EOF is unsatisfiable (RFC 7233
                        # §4.4) → 416, NOT a silent fall-back to the whole
                        # file from byte 0 (which makes a renderer that
                        # sought past the end restart the track).
                        if start >= size:
                            unsatisfiable = True
                    if not unsatisfiable:
                        start = max(0, start)
                        end = min(end, size - 1)
                        partial = start <= end
                        if not partial:
                            # Reversed range (e.g. "bytes=5-3") — serve the
                            # whole file. Without this reset Content-Length
                            # would be -1.
                            start, end = 0, size - 1
                except ValueError:
                    start, end, partial = 0, size - 1, False
            if unsatisfiable:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            length = end - start + 1
            self.send_response(206 if partial else 200)
            self.send_header("Content-Type", ctype)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(length))
            if partial:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            if method == "HEAD":
                return
            try:
                f.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = f.read(min(64 * 1024, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
            except (BrokenPipeError, ConnectionResetError):
                self.close_connection = True
            except Exception as e:  # noqa: BLE001 - last-resort guard
                logger.warning("local-file stream error: %s", e)
                self.close_connection = True


class _ProxyServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr):
        super().__init__(addr, _ProxyHandler)
        self._tokens: "OrderedDict[str, str]" = OrderedDict()
        # Hosts whose TLS cert failed strict verification once this session →
        # relayed unverified from then on, so a self-signed media server
        # doesn't pay a failed-verify round-trip on every chunk/seek request.
        self._insecure_hosts: set[str] = set()
        self._lock = threading.Lock()

    def is_insecure_host(self, host: str) -> bool:
        with self._lock:
            return host in self._insecure_hosts

    def mark_insecure_host(self, host: str) -> None:
        with self._lock:
            self._insecure_hosts.add(host)

    def clear_tokens(self) -> None:
        """Drop every registered stream token (called when a cast session
        ends) so a token can't be replayed once the device is done with it.
        The server keeps running for the next session."""
        with self._lock:
            self._tokens.clear()

    def register(self, upstream_url: str) -> str:
        token = secrets.token_urlsafe(12)
        with self._lock:
            self._tokens[token] = upstream_url
            # Cap history so a long session doesn't leak. 256 entries is
            # ~128 tracks of (stream + cover) — the currently-playing
            # token is always among the most recent, never near eviction.
            while len(self._tokens) > 256:
                self._tokens.popitem(last=False)
        return token

    def upstream_for(self, token: str) -> Optional[str]:
        with self._lock:
            url = self._tokens.get(token)
            if url is not None:
                # LRU: promote on access so an actively-streamed token
                # (re-requested on every buffer refill / seek) can't age
                # out and get FIFO-evicted mid-playback.
                self._tokens.move_to_end(token)
            return url


class CastProxy:
    """Lazily-started singleton HTTP relay. Thread-safe."""

    def __init__(self):
        self._server: Optional[_ProxyServer] = None
        self._thread: Optional[threading.Thread] = None
        self._lan_ip: Optional[str] = None
        # The interface address the listening socket is actually bound to —
        # the resolved LAN IP normally, or "0.0.0.0" if a specific bind
        # failed and we fell back to all interfaces.
        self._bound_ip: Optional[str] = None
        self._lock = threading.Lock()

    def _make_server(self, bind_ip: str) -> Optional[_ProxyServer]:
        """Bind a _ProxyServer to ``bind_ip``, the fixed port first (so a
        one-time firewall rule stays valid) then an ephemeral fallback.
        Returns the server, or None if both binds fail."""
        for port in (_PROXY_PORT, 0):
            try:
                return _ProxyServer((bind_ip, port))
            except OSError as e:
                logger.warning("bind %s:%s failed: %s", bind_ip, port or "ephemeral", e)
        return None

    def _start_locked(self, bind_ip: str) -> bool:
        """Start (or restart) the listening server bound to ``bind_ip``.
        Falls back to ``0.0.0.0`` if the specific bind fails, so reachability
        never regresses below the old all-interfaces behaviour. Caller holds
        ``self._lock``."""
        server = self._make_server(bind_ip)
        bound = bind_ip
        if server is None and bind_ip != "0.0.0.0":
            logger.warning("binding %s failed — falling back to all interfaces", bind_ip)
            server = self._make_server("0.0.0.0")
            bound = "0.0.0.0"
        if server is None:
            logger.error("could not start — will cast direct")
            return False
        if server.server_address[1] != _PROXY_PORT:
            logger.warning(
                "on ephemeral port %s (port %s taken) — "
                "a fixed-port firewall rule won't match",
                server.server_address[1],
                _PROXY_PORT,
            )
        self._server = server
        self._bound_ip = bound
        self._thread = threading.Thread(
            target=server.serve_forever, name="cast-proxy", daemon=True
        )
        self._thread.start()
        logger.info(
            "listening on http://%s:%s (bound %s)",
            self._lan_ip,
            server.server_address[1],
            bound,
        )
        return True

    def _stop_locked(self) -> None:
        if self._server is not None:
            try:
                self._server.shutdown()
                self._server.server_close()
            except Exception:
                pass
        self._server = None
        self._thread = None

    def _ensure_started(self) -> bool:
        with self._lock:
            if self._server is not None:
                return True
            lan_ip = _lan_ip()
            if not lan_ip:
                logger.warning(
                    "no LAN IP to advertise — proxy unavailable, will cast direct"
                )
                return False
            # Bind the resolved LAN IP (not every interface) so the relay
            # isn't reachable over Tailscale / other attached networks — the
            # cast device is on the LAN and the advertised URL uses this same
            # IP. _start_locked falls back to 0.0.0.0 if that bind fails.
            self._lan_ip = lan_ip
            return self._start_locked(lan_ip)

    def proxy_url(self, upstream_url: str) -> Optional[str]:
        """Register ``upstream_url`` and return a LAN-reachable proxy URL
        for it, or None if the proxy couldn't start (caller should fall
        back to the upstream URL directly)."""
        if not upstream_url or not self._ensure_started():
            return None
        # Re-resolve the LAN IP on every call so a network change (Wi-Fi
        # roam, VPN up/down, DHCP lease change) doesn't keep advertising a
        # stale source address the cast device can no longer reach. Because
        # we now bind a *specific* IP, a roam also means the old socket is on
        # a stale interface — so rebind to the new IP (the 0.0.0.0 fallback
        # binding stays put; it already listens everywhere).
        current_ip = _lan_ip()
        with self._lock:
            if current_ip and current_ip != self._lan_ip:
                self._lan_ip = current_ip
                if self._bound_ip not in ("0.0.0.0", current_ip):
                    logger.info(
                        "LAN IP changed %s→%s — rebinding proxy",
                        self._bound_ip,
                        current_ip,
                    )
                    self._stop_locked()
                    if not self._start_locked(current_ip):
                        return None
            if not self._lan_ip or self._server is None:
                return None
            token = self._server.register(upstream_url)
            port = self._server.server_address[1]
            return f"http://{self._lan_ip}:{port}/s/{token}"

    def clear_tokens(self) -> None:
        """Expire all registered stream tokens — called when a cast session
        ends so a token can't be replayed afterwards. Keeps the server
        running for the next cast. No-op if the proxy never started."""
        with self._lock:
            if self._server is not None:
                self._server.clear_tokens()

    def stop(self):
        with self._lock:
            self._stop_locked()
            self._lan_ip = None
            self._bound_ip = None


_PROXY: Optional[CastProxy] = None


def get_cast_proxy() -> CastProxy:
    global _PROXY
    if _PROXY is None:
        _PROXY = CastProxy()
    return _PROXY


def resolve_cast_url(upstream_url: str) -> str:
    """Map a media-server URL to the URL a cast device should actually
    fetch, honoring the ``cast_stream_routing`` setting:

      direct → return ``upstream_url`` unchanged
      proxy  → always relay through this machine
      auto   → relay unless the server is a private LAN IP the cast
               device can already reach on its own

    A ``file://`` URL (a downloaded local blob) is *always* relayed
    regardless of the setting — a cast device can never read a file off
    this machine, so direct/auto don't apply. The proxy serves it off
    disk, which is also what makes casting a downloaded track work with
    the server offline.

    Any failure (proxy can't start, no LAN IP) degrades gracefully to
    the upstream URL — worst case is the pre-proxy behavior."""
    if not upstream_url:
        return upstream_url
    if upstream_url.startswith("file:"):
        return get_cast_proxy().proxy_url(upstream_url) or upstream_url
    from modules.settings import get_settings

    mode = get_settings().cast_stream_routing
    if mode == "direct":
        return upstream_url
    if mode == "auto":
        host = urlparse(upstream_url).hostname or ""
        if _is_lan_direct_host(host):
            return upstream_url
    # mode == "proxy", or auto + a host the speaker likely can't reach.
    return get_cast_proxy().proxy_url(upstream_url) or upstream_url
