"""SubsonicProvider — Subsonic 1.16.1 + OpenSubsonic compatible
backend. Targets Navidrome primarily but should work against any
server that implements the Subsonic API faithfully (Gonic, LMS,
Funkwhale, Ampache, AirSonic).

Auth: token+salt mode. The user's password is held in our keyring
the same way the Jellyfin access token is; per-request we compute
``t = md5(password + salt)`` with a fresh random salt so the
password itself never leaks across the wire.

Adapter: Subsonic returns lowercase camelCase JSON; views in this
codebase consume Jellyfin-shape PascalCase dicts. The
``_adapt_album`` / ``_adapt_artist`` / ``_adapt_song`` helpers
project Subsonic responses into Jellyfin shape so no view code
needs to change. Phase 2 of the provider work will normalize both
backends to a jellytoast-internal schema and retire the adapter.
"""

import hashlib
import logging
import secrets
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

import requests

from modules.providers.base import AuthResult, MediaProvider, ServerInfo
from modules.settings import get_settings
from modules.sort_utils import article_stripped_key

logger = logging.getLogger(__name__)


CLIENT_NAME = "jellytoast"
PROTOCOL_VERSION = "1.16.1"


def _year_bounds(op: str, value: Any) -> tuple:
    """Translate a (op, value) pair for the year field into the
    (fromYear, toYear) inclusive range Subsonic's getAlbumList2 wants.

    `between` accepts the [lo, hi] pair the rule schema validates.
    `greater_than` / `less_than` are *strict* in the schema, so we
    nudge the bound by 1 to keep the endpoint inclusive.
    """
    if op == "equals":
        return int(value), int(value)
    if op == "greater_than":
        return int(value) + 1, 9999
    if op == "less_than":
        return 0, int(value) - 1
    if op == "between":
        lo, hi = int(value[0]), int(value[1])
        return min(lo, hi), max(lo, hi)
    raise ValueError(f"unsupported year op {op!r}")


def _is_server_mappable(rule: Dict[str, Any]) -> bool:
    """True iff Subsonic has a native endpoint that can filter on
    ``rule``. Mirrors the cases handled in
    ``SubsonicProvider._query_single_native``."""
    field = rule.get("field")
    op = rule.get("op")
    if field == "genre" and op == "equals":
        return True
    if field == "year" and op in (
        "equals",
        "greater_than",
        "less_than",
        "between",
    ):
        return True
    if field == "is_favorite" and op == "equals":
        # Only `equals True` maps to getStarred2; `equals False` has no
        # native "give me everything not starred" endpoint, so it falls
        # to the broad fetch + Python refine.
        return bool(rule.get("value"))
    return False


class SubsonicError(Exception):
    """Raised when a Subsonic JSON response carries status=failed.
    The .code attribute is the Subsonic error code from the spec
    (40 = wrong creds, 41 = LDAP token-auth disallowed, 50+ = various)."""

    def __init__(self, code: int, message: str):
        super().__init__(f"Subsonic error {code}: {message}")
        self.code = code
        self.message = message


def _build_query(params: dict) -> str:
    """URL-encode params, dropping only ``None`` values. Callers omit the
    params they don't want (the ``if x: params[k] = x`` guard pattern used
    throughout), so an explicit empty string is INTENTIONAL and must survive
    to the wire — notably search3's ``query=""`` match-all convention, which
    Navidrome / strict OpenSubsonic require to be PRESENT (an absent
    ``query`` is a code-10 missing-required-parameter error). Dropping ``""``
    here previously stripped it and silently defeated the all-songs
    pagination (#10), degrading the Songs view to a single random page."""
    clean = {k: v for k, v in params.items() if v is not None}
    return urlencode(clean)


