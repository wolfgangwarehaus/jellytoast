"""Signing in to a server — the one path both front ends use.

The desktop LoginView and the Qt Quick sign-in page used to carry their own
copies of "probe the URL, then authenticate", with different error wording,
and both inherited the same first-impression traps:

* The user had to pick Jellyfin vs Navidrome / Subsonic first; the wrong pick
  failed with "doesn't look like a Jellyfin server" against a perfectly good
  URL.
* Only ``http://`` was ever added. A bare host without its port, an
  https-only server, or a URL pasted from the server's web UI
  (``…:8096/web/#/home.html``, ``…:4533/app/#/album/…``) all failed.
* The providers' probes swallow every exception and return None, so an
  unreachable host (a typo, the server is down) was reported as "That URL
  responded but doesn't look like a … server".

Here: ``candidate_urls`` turns what was typed into the addresses worth
trying; ``detect`` probes every candidate for BOTH server kinds in parallel
and keeps the first that answers (in candidate order; the user's pick only
breaks a tie); ``sign_in`` then authenticates with the matching provider.
Failures are a ``ConnectError`` with a ``code``, and ``message_for`` is the
single place that words them for the user.

Network work runs on the caller's thread — call these from a worker
(``run_async``), never the GUI thread.
"""

from __future__ import annotations

import concurrent.futures as cf
import logging
import re
from dataclasses import dataclass
from typing import List, Optional
from urllib.parse import urlsplit, urlunsplit

logger = logging.getLogger(__name__)

KINDS = ("jellyfin", "subsonic")
# Ports tried when the address has none: Jellyfin's http / https defaults,
# then Navidrome's. A reverse-proxied server sits on 80 / 443 (the address
# as typed), which is always tried first.
_DEFAULT_PORTS = (8096, 8920, 4533)
PROBE_TIMEOUT_S = 6.0
# Connect is quicker to judge than read: a filtered port (common on 80 /
# 443 / the default ports of a server the user isn't running) would
# otherwise hold the whole detection for the full read allowance.
PROBE_CONNECT_TIMEOUT_S = 3.0


class ConnectError(Exception):
    UNREACHABLE = "unreachable"
    TIMEOUT = "timeout"
    TLS = "tls"
    NOT_SERVER = "not_server"
    WRONG_CREDENTIALS = "wrong_credentials"
    SERVER_ERROR = "server_error"
    MISSING_FIELDS = "missing_fields"

    def __init__(self, code: str, host: str = "", detail: str = ""):
        super().__init__(f"{code}: {host} {detail}".strip())
        self.code = code
        self.host = host
        self.detail = detail


@dataclass
class Found:
    kind: str  # "jellyfin" | "subsonic"
    url: str  # the base URL that answered — what to save
    product: str = ""  # "Navidrome", "Jellyfin Server", …
    version: str = ""


# ── Addresses ────────────────────────────────────────────────────────────

# Path tails that belong to a server's web UI or API rather than its base:
# Jellyfin's /web/…, Navidrome's /app/…, the Subsonic API's /rest/…, and a
# bare index.html. Anything else (a reverse-proxy base path such as
# /jellyfin) is kept.
_UI_TAIL = re.compile(r"/(?:web|app|rest)(?:/.*)?$|/index\.html?$", re.IGNORECASE)


def _netloc(host: str, port: Optional[int]) -> str:
    h = f"[{host}]" if ":" in host else host  # IPv6 literal
    return f"{h}:{port}" if port else h


def candidate_urls(raw: str) -> List[str]:
    """The base URLs worth probing for what the user typed, most likely
    first. Empty when nothing usable was typed."""
    s = (raw or "").strip()
    if not s:
        return []
    s = s.split("#", 1)[0]  # web-UI routes (…/web/#/home.html)
    scheme_given = "://" in s
    if not scheme_given:
        s = "http://" + s
    try:
        parts = urlsplit(s)
        host = parts.hostname or ""
        port = parts.port
    except ValueError:
        return []
    if not host:
        return []
    scheme = (parts.scheme or "http").lower()
    if scheme not in ("http", "https"):
        return []
    path = _UI_TAIL.sub("", parts.path or "").rstrip("/")

    out: List[str] = []

    def add(sch: str, prt: Optional[int]) -> None:
        url = urlunsplit((sch, _netloc(host, prt), path, "", ""))
        if url not in out:
            out.append(url)

    add(scheme, port)
    if not scheme_given:
        add("https", port)
    if port is None:
        for p in _DEFAULT_PORTS:
            add("https" if (scheme_given and scheme == "https") or p == 8920 else "http", p)
    return out


