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
backends to a JellyToast-internal schema and retire the adapter.
"""

import hashlib
import secrets
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

import requests

from modules.providers.base import MediaProvider, ServerInfo, AuthResult
from modules.settings import get_settings


CLIENT_NAME = "JellyToast"
PROTOCOL_VERSION = "1.16.1"


class SubsonicError(Exception):
    """Raised when a Subsonic JSON response carries status=failed.
    The .code attribute is the Subsonic error code from the spec
    (40 = wrong creds, 41 = LDAP token-auth disallowed, 50+ = various)."""

    def __init__(self, code: int, message: str):
        super().__init__(f"Subsonic error {code}: {message}")
        self.code = code
        self.message = message


def _build_query(params: dict) -> str:
    """URL-encode params, dropping any with empty/None values so the
    server doesn't see "id=" and choke."""
    clean = {k: v for k, v in params.items() if v is not None and v != ""}
    return urlencode(clean)


class SubsonicProvider(MediaProvider):
    """Subsonic / OpenSubsonic / Navidrome backend."""

    def __init__(self):
        self.settings = get_settings()
        self.session = requests.Session()
        self._server_url = self.settings.server_url.rstrip("/")
        # Subsonic doesn't have a separate user_id — username is the
        # identifier. We persist the username under user_id for
        # consistency with the Jellyfin code path so the LoginView /
        # Settings / Account page logic doesn't have to branch.
        self._username = self.settings.username or self.settings.user_id
        self._password = self.settings.access_token

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
        # Backfill from settings on the first read after construction
        # — KDE's Wayland secret service can race app launch, which
        # makes settings.access_token return "" at __init__ time even
        # when keyring has the token. Re-reading here means the first
        # is_authenticated call after the secret service warms up
        # rehydrates the cache. Gated so a confirmed-empty keyring
        # doesn't trigger the (potentially multi-second) retry on
        # every subsequent property read.
        if not self._password and not getattr(self, "_backfill_done", False):
            self._password = self.settings.access_token
            self._backfill_done = True
        # Second-chance wait — when we have a username + server_url
        # stored we *know* a token was previously persisted, so an
        # empty password here is the keyring race rather than a
        # genuinely-missing entry. Retry once with a much longer
        # budget before giving up. Gated by `_second_chance_done` so
        # subsequent is_authenticated reads don't re-block; if the
        # retry didn't surface a token we accept the empty-token
        # answer for the rest of the session.
        if (not self._password
                and self._username and self._server_url
                and not getattr(self, "_second_chance_done", False)):
            from modules.settings import _keyring_get_token
            v = _keyring_get_token(max_attempts=50, interval_s=0.15)
            if v:
                self._password = v
            self._second_chance_done = True
        ok = bool(self._username and self._password and self._server_url)
        if not getattr(self, "_boot_auth_logged", False):
            print(
                f"[boot-auth] subsonic url={'set' if self._server_url else 'empty'} "
                f"user={'set' if self._username else 'empty'} "
                f"token_len={len(self._password)} is_auth={ok}",
                flush=True,
            )
            self._boot_auth_logged = True
        return ok

    # ── Auth helpers ──────────────────────────────────────────────────

    def _auth_params(self) -> dict:
        """Per-request auth params for token+salt mode. A fresh salt
        is generated each call (Subsonic spec requires ≥6 random
        characters; we use 16 hex chars for headroom). Combined with
        ``c=JellyToast`` this also stamps every request as belonging
        to our client in Navidrome's logs / per-player profiles.

        Note: ``f`` (response format) is deliberately *not* included
        here — JSON formatting is REST-only. Including ``f=json`` on
        stream / getCoverArt URLs makes Navidrome and friends return
        a JSON error envelope instead of raw audio / image bytes, so
        mpv plays nothing and image decoders see invalid data. The
        ``_request`` helper adds ``f=json`` itself; ``_build_url``
        does not."""
        salt = secrets.token_hex(8)
        token = hashlib.md5(
            (self._password + salt).encode("utf-8")
        ).hexdigest()
        return {
            "u": self._username,
            "t": token,
            "s": salt,
            "v": PROTOCOL_VERSION,
            "c": CLIENT_NAME,
        }

    def _build_url(self, path: str, params: Optional[dict] = None,
                   server_url: Optional[str] = None) -> str:
        """Construct a fully-authed Subsonic URL for binary endpoints
        (stream, getCoverArt). No ``f=json`` — the server returns raw
        bytes for these and including the format param can flip it
        into a JSON error envelope instead."""
        full = dict(self._auth_params())
        if params:
            full.update(params)
        url = (server_url or self._server_url).rstrip("/")
        return f"{url}/rest/{path}?{_build_query(full)}"

    def _request(self, path: str, params: Optional[dict] = None,
                 server_url: Optional[str] = None) -> dict:
        """GET a Subsonic JSON endpoint, returning the inner
        subsonic-response dict. Raises ``SubsonicError`` on a failed
        status, ``requests`` exceptions on network errors."""
        full = dict(self._auth_params())
        full["f"] = "json"
        if params:
            full.update(params)
        base = (server_url or self._server_url).rstrip("/")
        url = f"{base}/rest/{path}?{_build_query(full)}"
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

    # ── Auth tier ─────────────────────────────────────────────────────

    def probe(self, server_url: str) -> Optional[ServerInfo]:
        """Pre-auth ping. Subsonic's /rest/ping responds even without
        credentials (it returns a failed status with code 10 "missing
        required parameter username"), and the response carries the
        server type + version regardless. We use that to confirm a
        Subsonic-compatible server lives at the URL."""
        try:
            qs = _build_query({
                "v": PROTOCOL_VERSION,
                "c": CLIENT_NAME,
                "f": "json",
            })
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

    def authenticate(self, server_url: str, username: str,
                     password: str) -> AuthResult:
        """Sign in by sending a credentialed ping. Subsonic has no
        separate login endpoint — every request is independently
        authenticated. We use ping as a credential test."""
        self._server_url = server_url.rstrip("/")
        self._username = username
        self._password = password
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
                # Tag the password so _auth_params switches modes.
                # (Future: add an explicit auth_mode field.)
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
        if not self.is_authenticated:
            return False
        try:
            self._request("ping")
            return True
        except SubsonicError as e:
            # 40 = wrong creds, 41 = LDAP token-auth issue. Treat as
            # definitive rejection so the boot path drops to LoginView.
            if e.code in (40, 41):
                return False
            return True
        except Exception:
            # Network error — treat as transient (matches Jellyfin
            # provider's tolerant behavior).
            return True

    def server_logout(self) -> bool:
        """No-op. Subsonic is stateless per request — there's no
        session to revoke server-side. Local credential clearing
        (handled by the host) is sufficient."""
        return True

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
                if s.get("artistId") else []
            ),
            "ProductionYear": s.get("year"),
            "PremiereDate": "",
            "ChildCount": s.get("songCount", 0),
            "Genres": [s.get("genre")] if s.get("genre") else [],
            "ImageTags": (
                {"Primary": s.get("coverArt")}
                if s.get("coverArt") else {}
            ),
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
            "ImageTags": (
                {"Primary": s.get("coverArt")}
                if s.get("coverArt") else {}
            ),
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
                if s.get("artistId") else []
            ),
            "IndexNumber": s.get("track"),
            "ParentIndexNumber": s.get("discNumber") or 1,
            "ProductionYear": s.get("year"),
            "RunTimeTicks": duration_ticks,
            "Genres": [s.get("genre")] if s.get("genre") else [],
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

    def get_items(self, parent_id: str = "", item_type: str = "",
                  limit: int = 100, start_index: int = 0,
                  sort_by: str = "SortName",
                  sort_order: str = "Ascending",
                  recursive: bool = False, genre_ids: str = "",
                  filters: str = "", years: str = "") -> Dict[str, Any]:
        """Multi-purpose browse — switches on item_type. Maps to
        Subsonic's getAlbumList2 / search3 / getStarred2 etc."""
        if item_type == "MusicAlbum":
            return self._get_albums(
                parent_id=parent_id, limit=limit, start_index=start_index,
                sort_by=sort_by, sort_order=sort_order,
                genre_id=genre_ids, filters=filters, year=years,
            )
        if item_type == "MusicArtist":
            # Subsonic's getArtists returns an indexed list (by
            # alphabet bucket); flattened to a single list of
            # adapted artist dicts.
            artists = self.get_artists(
                limit=limit or 200, start_index=start_index,
            )
            return {"Items": artists, "TotalRecordCount": len(artists)}
        if item_type == "Audio":
            return self._get_songs(
                parent_id=parent_id, limit=limit, start_index=start_index,
                genre_id=genre_ids,
            )
        if item_type == "Playlist":
            return self._get_playlists()
        return {"Items": [], "TotalRecordCount": 0}

    def _get_albums(self, parent_id: str, limit: int, start_index: int,
                    sort_by: str, sort_order: str, genre_id: str,
                    filters: str, year: str = "") -> Dict[str, Any]:
        # Map Jellyfin SortBy keys to Subsonic getAlbumList2 types.
        # Subsonic's set is fixed; we pick the closest equivalent
        # and rely on client-side re-sort (already in library_grid)
        # for compound sorts that don't map.
        first_key = (sort_by or "SortName").split(",", 1)[0]
        kind = "alphabeticalByName"
        if first_key == "AlbumArtist":
            kind = "alphabeticalByArtist"
        elif first_key == "PremiereDate" or first_key == "ProductionYear":
            kind = "byYear"
        elif first_key == "DateCreated":
            kind = "newest"
        elif first_key == "DatePlayed":
            kind = "recent"
        elif filters == "IsPlayed":
            kind = "frequent"  # closest to "played"
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
            # JellyToast sort-direction toggle.
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
        return {"Items": items, "TotalRecordCount": len(items)}

    def _get_songs(self, parent_id: str, limit: int, start_index: int,
                   genre_id: str) -> Dict[str, Any]:
        if genre_id:
            params = {"genre": genre_id, "count": min(limit, 500),
                      "offset": start_index}
            if parent_id:
                params["musicFolderId"] = parent_id
            try:
                resp = self._request("getSongsByGenre", params)
            except Exception:
                return {"Items": [], "TotalRecordCount": 0}
            songs = (resp.get("songsByGenre") or {}).get("song") or []
        else:
            params = {"size": min(limit, 500)}
            if parent_id:
                params["musicFolderId"] = parent_id
            try:
                resp = self._request("getRandomSongs", params)
            except Exception:
                return {"Items": [], "TotalRecordCount": 0}
            songs = (resp.get("randomSongs") or {}).get("song") or []
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
                "ImageTags": (
                    {"Primary": p.get("coverArt")}
                    if p.get("coverArt") else {}
                ),
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

    def get_artists(self, limit: int = 200,
                    start_index: int = 0) -> List[Dict[str, Any]]:
        try:
            resp = self._request("getArtists")
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

    def get_resume_items(self, limit: int = 12,
                         media_type: str = "") -> List[Dict[str, Any]]:
        """No real Subsonic equivalent — closest is "recently played"
        which we surface elsewhere. Return empty so the rail
        self-hides on the suggestions surface."""
        return []

    def get_latest_media(self, library_id: str = "",
                         limit: int = 16) -> List[Dict[str, Any]]:
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

    def search(self, term: str, limit: int = 50,
               item_types: str = "") -> List[Dict[str, Any]]:
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

    def get_random_audio_items(self, parent_id: str,
                               limit: int = 500) -> List[Dict[str, Any]]:
        params = {"size": min(limit, 500)}
        if parent_id:
            params["musicFolderId"] = parent_id
        try:
            resp = self._request("getRandomSongs", params)
        except Exception:
            return []
        songs = (resp.get("randomSongs") or {}).get("song") or []
        return [self._adapt_song(s) for s in songs]

    # ── Stream URLs ────────────────────────────────────────────────────

    def get_audio_stream_url(self, item_id: str) -> str:
        """Bit-perfect stream — format=raw tells Navidrome to skip
        ffmpeg entirely. We deliberately omit maxBitRate; sending it
        non-zero forces a transcode even with format=raw on older
        Navidrome builds."""
        if not item_id:
            return ""
        return self._build_url("stream", {"id": item_id, "format": "raw"})

    def get_video_stream_url(self, item_id: str) -> str:
        """Subsonic / Navidrome are music-only; no video. Returning
        empty string is safe because the queue manager only routes
        here when np.is_audio is False, which never happens for a
        music library."""
        return ""

    def get_audio_transcode_url(self, item_id: str,
                                 max_bitrate_kbps: int = 320,
                                 codec: str = "mp3") -> str:
        """Subsonic's stream endpoint takes ``format`` + ``maxBitRate``;
        the server transcodes on demand. We deliberately don't pass
        ``format=raw`` here (unlike get_audio_stream_url) — the cast
        path is precisely the case where we *want* a transcode."""
        if not item_id:
            return ""
        return self._build_url("stream", {
            "id": item_id,
            "format": codec,
            "maxBitRate": str(max_bitrate_kbps),
        })

    def get_image_url(self, item_id: str, image_type: str = "Primary",
                      width: int = 400, fill: bool = False) -> str:
        if not item_id:
            return ""
        # Subsonic getCoverArt takes the cover-art id, which for a
        # Subsonic-adapted item is what we stored at ImageTags.Primary
        # (often the album/artist id itself). Caller passes `item_id`
        # which works for albums/artists; for songs the AlbumPrimaryImageTag
        # / AlbumId is preferred but the song id also resolves on
        # Navidrome.
        return self._build_url("getCoverArt", {
            "id": item_id, "size": width,
        })

    # ── Playback reporting ─────────────────────────────────────────────

    def report_playback_start(self, item_id: str, position_ticks: int = 0,
                              play_session_id: str = "",
                              play_method: str = "DirectStream",
                              media_source_id: str = "") -> None:
        """Subsonic's "now playing" stamp. submission=false signals
        playback started; the server uses this for the 'now playing'
        row in admin tools and other clients listening for activity."""
        if not item_id:
            return
        try:
            self._request("scrobble", {
                "id": item_id, "submission": "false",
            })
        except Exception:
            pass

    def report_playback_progress(self, item_id: str, position_ticks: int,
                                  is_paused: bool = False,
                                  play_session_id: str = "",
                                  play_method: str = "DirectStream",
                                  media_source_id: str = "",
                                  event_name: str = "") -> None:
        """Subsonic has no progress endpoint — server tracks playback
        state from the stream URL itself. No-op."""
        return

    def report_playback_stopped(self, item_id: str, position_ticks: int,
                                play_session_id: str = "",
                                play_method: str = "DirectStream",
                                media_source_id: str = "") -> None:
        """submission=true increments play count + adds to history.
        The de-facto Last.fm rule is to send it once after >50% play
        or >4 minutes; the player_backend flow calls this on natural
        end (force_finished=True), which matches that intent."""
        if not item_id:
            return
        try:
            self._request("scrobble", {
                "id": item_id, "submission": "true",
            })
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

    # ── Cache control ──────────────────────────────────────────────────

    def invalidate_meta_cache(self, item_id: str = "") -> None:
        # No in-memory cache yet on this provider. When we add one
        # (mirroring JellyfinAPI._meta_cache) this hooks in.
        return