class SubsonicProvider(MediaProvider):
    """Subsonic / OpenSubsonic / Navidrome backend."""

    # Music-only server: an empty ``musicFolderId`` already returns the
    # union of every music folder, so the empty parent IS the "all
    # libraries" query. The multi-library selection layer relies on this
    # to keep the all/both case a single unfiltered round-trip.
    scopes_music_by_library = False

    def __init__(self):
        self.settings = get_settings()
        self.session = requests.Session()
        # Size the connection pool to the run_async worker pool (8): the
        # library grid fans many parallel _request calls through this one
        # shared session, and urllib3's default pool (10, but with
        # block=False it silently discards+reopens connections once full)
        # under that contention produced spurious connection errors that
        # tripped the connectivity monitor offline. A pool that matches
        # the fan-out keeps those requests on warm keep-alive connections.
        _adapter = requests.adapters.HTTPAdapter(
            pool_connections=16, pool_maxsize=16
        )
        self.session.mount("http://", _adapter)
        self.session.mount("https://", _adapter)
        self._server_url = self.settings.server_url.rstrip("/")
        # Subsonic doesn't have a separate user_id — username is the
        # identifier. We persist the username under user_id for
        # consistency with the Jellyfin code path so the LoginView /
        # Settings / Account page logic doesn't have to branch.
        self._username = self.settings.username or self.settings.user_id
        self._password = self.settings.access_token
        # LDAP-backed servers reject token+salt (error 41); authenticate()
        # falls back to plain-password auth and persists that choice.
        # Restore it here so a provider rebuilt on launch keeps using plain
        # auth instead of reverting to token auth and looping into logout.
        self._auth_mode_plain = self.settings.subsonic_auth_mode_plain

    # ── Identity ──────────────────────────────────────────────────────

    @property
    def kind(self) -> str:
        return "subsonic"

    @property
    def server_url(self) -> str:
        return self._server_url

    @property
    def user_id(self) -> str:
        return self._username

    @property
    def access_token(self) -> str:
        return self._password

    @property
    def device_id(self) -> str:
        return self.settings.device_id

    @property
    def is_authenticated(self) -> bool:
        # Backfill from settings on the first read after construction.
        # `settings.access_token` does its own dual-store (keyring +
        # QSettings) lookup, so this read is fast and resilient even
        # when the keyring is sleepy on boot. Gated by `_backfill_done`
        # so a confirmed-empty store doesn't re-read on every property
        # access.
        if not self._password and not getattr(self, "_backfill_done", False):
            self._password = self.settings.access_token
            self._backfill_done = True
        ok = bool(self._username and self._password and self._server_url)
        if not getattr(self, "_boot_auth_logged", False):
            logger.info(
                "boot-auth: url=%s user=%s token_len=%s is_auth=%s",
                "set" if self._server_url else "empty",
                "set" if self._username else "empty",
                len(self._password),
                ok,
            )
            self._boot_auth_logged = True
        return ok

    # ── Auth helpers ──────────────────────────────────────────────────

    def _auth_params(self) -> dict:
        """Per-request auth params for token+salt mode. A fresh salt
        is generated each call (Subsonic spec requires ≥6 random
        characters; we use 16 hex chars for headroom). Combined with
        ``c=jellytoast`` this also stamps every request as belonging
        to our client in Navidrome's logs / per-player profiles.

        Note: ``f`` (response format) is deliberately *not* included
        here — JSON formatting is REST-only. Including ``f=json`` on
        stream / getCoverArt URLs makes Navidrome and friends return
        a JSON error envelope instead of raw audio / image bytes, so
        mpv plays nothing and image decoders see invalid data. The
        ``_request`` helper adds ``f=json`` itself; ``_build_url``
        does not."""
        if getattr(self, "_auth_mode_plain", False):
            # LDAP-backed account: the server rejected token+salt (error
            # 41), so every request must carry the plain password (``p=``).
            # Relies on HTTPS for transport security, same as the spec's
            # plain scheme. Without this branch, post-login requests revert
            # to token auth and the server keeps returning 41 → logout loop.
            return {
                "u": self._username,
                "p": self._password,
                "v": PROTOCOL_VERSION,
                "c": CLIENT_NAME,
            }
        salt = secrets.token_hex(8)
        token = hashlib.md5((self._password + salt).encode("utf-8")).hexdigest()
        return {
            "u": self._username,
            "t": token,
            "s": salt,
            "v": PROTOCOL_VERSION,
            "c": CLIENT_NAME,
        }

    def _build_url(
        self, path: str, params: Optional[dict] = None, server_url: Optional[str] = None
    ) -> str:
        """Construct a fully-authed Subsonic URL for binary endpoints
        (stream, getCoverArt). No ``f=json`` — the server returns raw
        bytes for these and including the format param can flip it
        into a JSON error envelope instead."""
        full = dict(self._auth_params())
        if params:
            full.update(params)
        url = (server_url or self._server_url).rstrip("/")
        return f"{url}/rest/{path}?{_build_query(full)}"

    def _request(
        self, path: str, params: Optional[dict] = None, server_url: Optional[str] = None
    ) -> dict:
        """GET a Subsonic JSON endpoint, returning the inner
        subsonic-response dict. Raises ``SubsonicError`` on a failed
        status, ``requests`` exceptions on network errors.

        Feeds the connectivity tracker (modules.offline) on the way out
        so reachable/unreachable transitions are observable. A
        SubsonicError or HTTPError counts as ``note_success`` because
        the server *is* reachable — only the request was wrong; a
        ``RequestException`` (timeout, DNS, connection refused) is the
        real "network down" signal."""
        full = dict(self._auth_params())
        full["f"] = "json"
        if params:
            full.update(params)
        base = (server_url or self._server_url).rstrip("/")
        url = f"{base}/rest/{path}?{_build_query(full)}"
        # Split connect/read timeout: a *gone* server (host unreachable
        # / Pi powered off) fails the connect in ~3s instead of hanging
        # the full read budget; a slow-but-alive server still gets 15s
        # to answer. Without the short connect timeout, every call to a
        # dead server stalls its caller for 15s.
        from modules import offline as _offline

        try:
            r = self.session.get(url, timeout=(3.05, 15))
        except requests.exceptions.RequestException:
            _offline.note_request_failure()
            raise
        _offline.note_request_success()
        r.raise_for_status()
        body = r.json() if r.content else {}
        resp = body.get("subsonic-response", {})
        if resp.get("status") == "failed":
            err = resp.get("error") or {}
            code = err.get("code", -1)
            if code in (40, 41):
                # Definitive auth-reject — feed the auth-failure tracker
                # so the UI can drop to LoginView after a string of
                # these. Silent "No albums yet" with bad stored creds
                # is the worst stuck state to leave the user in.
                _offline.note_auth_failure()
            raise SubsonicError(
                code,
                err.get("message", "Unknown Subsonic error"),
            )
        # Subsonic response was "ok" → auth worked. Reset the
        # auth-failure counter so a transient code-40 earlier in the
        # session doesn't accumulate toward the threshold.
        _offline.note_auth_success()
        return resp

    # ── Auth tier ─────────────────────────────────────────────────────

    def probe(self, server_url: str) -> Optional[ServerInfo]:
        """Pre-auth ping. Subsonic's /rest/ping responds even without
        credentials (it returns a failed status with code 10 "missing
        required parameter username"), and the response carries the
        server type + version regardless. We use that to confirm a
        Subsonic-compatible server lives at the URL."""
        try:
            qs = _build_query(
                {
                    "v": PROTOCOL_VERSION,
                    "c": CLIENT_NAME,
                    "f": "json",
                }
            )
            url = f"{server_url.rstrip('/')}/rest/ping?{qs}"
            r = requests.get(url, timeout=5)
            r.raise_for_status()
            body = r.json() if r.content else {}
            resp = body.get("subsonic-response")
        except Exception:
            return None
        if not resp:
            return None
        # Either status confirms a Subsonic server. The "failed"
        # path is what we'll typically see (no creds in this probe)
        # but the response shape proves the URL is valid.
        if resp.get("status") not in ("ok", "failed"):
            return None
        # OpenSubsonic adds a 'type' field; vanilla Subsonic doesn't.
        # Server name comes back from getLicense after auth; the probe
        # only sees the protocol version and server's daemon name.
        product = resp.get("type", "Subsonic")
        return ServerInfo(
            server_id=server_url.rstrip("/"),  # no formal ServerId pre-auth
            name=resp.get("serverVersion", "") or product,
            product_name=product,
            version=resp.get("version", ""),
            raw=resp,
        )

    def authenticate(self, server_url: str, username: str, password: str) -> AuthResult:
        """Sign in by sending a credentialed ping. Subsonic has no
        separate login endpoint — every request is independently
        authenticated. We use ping as a credential test."""
        self._server_url = server_url.rstrip("/")
        self._username = username
        self._password = password
        # Reset to token auth; the error-41 fallback below flips it to plain.
        self._auth_mode_plain = False
        try:
            self._request("ping")
        except SubsonicError as e:
            self._username = ""
            self._password = ""
            # Code 41: server requires plain-password auth (LDAP). Try
            # a fallback request with p=password instead of t/s.
            if e.code == 41:
                # Re-attempt with plain password embedded in URL.
                # This is unfortunate but it's what the spec calls
                # for. Persist the password and rely on HTTPS for
                # transport security.
                self._username = username
                self._password = password
                # _auth_params() honors this flag and emits plain (p=)
                # auth for every subsequent request; persisted below so it
                # survives a relaunch.
                self._auth_mode_plain = True
                self._request_plain("ping", username, password)
                # If we got here, plain-pass auth works. Persist.
            else:
                raise
        # Verified — persist
        self.settings.server_url = self._server_url
        self.settings.username = username
        self.settings.user_id = username
        self.settings.access_token = password
        self.settings.provider_kind = "subsonic"
        self.settings.subsonic_auth_mode_plain = self._auth_mode_plain
        # Force-flush QSettings to disk RIGHT NOW. Auto-sync is on a
        # periodic timer + a destructor flush, but the destructor only
        # fires on a clean QCoreApplication shutdown — the tray-Quit
        # path tears the app down in ways that bypass it on KDE, and
        # any unflushed credential writes are silently lost. The token
        # survives via the keyring daemon; username/user_id are
        # QSettings-only and were the missing piece making next-boot
        # is_auth=False even though the token round-tripped.
        self.settings.flush()
        return AuthResult(
            server_url=self._server_url,
            user_id=username,
            username=username,
            access_token=password,
        )

    def _request_plain(self, path: str, username: str, password: str) -> dict:
        """Plain-password auth fallback (Subsonic error 41 path).
        Used only when token+salt is rejected because the user is
        backed by LDAP."""
        params = {
            "u": username,
            "p": password,
            "v": PROTOCOL_VERSION,
            "c": CLIENT_NAME,
            "f": "json",
        }
        url = f"{self._server_url}/rest/{path}?{_build_query(params)}"
        r = self.session.get(url, timeout=15)
        r.raise_for_status()
        body = r.json() if r.content else {}
        resp = body.get("subsonic-response", {})
        if resp.get("status") == "failed":
            err = resp.get("error") or {}
            raise SubsonicError(
                err.get("code", -1),
                err.get("message", "Unknown Subsonic error"),
            )
        return resp

    def verify_session(self) -> bool:
        # Trust persisted creds at boot. Confirmed 2026-05-16 on the
        # user's Navidrome: the first /rest/ping after a process
        # restart can return subsonic-response.status=failed + code 40
        # ("wrong password") even when the password is bit-for-bit
        # identical to what the user just successfully signed in with
        # (verified via SHA-256 fingerprints — see boot-auth log
        # diagnostics). The same credentials submitted via authenticate
        # are accepted immediately. Likely a Navidrome-side per-IP /
        # per-session cache quirk after the previous process's
        # connections were dropped.
        #
        # Matches Jellyfin's actual philosophy: only return False when
        # we KNOW credentials are gone (no username, no token, no URL).
        # If the persisted token is truly bad, the user will discover
        # that on the first real API call (which will show empty
        # surfaces or a 401-equivalent) and can re-auth then. Better
        # than a false-positive logout that forces re-entering the
        # exact same password the dual-store already has correct.
        return self.is_authenticated

    def server_logout(self) -> bool:
        """No-op. Subsonic is stateless per request — there's no
        session to revoke server-side. Local credential clearing
        (handled by the host) is sufficient."""
        return True

    def with_url(self, new_url: str) -> "SubsonicProvider":
        """Re-point this provider at ``new_url``. Subsonic is stateless
        per request, so swapping the base URL leaves auth (username +
        password) intact and the next ``_request`` lands at the new
        host immediately. Returns ``self`` for chaining."""
        self._server_url = (new_url or "").rstrip("/")
        return self

    # ── Adapters ──────────────────────────────────────────────────────

    @staticmethod
    def _adapt_album(s: dict) -> Dict[str, Any]:
        """Subsonic album JSON → Jellyfin-shape dict. The fields
        we project are the ones the views actually read; everything
        else stays in _subsonic_raw if a view ever needs to peek."""
        return {
            "Id": s.get("id", ""),
            "Name": s.get("name", "") or s.get("title", ""),
            "Type": "MusicAlbum",
            "AlbumArtist": s.get("artist", ""),
            "AlbumArtists": (
                [{"Id": s.get("artistId", ""), "Name": s.get("artist", "")}]
                if s.get("artistId")
                else []
            ),
            "ProductionYear": s.get("year"),
            "PremiereDate": "",
            "ChildCount": s.get("songCount", 0),
            "Genres": [s.get("genre")] if s.get("genre") else [],
            "ImageTags": ({"Primary": s.get("coverArt")} if s.get("coverArt") else {}),
            "UserData": {
                "IsFavorite": bool(s.get("starred")),
                "PlayCount": s.get("playCount", 0),
            },
            "_subsonic_raw": s,
        }

    @staticmethod
    def _adapt_artist(s: dict) -> Dict[str, Any]:
        return {
            "Id": s.get("id", ""),
            "Name": s.get("name", ""),
            "Type": "MusicArtist",
            "ChildCount": s.get("albumCount", 0),
            "Genres": [],
            "ImageTags": ({"Primary": s.get("coverArt")} if s.get("coverArt") else {}),
            "UserData": {"IsFavorite": bool(s.get("starred"))},
            "_subsonic_raw": s,
        }

    @staticmethod
    def _adapt_song(s: dict) -> Dict[str, Any]:
        # Jellyfin's RunTimeTicks is 100ns; Subsonic's duration is
        # whole seconds. Convert.
        duration_ticks = (s.get("duration", 0) or 0) * 10_000_000
        return {
            "Id": s.get("id", ""),
            "Name": s.get("title", "") or s.get("name", ""),
            "Type": "Audio",
            "MediaType": "Audio",
            # Container = file extension (Subsonic's `suffix`). Drives
            # Chromecast direct-play MIME lookup; anything outside the
            # Cast SDK's supported set falls through to a transcode.
            "Container": (s.get("suffix") or "").lower(),
            "Album": s.get("album", ""),
            "AlbumId": s.get("albumId", ""),
            "AlbumPrimaryImageTag": s.get("coverArt"),
            "AlbumArtist": s.get("artist", ""),
            "Artists": [s.get("artist", "")] if s.get("artist") else [],
            "ArtistItems": (
                [{"Id": s.get("artistId", ""), "Name": s.get("artist", "")}]
                if s.get("artistId")
                else []
            ),
            "IndexNumber": s.get("track"),
            "ParentIndexNumber": s.get("discNumber") or 1,
            "ProductionYear": s.get("year"),
            "RunTimeTicks": duration_ticks,
            "Genres": [s.get("genre")] if s.get("genre") else [],
            # Subsonic's per-song `created` timestamp (ISO 8601) maps
            # onto Jellyfin's DateCreated so the smart-rule date_added
            # field refines uniformly across backends. Subsonic has no
            # per-track last-played timestamp, so LastPlayedDate is
            # deliberately absent — a `last_played` rule simply never
            # matches on Subsonic (documented in smart_rule_schema).
            "DateCreated": s.get("created"),
            "UserData": {
                "IsFavorite": bool(s.get("starred")),
                "PlayCount": s.get("playCount", 0),
            },
            "_subsonic_raw": s,
        }

    # ── Browse tier ───────────────────────────────────────────────────

    def get_libraries(self) -> List[Dict[str, Any]]:
        """Subsonic music folders → Jellyfin-shaped library list. We
        fake CollectionType="music" since Subsonic doesn't have a
        type taxonomy beyond music."""
        try:
            resp = self._request("getMusicFolders")
        except Exception:
            return []
        folders = (resp.get("musicFolders") or {}).get("musicFolder") or []
        return [
            {
                "Id": str(f.get("id", "")),
                "Name": f.get("name", ""),
                "CollectionType": "music",
            }
            for f in folders
        ]

    def get_items(
        self,
        parent_id: str = "",
        item_type: str = "",
        limit: int = 100,
        start_index: int = 0,
        sort_by: str = "SortName",
        sort_order: str = "Ascending",
        recursive: bool = False,
        genre_ids: str = "",
        filters: str = "",
        years: str = "",
        timeout: int = 15,
    ) -> Dict[str, Any]:
        """Multi-purpose browse — switches on item_type. Maps to
        Subsonic's getAlbumList2 / search3 / getStarred2 etc."""
        if item_type == "MusicAlbum":
            return self._get_albums(
                parent_id=parent_id,
                limit=limit,
                start_index=start_index,
                sort_by=sort_by,
                sort_order=sort_order,
                genre_id=genre_ids,
                filters=filters,
                year=years,
            )
        if item_type == "MusicArtist":
            # Subsonic's getArtists returns an indexed list (by
            # alphabet bucket); flattened to a single list of
            # adapted artist dicts. Forward parent_id so the Artists
            # surface honours the multi-library picker exactly like
            # Albums/Songs do (without it, selecting one library left
            # the Artists tab showing every folder's artists).
            artists = self.get_artists(
                limit=limit or 200,
                start_index=start_index,
                parent_id=parent_id,
            )
            return {"Items": artists, "TotalRecordCount": len(artists)}
        if item_type == "Audio":
            return self._get_songs(
                parent_id=parent_id,
                limit=limit,
                start_index=start_index,
                genre_id=genre_ids,
            )
        if item_type == "Playlist":
            return self._get_playlists()
        return {"Items": [], "TotalRecordCount": 0}

    def _get_albums(
        self,
        parent_id: str,
        limit: int,
        start_index: int,
        sort_by: str,
        sort_order: str,
        genre_id: str,
        filters: str,
        year: str = "",
    ) -> Dict[str, Any]:
        # Map Jellyfin SortBy keys to Subsonic getAlbumList2 types.
        # Subsonic's set is fixed; we pick the closest equivalent
        # and rely on client-side re-sort (already in library_grid)
        # for compound sorts that don't map.
        first_key = (sort_by or "SortName").split(",", 1)[0]
        filter_set = {f.strip() for f in (filters or "").split(",") if f.strip()}
        # Content filters (IsFavorite / IsPlayed) win over the sort key:
        # they select the getAlbumList2 *type* that actually narrows the
        # set (starred / frequent); the requested sort is then applied
        # client-side below. Checking them AFTER the sort-key branches used
        # to silently drop the filter under any mapped sort — Favorites +
        # sort-by-AlbumArtist returned the WHOLE library — and left the
        # AlbumArtist client re-sort branch dead. Cross-provider parity
        # break vs Jellyfin, which applies IsFavorite regardless of sort.
        if "IsFavorite" in filter_set:
            kind = "starred"
        elif "IsPlayed" in filter_set:
            kind = "frequent"  # closest to "played"
        elif first_key == "AlbumArtist":
            kind = "alphabeticalByArtist"
        elif first_key == "PremiereDate" or first_key == "ProductionYear":
            kind = "byYear"
        elif first_key == "DateCreated":
            kind = "newest"
        elif first_key == "DatePlayed":
            kind = "recent"
        elif first_key == "Random":
            kind = "random"
        else:
            kind = "alphabeticalByName"
        params = {
            "type": kind,
            "size": min(limit, 500),  # Subsonic caps at 500
            "offset": start_index,
        }
        if genre_id:
            params["type"] = "byGenre"
            params["genre"] = genre_id
        if year:
            # byYear with same fromYear/toYear narrows to a single
            # year. Overrides the sort-derived type — the user clicked
            # the year specifically, so the filter wins over the sort.
            try:
                y = int(year)
                params["type"] = "byYear"
                params["fromYear"] = y
                params["toYear"] = y
            except (TypeError, ValueError):
                pass
        if params["type"] == "byYear" and "fromYear" not in params:
            # Sort-by-year without an explicit year filter: Subsonic's
            # byYear requires a fromYear/toYear pair or it returns no
            # results. Default to the widest range so the server still
            # returns the full library, sorted chronologically. Order
            # matters — fromYear < toYear is "ascending", flipping
            # them inverts the result order, which lets us honor the
            # jellytoast sort-direction toggle.
            if sort_order == "Descending":
                params["fromYear"] = 9999
                params["toYear"] = 0
            else:
                params["fromYear"] = 0
                params["toYear"] = 9999
        if parent_id:
            params["musicFolderId"] = parent_id
        try:
            resp = self._request("getAlbumList2", params)
        except Exception:
            return {"Items": [], "TotalRecordCount": 0}
        albums = (resp.get("albumList2") or {}).get("album") or []
        items = [self._adapt_album(a) for a in albums]
        if kind == "starred" and first_key in ("SortName", "AlbumArtist"):
            # getAlbumList2 type=starred returns server star-date order and
            # ignores our sort key. Re-sort client-side so the Favorites
            # rail matches the requested SortName/AlbumArtist ordering —
            # and the Jellyfin provider, which sends SortBy to the server
            # (cross-provider parity). Article-stripped so "The X" sorts
            # under X, matching library_grid.
            field = "AlbumArtist" if first_key == "AlbumArtist" else "Name"
            items = sorted(
                items,
                key=lambda it: article_stripped_key(it.get(field) or ""),
                reverse=(sort_order == "Descending"),
            )
        return {"Items": items, "TotalRecordCount": len(items)}

    def _get_songs(
        self, parent_id: str, limit: int, start_index: int, genre_id: str
    ) -> Dict[str, Any]:
        if genre_id:
            params = {"genre": genre_id, "count": min(limit, 500), "offset": start_index}
            if parent_id:
                params["musicFolderId"] = parent_id
            try:
                resp = self._request("getSongsByGenre", params)
            except Exception:
                return {"Items": [], "TotalRecordCount": 0}
            songs = (resp.get("songsByGenre") or {}).get("song") or []
        else:
            # No genre filter: list the whole library. Subsonic has no
            # dedicated "all songs" endpoint, so we use search3 with an
            # empty query — the Navidrome / OpenSubsonic convention for a
            # full-library song list — because it honours ``songOffset`` and
            # therefore paginates DETERMINISTICALLY (parity with Jellyfin's
            # all-songs view). The previous default, getRandomSongs, ignores
            # the offset and re-rolls an overlapping random batch on every
            # page; since it always returns a full ``size`` batch,
            # songs_view's ``len < PAGE_SIZE`` tail-stop never tripped on a
            # >500-track library → duplicate rows + endless pagination (#10).
            size = min(limit, 500)
            params = {
                "query": "",
                "songCount": size,
                "songOffset": start_index,
                "albumCount": 0,
                "artistCount": 0,
            }
            if parent_id:
                params["musicFolderId"] = parent_id
            try:
                resp = self._request("search3", params)
            except Exception:
                return {"Items": [], "TotalRecordCount": 0}
            songs = (resp.get("searchResult3") or {}).get("song") or []
            if not songs and start_index == 0:
                # Some legacy Subsonic / Airsonic builds don't treat an
                # empty query as match-all and return nothing. Fall back to a
                # single random page so the Songs view isn't empty. Because
                # an empty search3 at any offset > 0 returns [] (handled
                # above), the background cascade still stops after this one
                # page instead of re-rolling random batches forever.
                fallback = self.get_random_audio_items(parent_id, size)
                return {"Items": fallback, "TotalRecordCount": len(fallback)}
        items = [self._adapt_song(s) for s in songs]
        return {"Items": items, "TotalRecordCount": len(items)}

    def _get_playlists(self) -> Dict[str, Any]:
        try:
            resp = self._request("getPlaylists")
        except Exception:
            return {"Items": [], "TotalRecordCount": 0}
        pls = (resp.get("playlists") or {}).get("playlist") or []
        items = [
            {
                "Id": str(p.get("id", "")),
                "Name": p.get("name", ""),
                "Type": "Playlist",
                "ChildCount": p.get("songCount", 0),
                "Genres": [],
                "ImageTags": ({"Primary": p.get("coverArt")} if p.get("coverArt") else {}),
                "UserData": {"IsFavorite": False},
                "_subsonic_raw": p,
            }
            for p in pls
        ]
        return {"Items": items, "TotalRecordCount": len(items)}

    def get_item(self, item_id: str) -> Dict[str, Any]:
        """Best-effort item lookup. Tries getAlbum first (album +
        songs), falls back to getArtist, then getSong. Returns the
        adapted dict; empty {} on miss."""
        try:
            resp = self._request("getAlbum", {"id": item_id})
            album = resp.get("album")
            if album:
                return self._adapt_album(album)
        except Exception:
            pass
        try:
            resp = self._request("getArtist", {"id": item_id})
            artist = resp.get("artist")
            if artist:
                return self._adapt_artist(artist)
        except Exception:
            pass
        try:
            resp = self._request("getSong", {"id": item_id})
            song = resp.get("song")
            if song:
                return self._adapt_song(song)
        except Exception:
            pass
        return {}

    def get_album_tracks(self, album_id: str) -> List[Dict[str, Any]]:
        try:
            resp = self._request("getAlbum", {"id": album_id})
        except Exception:
            return []
        album = resp.get("album") or {}
        songs = album.get("song") or []
        return [self._adapt_song(s) for s in songs]

    def get_artist_albums(self, artist_id: str) -> List[Dict[str, Any]]:
        try:
            resp = self._request("getArtist", {"id": artist_id})
        except Exception:
            return []
        artist = resp.get("artist") or {}
        albums = artist.get("album") or []
        return [self._adapt_album(a) for a in albums]

    def get_artists(
        self, limit: int = 200, start_index: int = 0, parent_id: str = ""
    ) -> List[Dict[str, Any]]:
        params: Dict[str, Any] = {}
        if parent_id:
            # Subsonic/OpenSubsonic getArtists accepts musicFolderId to scope
            # to one library — parity with _get_albums/_get_songs. A server
            # that ignores it just returns the union (the prior behaviour).
            params["musicFolderId"] = parent_id
        try:
            resp = self._request("getArtists", params)
        except Exception:
            return []
        # Subsonic indexes artists alphabetically: artists.index is a
        # list of {name, artist:[...]}. Flatten and slice.
        indices = (resp.get("artists") or {}).get("index") or []
        flat: List[Dict[str, Any]] = []
        for idx in indices:
            for a in idx.get("artist") or []:
                flat.append(self._adapt_artist(a))
        if start_index:
            flat = flat[start_index:]
        if limit and limit > 0:
            flat = flat[:limit]
        return flat

    def get_playlist_items(self, playlist_id: str) -> List[Dict[str, Any]]:
        try:
            resp = self._request("getPlaylist", {"id": playlist_id})
        except Exception:
            return []
        pl = resp.get("playlist") or {}
        entries = pl.get("entry") or []
        return [self._adapt_song(s) for s in entries]

    def get_genres(self) -> List[Dict[str, Any]]:
        try:
            resp = self._request("getGenres")
        except Exception:
            return []
        gens = (resp.get("genres") or {}).get("genre") or []
        return [
            {
                "Id": g.get("value", ""),  # Subsonic genres key by name
                "Name": g.get("value", ""),
                "Type": "MusicGenre",
            }
            for g in gens
        ]

    def get_resume_items(self, limit: int = 12, media_type: str = "") -> List[Dict[str, Any]]:
        """No real Subsonic equivalent — closest is "recently played"
        which we surface elsewhere. Return empty so the rail
        self-hides on the suggestions surface."""
        return []

    def get_latest_media(self, library_id: str = "", limit: int = 16) -> List[Dict[str, Any]]:
        """Maps to getAlbumList2?type=newest."""
        params = {"type": "newest", "size": min(limit, 500)}
        if library_id:
            params["musicFolderId"] = library_id
        try:
            resp = self._request("getAlbumList2", params)
        except Exception:
            return []
        albums = (resp.get("albumList2") or {}).get("album") or []
        return [self._adapt_album(a) for a in albums]

    def search(self, term: str, limit: int = 50, item_types: str = "") -> List[Dict[str, Any]]:
        """search3 returns three buckets at once. We flatten to the
        bucket the caller asked for via item_types — matches the
        Jellyfin search() shape used by the native search view."""
        params = {
            "query": term,
            "songCount": limit if "Audio" in item_types else 0,
            "albumCount": limit if "MusicAlbum" in item_types else 0,
            "artistCount": limit if "MusicArtist" in item_types else 0,
        }
        # If no specific type asked, fetch all three at modest counts.
        if not item_types:
            params["songCount"] = min(limit, 20)
            params["albumCount"] = min(limit, 20)
            params["artistCount"] = min(limit, 20)
        try:
            resp = self._request("search3", params)
        except Exception:
            return []
        result = resp.get("searchResult3") or {}
        out: List[Dict[str, Any]] = []
        if "MusicArtist" in item_types or not item_types:
            out.extend(self._adapt_artist(a) for a in (result.get("artist") or []))
        if "MusicAlbum" in item_types or not item_types:
            out.extend(self._adapt_album(a) for a in (result.get("album") or []))
        if "Audio" in item_types or not item_types:
            out.extend(self._adapt_song(s) for s in (result.get("song") or []))
        return out

    def search_all(
        self, term: str, songs: int = 12, albums: int = 14, artists: int = 14
    ) -> Dict[str, List[Dict[str, Any]]]:
        # search3 natively returns all three buckets, so a multi-type
        # query is one HTTP round-trip. The naive search()-per-type
        # path used to do three.
        params = {
            "query": term,
            "songCount": songs,
            "albumCount": albums,
            "artistCount": artists,
        }
        try:
            resp = self._request("search3", params)
        except Exception:
            return {"Audio": [], "MusicAlbum": [], "MusicArtist": []}
        result = resp.get("searchResult3") or {}
        return {
            "Audio": [self._adapt_song(s) for s in (result.get("song") or [])],
            "MusicAlbum": [self._adapt_album(a) for a in (result.get("album") or [])],
            "MusicArtist": [self._adapt_artist(a) for a in (result.get("artist") or [])],
        }

    def get_random_audio_items(self, parent_id: str, limit: int = 500) -> List[Dict[str, Any]]:
        params: Dict[str, Any] = {"size": min(limit, 500)}
        if parent_id:
            params["musicFolderId"] = parent_id
        try:
            resp = self._request("getRandomSongs", params)
        except Exception:
            return []
        songs = (resp.get("randomSongs") or {}).get("song") or []
        return [self._adapt_song(s) for s in songs]

    # ── Seeded radio ──────────────────────────────────────────────────

    def get_similar_songs(self, item_id: str, count: int = 50) -> List[Dict[str, Any]]:
        """``getSimilarSongs2`` — the v2 variant returns ID3-organized
        results keyed by Subsonic song IDs, where v1 returns deprecated
        artist IDs. Accepts song / album / artist IDs interchangeably;
        the server resolves the seed kind."""
        if not item_id:
            return []
        try:
            resp = self._request(
                "getSimilarSongs2",
                {
                    "id": item_id,
                    "count": count,
                },
            )
        except Exception:
            return []
        songs = (resp.get("similarSongs2") or {}).get("song") or []
        return [self._adapt_song(s) for s in songs]

    def get_instant_mix(self, item_id: str, count: int = 50) -> List[Dict[str, Any]]:
        """Subsonic has no native instant-mix endpoint; the closest
        match is ``getSimilarSongs2``, which is exactly what callers of
        ``get_similar_songs`` get. We alias here so the queue manager
        can semantically request a 'mix' on either provider without
        branching on ``provider.kind``."""
        return self.get_similar_songs(item_id, count=count)

    def get_genre_radio(
        self, genre_name: str, count: int = 50, offset: int = 0
    ) -> List[Dict[str, Any]]:
        """``getSongsByGenre`` — Subsonic has no dedicated genre-radio
        endpoint, so this returns a randomly-sampled batch of tracks
        within the named genre. Order is server-defined; treat it as
        a bag of seeds rather than a curated sequence. ``offset``
        forwards to the endpoint's ``offset`` param so callers can
        page past already-played tracks."""
        if not genre_name:
            return []
        params: Dict[str, Any] = {
            "genre": genre_name,
            "count": count,
        }
        if offset > 0:
            params["offset"] = offset
        try:
            resp = self._request("getSongsByGenre", params)
        except Exception:
            return []
        songs = (resp.get("songsByGenre") or {}).get("song") or []
        return [self._adapt_song(s) for s in songs]

    # ── Stream URLs ────────────────────────────────────────────────────

    def get_audio_stream_url(self, item_id: str, quality: Optional[str] = None) -> str:
        """Stream URL honoring the user's `audio_quality` setting (or
        the ``quality`` override the download path passes).
        ``"original"`` (default) → format=raw, bit-perfect, server
        skips ffmpeg. Anything else (a string of kbps like ``"320"``)
        → format=mp3 + maxBitRate, server transcodes on demand for
        bandwidth-constrained playback. format=raw with a non-zero
        maxBitRate forces a transcode on older Navidrome builds, so
        we only set maxBitRate when not in raw mode."""
        if not item_id:
            return ""
        from modules.settings import get_settings

        quality = (quality or get_settings().audio_quality or "original").strip().lower()
        if quality == "original":
            return self._build_url("stream", {"id": item_id, "format": "raw"})
        try:
            kbps = max(32, int(quality))
        except ValueError:
            kbps = 320
        return self._build_url(
            "stream",
            {
                "id": item_id,
                "format": "mp3",
                "maxBitRate": str(kbps),
            },
        )

    def get_video_stream_url(self, item_id: str) -> str:
        """Subsonic / Navidrome are music-only; no video. Returning
        empty string is safe because the queue manager only routes
        here when np.is_audio is False, which never happens for a
        music library."""
        return ""

    def get_audio_transcode_url(
        self, item_id: str, max_bitrate_kbps: int = 320, codec: str = "mp3"
    ) -> str:
        """Subsonic's stream endpoint takes ``format`` + ``maxBitRate``;
        the server transcodes on demand. We deliberately don't pass
        ``format=raw`` here (unlike get_audio_stream_url) — the cast
        path is precisely the case where we *want* a transcode."""
        if not item_id:
            return ""
        return self._build_url(
            "stream",
            {
                "id": item_id,
                "format": codec,
                "maxBitRate": str(max_bitrate_kbps),
            },
        )

    def get_image_url(
        self, item_id: str, image_type: str = "Primary", width: int = 400, fill: bool = False
    ) -> str:
        if not item_id:
            return ""
        # Subsonic getCoverArt takes the cover-art id, which for a
        # Subsonic-adapted item is what we stored at ImageTags.Primary
        # (often the album/artist id itself). Caller passes `item_id`
        # which works for albums/artists; for songs the AlbumPrimaryImageTag
        # / AlbumId is preferred but the song id also resolves on
        # Navidrome.
        return self._build_url(
            "getCoverArt",
            {
                "id": item_id,
                "size": width,
            },
        )

    def keep_alive_url(self) -> str:
        """Cheap GET URL for periodic heartbeats — keeps QNAM's TCP
        connection (and TLS session) warm so the next real request
        doesn't pay a fresh handshake. Subsonic's /ping is the
        canonical no-op endpoint and returns ~50 bytes."""
        if not self.is_authenticated:
            return ""
        return self._build_url("ping", {})

    # ── Playback reporting ─────────────────────────────────────────────

    def report_playback_start(
        self,
        item_id: str,
        position_ticks: int = 0,
        play_session_id: str = "",
        play_method: str = "DirectStream",
        media_source_id: str = "",
    ) -> None:
        """Subsonic's "now playing" stamp. submission=false signals
        playback started; the server uses this for the 'now playing'
        row in admin tools and other clients listening for activity."""
        if not item_id:
            return
        try:
            self._request(
                "scrobble",
                {
                    "id": item_id,
                    "submission": "false",
                },
            )
        except Exception:
            pass

    def report_playback_progress(
        self,
        item_id: str,
        position_ticks: int,
        is_paused: bool = False,
        play_session_id: str = "",
        play_method: str = "DirectStream",
        media_source_id: str = "",
        event_name: str = "",
    ) -> None:
        """Subsonic has no progress endpoint — server tracks playback
        state from the stream URL itself. No-op."""
        return

    def report_playback_stopped(
        self,
        item_id: str,
        position_ticks: int,
        play_session_id: str = "",
        play_method: str = "DirectStream",
        media_source_id: str = "",
    ) -> None:
        """submission=true increments play count + adds to history.
        The de-facto Last.fm rule is to send it once after >50% play
        or >4 minutes; the player_backend flow calls this on natural
        end (force_finished=True), which matches that intent."""
        if not item_id:
            return
        try:
            self._request(
                "scrobble",
                {
                    "id": item_id,
                    "submission": "true",
                },
            )
        except Exception:
            pass

    def mark_played(self, item_id: str) -> None:
        """Subsonic auto-marks played from the scrobble report; no
        separate "mark played" endpoint exists. No-op."""
        return

    def mark_unplayed(self, item_id: str) -> None:
        return

    def toggle_favorite(self, item_id: str, favorite: bool) -> None:
        if not item_id:
            return
        op = "star" if favorite else "unstar"
        try:
            self._request(op, {"id": item_id})
        except Exception:
            pass

    def get_lyrics(self, item_id: str) -> Optional[Dict[str, Any]]:
        """Fetch lyrics via OpenSubsonic's getLyricsBySongId, projected
        into Jellyfin's get_lyrics shape so the lyrics rail in
        NowPlayingPage renders with no consumer-side branching.

        Subsonic delivers structured lyrics under
        ``lyricsList.structuredLyrics[i].line[j]`` with each line
        carrying ``start`` (milliseconds) and ``value`` (text). We map:
          line.value  -> "Text"
          line.start  -> "Start" in 100-ns ticks (1ms = 10_000 ticks)
        which is exactly the Jellyfin /Audio/{id}/Lyrics shape — the
        rail's "synced if any Start > 0" heuristic Just Works.

        OpenSubsonic-only path; vanilla Subsonic's older getLyrics
        (artist+title plain-text) is not supported here. Navidrome,
        the primary Subsonic target, implements getLyricsBySongId.
        """
        if not item_id:
            return None
        try:
            resp = self._request("getLyricsBySongId", {"id": item_id})
        except Exception:
            return None
        container = resp.get("lyricsList") or {}
        structured = container.get("structuredLyrics") or []
        if not structured:
            return None
        # Multiple language entries possible — take the first. If
        # multilingual selection ever matters, expose a knob then.
        raw_lines = structured[0].get("line") or []
        if not raw_lines:
            return None
        lines: List[Dict[str, Any]] = []
        for ln in raw_lines:
            text = (ln.get("value") or "").strip()
            start_ms = int(ln.get("start") or 0)
            lines.append({"Text": text, "Start": start_ms * 10_000})
        return {"Lyrics": lines}

    # ── Internet radio ────────────────────────────────────────────────
    #
    # Subsonic 1.16.1 + OpenSubsonic CRUD. Read is anonymous; create /
    # update / delete are admin-only — non-admin users hit a Subsonic
    # error code 50 (generic auth failure) from Navidrome on the write
    # endpoints, which surfaces as a SubsonicError to the caller.

    def get_internet_radio_stations(self) -> List[Dict[str, Any]]:
        """Subsonic ``getInternetRadioStations.view``. Returns the raw
        per-station dicts (id / name / streamUrl / homePageUrl, plus
        OpenSubsonic's optional ``coverArt``). Empty list when the
        server has no stations defined or when the request itself
        fails — the UI's "no stations" state is the same in both cases."""
        try:
            resp = self._request("getInternetRadioStations")
        except Exception:
            return []
        container = resp.get("internetRadioStations") or {}
        stations = container.get("internetRadioStation") or []
        return list(stations)

    def create_internet_radio_station(
        self, name: str, stream_url: str, home_page_url: Optional[str] = None
    ) -> Dict[str, Any]:
        """Subsonic ``createInternetRadioStation.view``. Admin-only on
        Navidrome. The endpoint returns an empty ``subsonic-response``
        on success — to keep parity with the dict-return contract we
        re-fetch the station list and return the matching entry by
        ``streamUrl`` (the only stable identifier the caller knew
        before the create, since the server mints the id)."""
        params: Dict[str, Any] = {"name": name, "streamUrl": stream_url}
        if home_page_url:
            params["homepageUrl"] = home_page_url
        self._request("createInternetRadioStation", params)
        # Find the freshly-created entry. Matching on streamUrl is the
        # least-bad heuristic; name collisions are legal in Subsonic.
        for s in self.get_internet_radio_stations():
            if s.get("streamUrl") == stream_url and s.get("name") == name:
                return s
        # Fallback: the create succeeded server-side but the read-back
        # missed (race condition or aggressive server cache). Hand back
        # what the caller knew so the UI can render optimistically.
        return {
            "id": "",
            "name": name,
            "streamUrl": stream_url,
            "homePageUrl": home_page_url or "",
        }

    def update_internet_radio_station(
        self, station_id: str, name: str, stream_url: str, home_page_url: Optional[str] = None
    ) -> Dict[str, Any]:
        """Subsonic ``updateInternetRadioStation.view``. Admin-only.
        Same return-shape rationale as create: re-fetch + match on id
        so the caller gets a server-authoritative dict back."""
        params: Dict[str, Any] = {
            "id": station_id,
            "name": name,
            "streamUrl": stream_url,
        }
        if home_page_url is not None:
            # Empty string is a valid "clear the homepage" signal; only
            # drop the param when the caller passes None to mean "leave
            # untouched". (Subsonic's spec doesn't distinguish the two
            # at the endpoint level; this is jellytoast convention.)
            params["homepageUrl"] = home_page_url
        self._request("updateInternetRadioStation", params)
        for s in self.get_internet_radio_stations():
            if str(s.get("id")) == str(station_id):
                return s
        return {
            "id": station_id,
            "name": name,
            "streamUrl": stream_url,
            "homePageUrl": home_page_url or "",
        }

    def delete_internet_radio_station(self, station_id: str) -> None:
        """Subsonic ``deleteInternetRadioStation.view``. Admin-only."""
        self._request("deleteInternetRadioStation", {"id": station_id})

    # ── Smart playlists ────────────────────────────────────────────────

    def query_items(self, rules: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Evaluate a smart-playlist rule set against Subsonic.

        Strategy: pick the most selective rule with a native server
        query, fire that, then run the full rule set through the
        Python refinement pass (``modules.providers.smart_rule_eval``)
        to apply everything else. If no rule maps to a server query
        (e.g. only ``play_count`` / ``rating`` / ``artist`` /
        ``album``), fall back to a broad ``getAlbumList2`` fetch of
        up to ``_BROAD_FETCH_CAP`` albums plus per-album ``getAlbum``
        calls, then refine in Python.

        Server-native legs::

            genre equals X         → getSongsByGenre?genre=X
            year equals X          → getAlbumList2?type=byYear fromYear=X
            year greater_than X    → getAlbumList2?type=byYear fromYear=X+1
            year less_than X       → getAlbumList2?type=byYear toYear=X-1
            year between [lo, hi]  → getAlbumList2?type=byYear fromYear=lo
            is_favorite equals T   → getStarred2 (only `equals True`;
                                     `equals False` has no native
                                     endpoint and refines client-side)

        Refined in Python (no server-side filter on Subsonic)::

            genre not_equals, contains
            artist equals / contains / starts_with / ends_with
            album  equals / contains / starts_with / ends_with
            play_count any op
            rating any op
            is_favorite equals False
            date_added any op          — adapted items carry the song's
                                         `created` timestamp as
                                         DateCreated; the rule refines
                                         client-side. Subsonic has no
                                         "added in last N days" filter.
            last_played any op         — Subsonic exposes no per-track
                                         last-played timestamp, so the
                                         rule can never match on this
                                         backend (documented in
                                         smart_rule_schema).
            multi-rule match=all (AND) — server leg picks one, refine
                                         enforces the rest
            multi-rule match=any (OR)  — broad fetch + Python OR

        ``limit`` truncates after sort. ``sort`` / ``sort_desc`` apply
        in the Python refine layer over the adapted items.
        """
        from modules.providers.smart_rule_schema import validate_rules

        errors = validate_rules(rules)
        if errors:
            raise ValueError("invalid smart-playlist rule set: " + "; ".join(errors))

        raw_rules = rules.get("rules") or []
        if not raw_rules:
            return []

        match = rules.get("match", "all")
        mappable = [r for r in raw_rules if _is_server_mappable(r)]

        if match == "any" and len(raw_rules) > 1:
            return self._evaluate_any(raw_rules, mappable, rules)
        return self._evaluate_all(raw_rules, mappable, rules)

    def _evaluate_all(
        self, raw_rules: List[Dict[str, Any]], mappable: List[Dict[str, Any]], rules: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        """AND semantics. Pick the most selective server-mappable rule
        (first wins — rule order is the user's selectivity hint), fetch
        the candidates, then refine on the remaining rules in Python."""
        from modules.providers.smart_rule_eval import refine_items

        if mappable:
            server_rule = mappable[0]
            candidates = self._query_single_native(server_rule)
            # Server query already enforced server_rule; refine the rest.
            remaining = [r for r in raw_rules if r is not server_rule]
        else:
            candidates = self._broad_fetch()
            remaining = raw_rules

        refine_rules = dict(rules)
        refine_rules["rules"] = remaining
        return refine_items(candidates, refine_rules)

    def _evaluate_any(
        self, raw_rules: List[Dict[str, Any]], mappable: List[Dict[str, Any]], rules: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        """OR semantics. Each mappable rule contributes its server-
        fetched matches; non-mappable rules drive a broad fetch and a
        per-rule Python filter. Deduped union, sorted + limited."""
        from modules.providers.smart_rule_eval import (
            matches_rule,
            sort_items,
        )

        seen: set = set()
        merged: List[Dict[str, Any]] = []

        def _accept(item: Dict[str, Any]) -> None:
            iid = item.get("Id")
            if iid and iid not in seen:
                seen.add(iid)
                merged.append(item)

        for rule in mappable:
            for item in self._query_single_native(rule):
                _accept(item)

        non_mappable = [r for r in raw_rules if r not in mappable]
        if non_mappable:
            broad = self._broad_fetch()
            for item in broad:
                if any(matches_rule(item, r) for r in non_mappable):
                    _accept(item)

        merged = sort_items(
            merged,
            rules.get("sort"),
            bool(rules.get("sort_desc", False)),
        )
        limit = rules.get("limit")
        if isinstance(limit, int) and not isinstance(limit, bool):
            if limit == 0:
                return []
            if limit > 0:
                merged = merged[:limit]
        return merged

    # Maximum number of albums to pull in the no-mappable-rule
    # fallback path. Subsonic's getAlbumList2 caps each request at 500
    # and we don't paginate further in v1 — the rule engine documents
    # this so power users with a 10 000+ album library know to keep
    # at least one server-mappable rule in the set.
    _BROAD_FETCH_CAP = 500

    def _query_single_native(self, rule: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Translate one server-mappable rule into a Subsonic call and
        return the adapted item list. Caller is responsible for
        applying the remaining refines via ``refine_items``."""
        field, op, value = rule["field"], rule["op"], rule["value"]

        if field == "genre" and op == "equals":
            params = {"genre": value, "count": 500, "offset": 0}
            try:
                resp = self._request("getSongsByGenre", params)
            except Exception:
                return []
            songs = (resp.get("songsByGenre") or {}).get("song") or []
            return [self._adapt_song(s) for s in songs]

        if field == "year" and op in (
            "equals",
            "greater_than",
            "less_than",
            "between",
        ):
            from_year, to_year = _year_bounds(op, value)
            params = {
                "type": "byYear",
                "size": 500,
                "fromYear": from_year,
                "toYear": to_year,
            }
            try:
                resp = self._request("getAlbumList2", params)
            except Exception:
                return []
            albums = (resp.get("albumList2") or {}).get("album") or []
            tracks: List[Dict[str, Any]] = []
            for album in albums:
                try:
                    tracks.extend(self.get_album_tracks(album.get("id", "")))
                except Exception:
                    continue
            return tracks

        if field == "is_favorite" and op == "equals" and value:
            # getStarred2 returns the user's starred songs directly.
            # Only `equals True` reaches here (see _is_server_mappable);
            # `equals False` is a broad-fetch + Python refine.
            try:
                resp = self._request("getStarred2", {})
            except Exception:
                return []
            songs = (resp.get("starred2") or {}).get("song") or []
            adapted: List[Dict[str, Any]] = []
            for s in songs:
                item = self._adapt_song(s)
                # Every song in the getStarred2 payload is a favorite by
                # construction; some servers omit the per-song `starred`
                # timestamp in this response, so force the flag on so a
                # downstream refine on is_favorite doesn't drop them.
                item.setdefault("UserData", {})["IsFavorite"] = True
                adapted.append(item)
            return adapted

        # The mappable check should have prevented us getting here.
        return []

    def _broad_fetch(self) -> List[Dict[str, Any]]:
        """Pull up to ``_BROAD_FETCH_CAP`` albums and expand to tracks.

        Used when no rule maps to a Subsonic endpoint. The cap keeps
        the request bounded to a single round-trip plus N per-album
        fetches; larger libraries lose tail matches. Documented in
        ``query_items``' docstring as a v1 limit.
        """
        params = {
            "type": "alphabeticalByArtist",
            "size": self._BROAD_FETCH_CAP,
            "offset": 0,
        }
        try:
            resp = self._request("getAlbumList2", params)
        except Exception:
            return []
        albums = (resp.get("albumList2") or {}).get("album") or []
        songs: List[Dict[str, Any]] = []
        for album in albums:
            try:
                songs.extend(self.get_album_tracks(album.get("id", "")))
            except Exception:
                continue
        return songs

    # ── Metadata editing ───────────────────────────────────────────────

    def update_track_metadata(self, item_id: str, edits: Dict[str, Any]) -> Dict[str, Any]:
        """Subsonic / OpenSubsonic / Navidrome have no server-side
        tag-edit endpoint (file is the source of truth) — see
        ``docs/research/tag_editing.md`` § 2 for the survey. The UI
        layer gates this behind ``can_edit_metadata`` so it should
        never actually be reached; we raise loudly so it's obvious if
        a caller forgot the gate."""
        raise NotImplementedError(
            "Subsonic / Navidrome do not expose a metadata-edit endpoint; "
            "tag editing is Jellyfin-only."
        )

    # ── Cache control ──────────────────────────────────────────────────

    def invalidate_meta_cache(self, item_id: str = "") -> None:
        # No in-memory cache yet on this provider. When we add one
        # (mirroring JellyfinAPI._meta_cache) this hooks in.
        return

    def close(self) -> None:
        """Close the requests.Session so its connection pool is released
        when this provider is discarded (sign-out / kind switch). The
        session is per-instance (built in __init__), so a fresh provider
        gets a fresh session — safe to close here."""
        try:
            session = getattr(self, "session", None)
            if session is not None:
                session.close()
        except Exception:
            pass
