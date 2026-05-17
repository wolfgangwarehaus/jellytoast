"""
Jellyfin REST API client.
Supports library browsing, music navigation (artists/albums/tracks),
direct audio streams (bit-perfect), HLS video, lyrics, and playback reporting.
"""

import copy
import requests
from collections import OrderedDict
from typing import Optional, List, Dict, Any, Tuple
from modules.settings import get_settings


CLIENT_NAME = "jellytoast"
CLIENT_VERSION = "1.0.0"
DEVICE_NAME = "jellytoast Desktop"


class JellyfinAPI:
    # Bound LRU cache for stable metadata GETs. Keyed by (op, item_id);
    # invalidated on logout / re-authenticate so cross-account queries
    # never return stale data. 512 entries covers typical browse depth
    # of artists × albums × tracks without unbounded growth.
    _META_CACHE_MAX = 512

    def __init__(self):
        self.settings = get_settings()
        self.session = requests.Session()
        self.server_url = self.settings.server_url.rstrip("/")
        self.user_id = self.settings.user_id
        self.token = self.settings.access_token
        self._meta_cache: "OrderedDict[Tuple[str, str], Any]" = OrderedDict()

    @property
    def device_id(self) -> str:
        return self.settings.device_id

    @property
    def is_authenticated(self) -> bool:
        # Backfill from settings if the cached token is empty — KDE's
        # Wayland secret service can race app launch, which makes
        # settings.access_token return "" at __init__ time even when
        # keyring has the token. Re-reading here lets the first
        # is_authenticated call after the secret service warms up
        # rehydrate the cache instead of stranding the session at
        # the login view for the rest of the run.
        if not self.token and not getattr(self, "_backfill_done", False):
            self.token = self.settings.access_token
            self._backfill_done = True
        ok = bool(self.token and self.user_id and self.server_url)
        if not getattr(self, "_boot_auth_logged", False):
            print(
                f"[boot-auth] jellyfin url={'set' if self.server_url else 'empty'} "
                f"user={'set' if self.user_id else 'empty'} "
                f"token_len={len(self.token)} is_auth={ok}",
                flush=True,
            )
            self._boot_auth_logged = True
        return ok

    @property
    def auth_header(self) -> str:
        h = (
            f'MediaBrowser Client="{CLIENT_NAME}", Device="{DEVICE_NAME}", '
            f'DeviceId="{self.device_id}", Version="{CLIENT_VERSION}"'
        )
        if self.token:
            h += f', Token="{self.token}"'
        return h

    def _headers(self) -> Dict[str, str]:
        return {"X-Emby-Authorization": self.auth_header, "Content-Type": "application/json"}

    # ── Auth ────────────────────────────────────────────────────────────────

    def authenticate(self, server_url: str, username: str, password: str) -> Dict:
        self.server_url = server_url.rstrip("/")
        url = f"{self.server_url}/Users/AuthenticateByName"
        resp = self.session.post(
            url, json={"Username": username, "Pw": password}, headers=self._headers(), timeout=10
        )
        resp.raise_for_status()
        data = resp.json()
        self.token = data["AccessToken"]
        self.user_id = data["User"]["Id"]
        # Persist
        self.settings.server_url = self.server_url
        self.settings.username = username
        self.settings.access_token = self.token
        self.settings.user_id = self.user_id
        # Force-flush — tray Quit on KDE skips the QSettings destructor
        # path, so QSettings-only fields (username, user_id) can be lost
        # if we don't sync immediately. The token survives via keyring.
        self.settings.flush()
        return data

    def verify_session(self) -> bool:
        """Test current credentials against server. Returns True if
        the server confirms the session, False ONLY when the server
        explicitly rejects the token (401 / 403). Network errors,
        timeouts, and unreachable hosts are treated as "can't tell"
        and return True — we don't want a brief network hiccup at
        boot to drop the user back to the LoginView when their
        credentials are perfectly valid.

        The trade-off: if a token is genuinely revoked AND the user
        is offline, we'd let them try to use the app with the bad
        token. Their first REST call will surface the 401 and they
        can re-auth then. Better than a false-positive logout that
        forces re-login on every flaky-network boot."""
        if not self.is_authenticated:
            return False
        try:
            r = self.session.get(
                f"{self.server_url}/Users/{self.user_id}",
                headers=self._headers(),
                timeout=8,
            )
        except Exception:
            # Network error — couldn't even reach the server. Assume
            # creds are still good; let the next API call surface a
            # real auth problem if there is one.
            return True
        # Definitive rejection: token isn't accepted.
        if r.status_code in (401, 403):
            return False
        # 200 = good. Anything else (5xx, etc.) is a transient server
        # condition — keep creds.
        return True

    def server_info(self, server_url: str = "") -> Dict:
        """Pre-auth probe of /System/Info/Public. No auth header
        needed. Used by the LoginView to validate the URL is actually
        a Jellyfin server before the password is sent over the wire,
        and to capture the ServerId for future multi-server support.
        Pass an explicit `server_url` when probing a URL that hasn't
        been committed to settings yet."""
        url = (server_url or self.server_url).rstrip("/")
        if not url:
            raise ValueError("Server URL is empty")
        r = self.session.get(f"{url}/System/Info/Public", timeout=5)
        r.raise_for_status()
        return r.json() if r.content else {}

    def server_logout(self) -> bool:
        """POST /Sessions/Logout — server revokes this device's token
        and removes the row from the admin Devices dashboard. Best-
        effort: a network failure here doesn't block local sign-out
        but is logged. Must be called before clearing self.token,
        since the call needs the token in the auth header."""
        if not self.is_authenticated:
            return False
        try:
            self.session.post(
                f"{self.server_url}/Sessions/Logout",
                headers=self._headers(),
                timeout=5,
            )
            return True
        except Exception as e:
            print(f"[jellytoast] /Sessions/Logout failed: {e}", flush=True)
            return False

    def logout(self):
        self.token = ""
        self.user_id = ""
        self.settings.access_token = ""
        self.settings.user_id = ""
        self._meta_cache.clear()

    def invalidate_meta_cache(self, item_id: str = ""):
        """Drop a single item's cached metadata, or the whole cache when
        no item_id is given. Call after server-side mutations (favorite
        toggle, edits) that would make the cached snapshot stale."""
        if not item_id:
            self._meta_cache.clear()
            return
        for key in [k for k in self._meta_cache if k[1] == item_id]:
            del self._meta_cache[key]

    def _cached(self, op: str, item_id: str, fetch):
        """Return a deep copy of the cached value or fetch + cache + copy.
        Deep-copy on read is required because callers mutate the dicts
        we return (e.g. `_expand_context` injects AlbumId into every
        track), and a shared reference would let those mutations leak
        across calls."""
        key = (op, item_id)
        cached = self._meta_cache.get(key)
        if cached is not None:
            self._meta_cache.move_to_end(key)
            return copy.deepcopy(cached)
        value = fetch()
        self._meta_cache[key] = value
        self._meta_cache.move_to_end(key)
        while len(self._meta_cache) > self._META_CACHE_MAX:
            self._meta_cache.popitem(last=False)
        return copy.deepcopy(value)

    # ── Generic queries ─────────────────────────────────────────────────────

    def _get(self, path: str, params: Optional[Dict] = None) -> Dict:
        """GET wrapper that feeds the connectivity tracker. HTTPError
        4xx / 5xx still counts as "server reachable" — only a
        ``RequestException`` (timeout, DNS fail, connection refused)
        signals the server itself is gone."""
        url = f"{self.server_url}{path}"
        import requests
        from modules import offline as _offline

        try:
            r = self.session.get(
                url,
                headers=self._headers(),
                params=params or {},
                timeout=15,
            )
        except requests.exceptions.RequestException:
            _offline.note_request_failure()
            raise
        _offline.note_request_success()
        if r.status_code in (401, 403):
            # Definitive auth-reject — feed the auth-failure tracker so
            # the UI can drop to LoginView after a string of these
            # instead of silently surfacing 401-driven empty states.
            _offline.note_auth_failure()
        elif 200 <= r.status_code < 300:
            _offline.note_auth_success()
        r.raise_for_status()
        return r.json() if r.content else {}

    def _post(self, path: str, payload: Optional[Dict] = None) -> Optional[Dict]:
        """POST wrapper. Same reachability semantics as _get — a
        bare-except path silently dropping the failure used to mask
        network outages; we now classify the exception so timeouts /
        connection errors feed the offline tracker while still
        preserving the no-throw contract callers depend on."""
        url = f"{self.server_url}{path}"
        import requests
        from modules import offline as _offline

        try:
            r = self.session.post(
                url,
                headers=self._headers(),
                json=payload or {},
                timeout=10,
            )
        except requests.exceptions.RequestException:
            _offline.note_request_failure()
            return None
        except Exception:
            # Any non-network failure (JSON encode, etc.) leaves the
            # tracker alone — connectivity isn't the right signal here.
            return None
        _offline.note_request_success()
        if r.status_code in (401, 403):
            _offline.note_auth_failure()
        elif 200 <= r.status_code < 300:
            _offline.note_auth_success()
        try:
            return r.json() if r.content else None
        except ValueError:
            return None

    # ── Libraries ───────────────────────────────────────────────────────────

    def get_libraries(self) -> List[Dict]:
        return self._get(f"/Users/{self.user_id}/Views").get("Items", [])

    def get_resume_items(self, limit: int = 12, media_type: str = "") -> List[Dict]:
        params = {"Limit": limit, "Fields": "PrimaryImageAspectRatio,BasicSyncInfo"}
        if media_type:
            params["MediaTypes"] = media_type
        return self._get(f"/Users/{self.user_id}/Items/Resume", params).get("Items", [])

    def get_latest_media(self, library_id: str = "", limit: int = 16) -> List[Dict]:
        params = {"Limit": limit, "Fields": "PrimaryImageAspectRatio,BasicSyncInfo"}
        if library_id:
            params["ParentId"] = library_id
        return self._get(f"/Users/{self.user_id}/Items/Latest", params)

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
    ) -> Dict:
        params = {
            "Limit": limit,
            "StartIndex": start_index,
            "Fields": "PrimaryImageAspectRatio,BasicSyncInfo,ProductionYear,RunTimeTicks",
            "SortBy": sort_by,
            "SortOrder": sort_order,
            "Recursive": recursive,
        }
        if parent_id:
            params["ParentId"] = parent_id
        if item_type:
            params["IncludeItemTypes"] = item_type
        if genre_ids:
            # Comma-separated for multiple, but the typical caller
            # passes a single genre Id from a tile click.
            params["GenreIds"] = genre_ids
        if filters:
            # Comma-separated Jellyfin filter names — IsPlayed,
            # IsFavorite, IsUnplayed, etc. Used by the Suggestions view
            # to scope the recently/frequently-played rails to items
            # that actually have a play history.
            params["Filters"] = filters
        if years:
            # Comma-separated production years; tile-click year filter
            # passes a single year string ("2013").
            params["Years"] = years
        return self._get(f"/Users/{self.user_id}/Items", params)

    def search(self, term: str, limit: int = 50, item_types: str = "") -> List[Dict]:
        # `item_types` is the comma-separated IncludeItemTypes; default
        # casts a wide net across all media kinds. Native Search calls
        # this once per kind ("Audio" / "MusicAlbum" / "MusicArtist") so
        # each per-section result list has a deterministic cap.
        params = {
            "SearchTerm": term,
            "UserId": self.user_id,
            "Recursive": True,
            "Limit": limit,
            "IncludeItemTypes": item_types or "Movie,Series,Episode,Audio,MusicAlbum,MusicArtist",
            "Fields": "PrimaryImageAspectRatio,ProductionYear,AlbumArtist",
        }
        return self._get("/Items", params).get("Items", [])

    # ── Music ───────────────────────────────────────────────────────────────

    def get_artists(self, limit: int = 200, start_index: int = 0) -> List[Dict]:
        params = {
            "UserId": self.user_id,
            "Limit": limit,
            "StartIndex": start_index,
            "SortBy": "SortName",
            "SortOrder": "Ascending",
            "Fields": "PrimaryImageAspectRatio",
        }
        return self._get("/Artists/AlbumArtists", params).get("Items", [])

    def get_artist_albums(self, artist_id: str) -> List[Dict]:
        def _fetch():
            params = {
                "AlbumArtistIds": artist_id,
                "UserId": self.user_id,
                "IncludeItemTypes": "MusicAlbum",
                "Recursive": True,
                "SortBy": "PremiereDate,SortName",
                "SortOrder": "Descending",
                "Fields": "PrimaryImageAspectRatio,ProductionYear,ChildCount",
            }
            return self._get(f"/Users/{self.user_id}/Items", params).get("Items", [])

        return self._cached("artist_albums", artist_id, _fetch)

    def get_album_tracks(self, album_id: str) -> List[Dict]:
        def _fetch():
            params = {
                "ParentId": album_id,
                "UserId": self.user_id,
                "SortBy": "ParentIndexNumber,IndexNumber,SortName",
                "Fields": "RunTimeTicks,Artists,AlbumArtist,IndexNumber,ParentIndexNumber",
            }
            return self._get(f"/Users/{self.user_id}/Items", params).get("Items", [])

        return self._cached("album_tracks", album_id, _fetch)

    def get_playlist_items(self, playlist_id: str) -> List[Dict]:
        # `Fields=AlbumId` is required so cover art for each track resolves
        # from its native album (playlist tracks span many albums, unlike
        # `get_album_tracks` where AlbumId is uniform and we inject it).
        params = {
            "UserId": self.user_id,
            "Fields": "RunTimeTicks,Artists,AlbumArtist,AlbumId,IndexNumber,ParentIndexNumber",
        }
        return self._get(f"/Playlists/{playlist_id}/Items", params).get("Items", [])

    def get_random_audio_items(self, parent_id: str, limit: int = 500) -> List[Dict]:
        # Random-sorted Audio items under a library/folder — used to
        # synthesize a true library-wide shuffle when Jellyfin Web's
        # "Shuffle" button only shuffled within one album.
        params = {
            "UserId": self.user_id,
            "ParentId": parent_id,
            "IncludeItemTypes": "Audio",
            "Recursive": True,
            "SortBy": "Random",
            "Limit": limit,
            "Fields": "RunTimeTicks,Artists,AlbumArtist,AlbumId,IndexNumber,ParentIndexNumber",
        }
        return self._get(f"/Users/{self.user_id}/Items", params).get("Items", [])

    def get_genres(self) -> List[Dict]:
        params = {"UserId": self.user_id, "IncludeItemTypes": "Audio,MusicAlbum", "Recursive": True}
        return self._get("/MusicGenres", params).get("Items", [])

    def get_lyrics(self, item_id: str) -> Optional[Dict]:
        try:
            return self._get(f"/Audio/{item_id}/Lyrics")
        except Exception:
            return None

    # ── Item details ────────────────────────────────────────────────────────

    def get_item(self, item_id: str) -> Dict:
        return self._cached(
            "item",
            item_id,
            lambda: self._get(f"/Users/{self.user_id}/Items/{item_id}"),
        )

    # ── Stream URLs ─────────────────────────────────────────────────────────

    def get_audio_stream_url(
        self, item_id: str, container_priority: bool = True, quality: "str | None" = None
    ) -> str:
        """
        Direct audio stream — bit-perfect when format is supported.
        Uses /Audio/{id}/stream which honors static=true for direct play.

        Session-binding query params (UserId, DeviceId, MediaSourceId)
        let the server's stream tracker attribute the bytes to the
        same session our /Sessions/Playing reports use. Without them
        the server treats the stream as anonymous and play-count
        attribution drifts.

        ``quality`` overrides the user's audio_quality setting for this
        call — the download path passes ``download_quality``.
        """
        quality = quality or self.settings.audio_quality
        # MediaSourceId == ItemId for music items (single source per
        # audio file). Sending it explicitly is what the official
        # clients do and what /PlaybackInfo would echo back.
        common = (
            f"api_key={self.token}"
            f"&UserId={self.user_id}"
            f"&DeviceId={self.device_id}"
            f"&MediaSourceId={item_id}"
        )
        if quality == "original":
            return f"{self.server_url}/Audio/{item_id}/stream?{common}&static=true"
        try:
            bitrate = int(quality) * 1000
        except ValueError:
            bitrate = 320_000
        return (
            f"{self.server_url}/Audio/{item_id}/stream.mp3"
            f"?{common}&MaxStreamingBitrate={bitrate}&AudioCodec=mp3"
        )

    def get_video_stream_url(self, item_id: str) -> str:
        """Direct video stream for original-format playback."""
        return f"{self.server_url}/Videos/{item_id}/stream?static=true&api_key={self.token}"

    def get_image_url(
        self, item_id: str, image_type: str = "Primary", width: int = 400, fill: bool = False
    ) -> str:
        if not item_id:
            return ""
        path = "FillWidth" if fill else "width"
        return (
            f"{self.server_url}/Items/{item_id}/Images/{image_type}"
            f"?{path}={width}&quality=90&api_key={self.token}"
        )

    # ── Playback reporting ──────────────────────────────────────────────────

    # Jellyfin's PlaybackStartInfo / PlaybackProgressInfo / PlaybackStopInfo
    # schemas all expect:
    #   - ItemId (required)
    #   - MediaSourceId (multi-source items use this to disambiguate;
    #     for music it equals ItemId, but the field still has to be sent)
    #   - PlaySessionId — a client-generated GUID that ties Start →
    #     Progress → Stop together. Without it the server creates
    #     "ghost" rows in the admin Sessions view and can't dedupe
    #     reports across a single play.
    #   - PlayMethod — DirectPlay / DirectStream / Transcode. Misreporting
    #     this skews the server's transcoding statistics. The default
    #     here is DirectStream (server ships bytes, client decodes);
    #     callers pass `play_method="Transcode"` for the stream.mp3
    #     path when the user picked a non-original audio quality.

    def report_playback_start(
        self,
        item_id: str,
        position_ticks: int = 0,
        play_session_id: str = "",
        play_method: str = "DirectStream",
        media_source_id: str = "",
    ):
        self._post(
            "/Sessions/Playing",
            {
                "ItemId": item_id,
                "MediaSourceId": media_source_id or item_id,
                "PlaySessionId": play_session_id,
                "CanSeek": True,
                "PlayMethod": play_method,
                "PositionTicks": position_ticks,
            },
        )

    def report_playback_progress(
        self,
        item_id: str,
        position_ticks: int,
        is_paused: bool = False,
        play_session_id: str = "",
        play_method: str = "DirectStream",
        media_source_id: str = "",
        event_name: str = "",
    ):
        payload = {
            "ItemId": item_id,
            "MediaSourceId": media_source_id or item_id,
            "PlaySessionId": play_session_id,
            "PositionTicks": position_ticks,
            "IsPaused": is_paused,
            "PlayMethod": play_method,
        }
        # Optional EventName ("timeupdate" / "pause" / "unpause") — some
        # admin tools key UI updates off this string.
        if event_name:
            payload["EventName"] = event_name
        self._post("/Sessions/Playing/Progress", payload)

    def report_playback_stopped(
        self,
        item_id: str,
        position_ticks: int,
        play_session_id: str = "",
        play_method: str = "DirectStream",
        media_source_id: str = "",
    ):
        self._post(
            "/Sessions/Playing/Stopped",
            {
                "ItemId": item_id,
                "MediaSourceId": media_source_id or item_id,
                "PlaySessionId": play_session_id,
                "PlayMethod": play_method,
                "PositionTicks": position_ticks,
            },
        )

    def mark_played(self, item_id: str):
        self._post(f"/Users/{self.user_id}/PlayedItems/{item_id}")

    def mark_unplayed(self, item_id: str):
        try:
            self.session.delete(
                f"{self.server_url}/Users/{self.user_id}/PlayedItems/{item_id}",
                headers=self._headers(),
                timeout=5,
            )
        except Exception:
            pass

    def toggle_favorite(self, item_id: str, favorite: bool):
        if favorite:
            self._post(f"/Users/{self.user_id}/FavoriteItems/{item_id}")
        else:
            try:
                self.session.delete(
                    f"{self.server_url}/Users/{self.user_id}/FavoriteItems/{item_id}",
                    headers=self._headers(),
                    timeout=5,
                )
            except Exception:
                pass
        # The cached `get_item` snapshot for this id carries a stale
        # `UserData.IsFavorite` until we drop it.
        self.invalidate_meta_cache(item_id)

    # ── Metadata editing ─────────────────────────────────────────────────────
    #
    # v1 editable-field whitelist. Each entry maps the BaseItemDto
    # property the caller passes in to the `LockedFields` enum value
    # Jellyfin uses to pin that property server-side. Mapping notes:
    #   * Name/Genres lock via their canonical MetadataField enum names.
    #   * Artists / AlbumArtist lock via "Cast" — the Jellyfin
    #     MetadataField enum collapses every people-list edit under Cast.
    #   * Album, IndexNumber, ProductionYear have no canonical enum entry
    #     in MetadataField; we still write the literal field name into
    #     LockedFields so the value the user typed is at least visibly
    #     pinned in the editor UI (Jellyfin ignores unknown enum strings
    #     rather than 400-ing; this is the documented escape hatch).
    EDITABLE_FIELDS = {
        "Name": "Name",
        "Artists": "Cast",
        "Album": "Name",
        "AlbumArtist": "Cast",
        "Genres": "Genres",
        "IndexNumber": "IndexNumber",
        "ProductionYear": "ProductionYear",
    }

    def update_item_metadata(self, item_id: str, edits: Dict[str, Any]) -> Dict[str, Any]:
        """GET the full BaseItemDto, merge ``edits`` over it, append
        the matching LockedFields entries, then POST the full body
        back. The full-DTO round-trip is the documented workaround
        for jellyfin/jellyfin#10724 — sparse patches return 400 and
        can leave the item temporarily un-fetchable until rescan.

        Raises ValueError on an unknown edit key, HTTPError on a
        server-side rejection (4xx/5xx). Returns the merged BaseItemDto
        we POSTed (Jellyfin's update endpoint replies with an empty
        body on success).
        """
        unknown = [k for k in edits if k not in self.EDITABLE_FIELDS]
        if unknown:
            raise ValueError(
                f"Unknown edit field(s): {', '.join(sorted(unknown))}. "
                f"v1 editable subset: {', '.join(sorted(self.EDITABLE_FIELDS))}."
            )

        current = self._get(f"/Users/{self.user_id}/Items/{item_id}")
        merged = copy.deepcopy(current) if current else {}

        # Apply edits.
        for key, value in edits.items():
            merged[key] = value

        # Ensure LockedFields contains the lock-name for every edited
        # key. Preserve any pre-existing locks so we never silently
        # unlock a field the user previously pinned.
        locked = list(merged.get("LockedFields") or [])
        for key in edits:
            lock_name = self.EDITABLE_FIELDS[key]
            if lock_name not in locked:
                locked.append(lock_name)
        merged["LockedFields"] = locked

        # POST raw so 4xx/5xx surface as HTTPError — _post swallows
        # everything, which is the wrong behaviour for a write the
        # user explicitly confirmed and wants feedback on.
        url = f"{self.server_url}/Items/{item_id}"
        from modules import offline as _offline

        try:
            r = self.session.post(
                url,
                headers=self._headers(),
                json=merged,
                timeout=15,
            )
        except requests.exceptions.RequestException:
            _offline.note_request_failure()
            raise
        _offline.note_request_success()
        if r.status_code in (401, 403):
            _offline.note_auth_failure()
        elif 200 <= r.status_code < 300:
            _offline.note_auth_success()
        r.raise_for_status()

        # The cached item snapshot is now stale.
        self.invalidate_meta_cache(item_id)
        return merged


# ── Singleton accessor ──────────────────────────────────────────────────────

_api: Optional[JellyfinAPI] = None


def get_api() -> JellyfinAPI:
    global _api
    if _api is None:
        _api = JellyfinAPI()
    return _api