# ── Probing ──────────────────────────────────────────────────────────────


def _classify_request_error(e: Exception) -> str:
    import requests

    if isinstance(e, requests.exceptions.SSLError):
        return ConnectError.TLS
    if isinstance(e, (requests.exceptions.ConnectTimeout, requests.exceptions.ReadTimeout)):
        return ConnectError.TIMEOUT
    if isinstance(e, requests.exceptions.ConnectionError):
        return ConnectError.UNREACHABLE
    return ConnectError.NOT_SERVER


def _answered_base(resp, endpoint: str, fallback: str) -> str:
    """The base URL that actually answered — after redirects (http → https,
    a proxy adding a path), so sign-in and every later request go there
    directly instead of tripping the redirect again (a redirected POST
    turns into a GET and the sign-in fails)."""
    final = getattr(resp, "url", "") or ""
    i = final.find(endpoint)
    return final[:i].rstrip("/") if i > 0 else fallback


def _probe(url: str, kind: str):
    """One unauthenticated probe. Returns ``Found`` or an error code."""
    import requests

    try:
        if kind == "subsonic":
            # /rest/ping answers without credentials (status "failed",
            # code 10/40), and the body proves it's a Subsonic server.
            r = requests.get(
                f"{url}/rest/ping",
                params={"v": "1.16.1", "c": "jellytoast", "f": "json"},
                timeout=(PROBE_CONNECT_TIMEOUT_S, PROBE_TIMEOUT_S),
            )
            body = r.json().get("subsonic-response") if r.content else None
            if isinstance(body, dict) and body.get("status") in ("ok", "failed"):
                product = body.get("type") or "Subsonic"
                return Found(
                    "subsonic", _answered_base(r, "/rest/ping", url), product.capitalize(),
                    body.get("serverVersion", ""),
                )
            return ConnectError.NOT_SERVER
        r = requests.get(
            f"{url}/System/Info/Public", timeout=(PROBE_CONNECT_TIMEOUT_S, PROBE_TIMEOUT_S)
        )
        info = r.json() if r.status_code == 200 and r.content else None
        if isinstance(info, dict) and "Id" in info and "ProductName" in info:
            return Found(
                "jellyfin", _answered_base(r, "/System/Info/Public", url),
                info.get("ProductName", ""), info.get("Version", ""),
            )
        return ConnectError.NOT_SERVER
    except ValueError:  # an HTTP answer that isn't JSON: some other web server
        return ConnectError.NOT_SERVER
    except Exception as e:
        return _classify_request_error(e)


# Which failure to report when nothing answered as a server, most useful
# first: a TLS problem or "it answered, but it's not a music server" says
# more than "unreachable".
_ERROR_RANK = (
    ConnectError.TLS,
    ConnectError.NOT_SERVER,
    ConnectError.TIMEOUT,
    ConnectError.UNREACHABLE,
)


def detect(raw: str, preferred: Optional[str] = None) -> Found:
    """Find the server behind ``raw``: every candidate URL × both kinds,
    probed in parallel. With a ``preferred`` kind (the user's pick), a
    server of that kind wins wherever it answered — a host can run both
    (Jellyfin on 8096 and Navidrome on 4533), and the pick says which one
    was meant; the other kind is the fallback for a wrong pick. Without
    one, the earliest candidate wins. Raises ``ConnectError`` when
    nothing answered as a server."""
    cands = candidate_urls(raw)
    if not cands:
        raise ConnectError(ConnectError.MISSING_FIELDS)
    host = urlsplit(cands[0]).hostname or ""
    kinds = sorted(KINDS, key=lambda k: k != preferred)
    # Rank key: kind first when the user picked one, else address first.
    jobs = [
        ((ki, i) if preferred else (i, ki), url, kind)
        for i, url in enumerate(cands)
        for ki, kind in enumerate(kinds)
    ]
    found: dict = {}
    errors: dict = {}
    # Not a `with` block: leaving one waits for every probe still running,
    # so a quick answer would sit out the slow candidates' timeouts.
    pool = cf.ThreadPoolExecutor(max_workers=len(jobs))
    try:
        futs = {pool.submit(_probe, url, kind): rank for rank, url, kind in jobs}
        pending = set(futs)
        for fut in cf.as_completed(futs):
            pending.discard(fut)
            key = futs[fut]
            res = fut.result()
            if isinstance(res, Found):
                found[key] = res
            else:
                errors[key] = res
            # Stop as soon as no unfinished probe could beat the best answer.
            if found:
                best = min(found)
                if all(futs[p] > best for p in pending):
                    return found[best]
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    if found:
        return found[min(found)]
    # Report what happened to the address as typed, if it said anything
    # useful; otherwise the most informative failure overall.
    typed = [errors[rank] for rank, url, _k in jobs if url == cands[0] and rank in errors]
    for pool_ in (typed, list(errors.values())):
        for code in _ERROR_RANK:
            if code in pool_:
                raise ConnectError(code, host)
    raise ConnectError(ConnectError.UNREACHABLE, host)


