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
"""

import http.server
import secrets
import socket
import ssl
import threading
import urllib.error
import urllib.request
from collections import OrderedDict
from ipaddress import ip_address, ip_network
from typing import Optional
from urllib.parse import urlparse

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
    "Content-Type", "Content-Length", "Content-Range",
    "Accept-Ranges", "Last-Modified", "ETag", "Cache-Control",
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


class _ProxyHandler(http.server.BaseHTTPRequestHandler):
    server_version = "JellyToastCastProxy/1"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        # Quieter than the stdlib default's stderr spew — one tagged line.
        print(f"[cast-proxy] {self.client_address[0]} {fmt % args}",
              flush=True)

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
        req = urllib.request.Request(upstream, method=method)
        # Forward Range so the cast device can still seek.
        rng = self.headers.get("Range")
        if rng:
            req.add_header("Range", rng)
        req.add_header("User-Agent", "JellyToastCastProxy/1")
        ctx = None
        if upstream.lower().startswith("https"):
            # This proxy exists to paper over exactly the servers a cast
            # device can't reach — self-signed-cert hosts included. It's
            # the user's own media server over a link they control, so
            # skip verification rather than fail the cast.
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        try:
            resp = urllib.request.urlopen(req, timeout=15, context=ctx)
        except urllib.error.HTTPError as e:
            # Upstream answered with an error — forward the real status
            # so the cast device and our logs see the actual cause.
            print(f"[cast-proxy] upstream HTTP {e.code} for {upstream}",
                  flush=True)
            self.send_error(e.code, f"Upstream: {e.reason}")
            return
        except (urllib.error.URLError, OSError) as e:
            print(f"[cast-proxy] upstream unreachable: {e} ({upstream})",
                  flush=True)
            self.send_error(502, "Upstream unreachable")
            return
        try:
            with resp:
                self.send_response(resp.status)
                for h in _PASS_RESPONSE_HEADERS:
                    v = resp.headers.get(h)
                    if v is not None:
                        self.send_header(h, v)
                # No length and not a range reply → fall back to
                # connection-close framing so the client knows where
                # the body ends under HTTP/1.1.
                if (resp.headers.get("Content-Length") is None
                        and resp.headers.get("Content-Range") is None):
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
            print(f"[cast-proxy] stream error: {e}", flush=True)
            self.close_connection = True


class _ProxyServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr):
        super().__init__(addr, _ProxyHandler)
        self._tokens: "OrderedDict[str, str]" = OrderedDict()
        self._lock = threading.Lock()

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
            return self._tokens.get(token)


class CastProxy:
    """Lazily-started singleton HTTP relay. Thread-safe."""

    def __init__(self):
        self._server: Optional[_ProxyServer] = None
        self._thread: Optional[threading.Thread] = None
        self._lan_ip: Optional[str] = None
        self._lock = threading.Lock()

    def _ensure_started(self) -> bool:
        with self._lock:
            if self._server is not None:
                return True
            lan_ip = _lan_ip()
            if not lan_ip:
                print("[cast-proxy] no LAN IP to advertise — proxy "
                      "unavailable, will cast direct", flush=True)
                return False
            # Bind every interface so the LAN IP is reachable. Try the
            # fixed port first so a one-time firewall rule stays valid;
            # fall back to an ephemeral port if it's taken.
            server = None
            for port in (_PROXY_PORT, 0):
                try:
                    server = _ProxyServer(("0.0.0.0", port))
                    break
                except OSError as e:
                    print(f"[cast-proxy] port "
                          f"{port or 'ephemeral'} unavailable: {e}",
                          flush=True)
            if server is None:
                print("[cast-proxy] could not start — will cast direct",
                      flush=True)
                return False
            if server.server_address[1] != _PROXY_PORT:
                print(f"[cast-proxy] NOTE: on ephemeral port "
                      f"{server.server_address[1]} (port {_PROXY_PORT} "
                      f"taken) — a fixed-port firewall rule won't match",
                      flush=True)
            self._server = server
            self._lan_ip = lan_ip
            self._thread = threading.Thread(
                target=server.serve_forever, name="cast-proxy",
                daemon=True,
            )
            self._thread.start()
            print(f"[cast-proxy] listening on "
                  f"http://{lan_ip}:{server.server_address[1]}", flush=True)
            return True

    def proxy_url(self, upstream_url: str) -> Optional[str]:
        """Register ``upstream_url`` and return a LAN-reachable proxy URL
        for it, or None if the proxy couldn't start (caller should fall
        back to the upstream URL directly)."""
        if not upstream_url or not self._ensure_started():
            return None
        assert self._server is not None and self._lan_ip is not None
        token = self._server.register(upstream_url)
        port = self._server.server_address[1]
        return f"http://{self._lan_ip}:{port}/s/{token}"

    def stop(self):
        with self._lock:
            if self._server is not None:
                try:
                    self._server.shutdown()
                    self._server.server_close()
                except Exception:
                    pass
                self._server = None
                self._thread = None
                self._lan_ip = None


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

    Any failure (proxy can't start, no LAN IP) degrades gracefully to
    the upstream URL — worst case is the pre-proxy behavior."""
    if not upstream_url:
        return upstream_url
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