# ── Signing in ───────────────────────────────────────────────────────────


def _classify_auth_error(e: Exception, host: str) -> ConnectError:
    import requests

    from jellytoast.providers.subsonic import SubsonicError

    if isinstance(e, ConnectError):
        return e
    if isinstance(e, SubsonicError):
        if e.code in (40, 41, 44):
            return ConnectError(ConnectError.WRONG_CREDENTIALS, host)
        return ConnectError(ConnectError.SERVER_ERROR, host, e.message)
    if isinstance(e, requests.exceptions.HTTPError):
        status = getattr(e.response, "status_code", 0) or 0
        if status in (401, 403):
            return ConnectError(ConnectError.WRONG_CREDENTIALS, host)
        if status == 404:
            return ConnectError(ConnectError.NOT_SERVER, host)
        return ConnectError(ConnectError.SERVER_ERROR, host, f"HTTP {status}")
    if isinstance(e, requests.exceptions.RequestException):
        return ConnectError(_classify_request_error(e), host)
    msg = str(e) or e.__class__.__name__
    if "401" in msg or "Unauthorized" in msg:
        return ConnectError(ConnectError.WRONG_CREDENTIALS, host)
    return ConnectError(ConnectError.SERVER_ERROR, host, msg)


def sign_in(raw: str, username: str, password: str, preferred: Optional[str] = None) -> Found:
    """Detect the server, switch the app to its kind, and authenticate
    (which persists the credentials). Returns what was found; raises
    ``ConnectError``. Worker thread only."""
    username = (username or "").strip()
    if not (raw or "").strip() or not username:
        raise ConnectError(ConnectError.MISSING_FIELDS)
    found = detect(raw, preferred)
    from jellytoast.providers import get_provider, reset_provider
    from jellytoast.settings import get_settings

    settings = get_settings()
    if get_provider().kind != found.kind:
        settings.provider_kind = found.kind
        reset_provider()
    host = urlsplit(found.url).hostname or ""
    try:
        get_provider().authenticate(found.url, username, password)
    except Exception as e:
        raise _classify_auth_error(e, host) from e
    settings.server_is_navidrome = "navidrome" in (found.product or "").lower()
    logger.info("signed in to %s %s at %s", found.product or found.kind, found.version, host)
    return found


def message_for(err: Exception) -> str:
    """The user-facing sentence for a sign-in failure."""
    from PySide6.QtCore import QCoreApplication

    def tr(s: str) -> str:
        return QCoreApplication.translate("ServerConnect", s)

    if not isinstance(err, ConnectError):
        err = ConnectError(ConnectError.SERVER_ERROR, detail=str(err) or err.__class__.__name__)
    host = err.host or tr("the server")
    code = err.code
    if code == ConnectError.MISSING_FIELDS:
        return tr("Please fill in the server address and username.")
    if code == ConnectError.WRONG_CREDENTIALS:
        return tr("Wrong username or password.")
    if code == ConnectError.UNREACHABLE:
        return tr("Couldn't reach {0}. Check the address, and that the server is running.").format(host)
    if code == ConnectError.TIMEOUT:
        return tr("{0} didn't answer in time. Check the address and your network.").format(host)
    if code == ConnectError.TLS:
        return tr(
            "Couldn't make a secure connection to {0} (a certificate problem). "
            "If the server doesn't use HTTPS, try http:// instead."
        ).format(host)
    if code == ConnectError.NOT_SERVER:
        return tr(
            "{0} answered, but it isn't a Jellyfin or Navidrome / Subsonic server. "
            "Check the address and port."
        ).format(host)
    detail = err.detail or ""
    return (
        tr("The server couldn't sign you in: {0}").format(detail)
        if detail
        else tr("The server couldn't sign you in.")
    )
