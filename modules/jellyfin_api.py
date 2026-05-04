"""
Jellyfin REST API client.
Supports library browsing, music navigation (artists/albums/tracks),
direct audio streams (bit-perfect), HLS video, lyrics, and playback reporting.
"""

import copy
import requests
import uuid
from collections import OrderedDict
from typing import Optional, List, Dict, Any, Tuple
from modules.settings import get_settings


CLIENT_NAME = "JellyToast"
CLIENT_VERSION = "1.0.0"
DEVICE_NAME = "JellyToast Desktop"


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
        return bool(self.token and self.user_id and self.server_url)

    @property
    def auth_header(self) -> str:
        h = (f'MediaBrowser Client="{CLIENT_NAME}", Device="{DEVICE_NAME}", '
             f'DeviceId="{self.device_id}", Version="{CLIENT_VERSION}"')
        if self.token:
            h += f', Token="{self.token}"'
        return h

    def _headers(self) -> Dict[str, str]:
        return {"X-Emby-Authorization": self.auth_header,
                "Content-Type": "application/json"}

    # ── Auth ────────────────────────────────────────────────────────────────

    def authenticate(self, server_url: str, username: str, password: str) -> Dict:
        self.server_url = server_url.rstrip("/")
        url = f"{self.server_url}/Users/AuthenticateByName"
        resp = self.session.post(url, json={"Username": username, "Pw": password},
                                  headers=self._headers(), timeout=10)
        resp.raise_for_status()
        data = resp.json()
        self.token = data["AccessToken"]
        self.user_id = data["User"]["Id"]
        # Persist
        self.settings.server_url = self.server_url
        self.settings.username = username
        self.settings.access_token = self.token
        self.settings.user_id = self.user_id
        return data

    def verify_session(self) -> bool:
        """Test current credentials against server."""
        if not self.is_authenticated:
            return False
        try:
            r = self.session.get(f"{self.server_url}/Users/{self.user_id}",
                                  headers=self._headers(), timeout=5)
            return r.status_code == 200
        except Exception:
            return False

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
                headers=self._headers(), timeout=5,
            )
            return True
        except Exception as e:
            print(f"[JellyToast] /Sessions/Logout failed: {e}", flush=True)
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
        url = f"{self.server_url}{path}"
        r = self.session.get(url, headers=self._headers(), params=params or {}, timeout=15)
        r.raise_for_status()
        return r.json() if r.content else {}

    def _post(self, path: str, payload: Optional[Dict] = None) -> Optional[Dict]:
        url = f"{self.server_url}{path}"
        try:
            r = self.session.post(url, headers=self._headers(),
                                   json=payload or {}, timeout=10)
            return r.json() if r.content else None
        except Exception:
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

    def get_items(self, parent_id: str = "", item_type: str = "", limit: int = 100,
                  start_index: int = 0, sort_by: str = "SortName",
                  sort_order: str = "Ascending", recursive: bool = False,
                  genre_ids: str = "", filters: str = "") -> Dict:
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
        return self._get(f"/Users/{self.user_id}/Items", params)

    def search(self, term: str, limit: int = 50, item_types: str = "") -> List[Dict]:
        # `item_types` is the comma-separated IncludeItemTypes; default
        # casts a wide net across all media kinds. Native Search calls
        # this once per kind ("Audio" / "MusicAlbum" / "MusicArtist") so
        # each per-section result list has a deterministic cap.
        params = {
            "SearchTerm": term, "UserId": self.user_id, "Recursive": True, "Limit": limit,
            "IncludeItemTypes": item_types or "Movie,Series,Episode,Audio,MusicAlbum,MusicArtist",
            "Fields": "PrimaryImageAspectRatio,ProductionYear,AlbumArtist",
        }
        return self._get("/Items", params).get("Items", [])

    # ── Music ───────────────────────────────────────────────────────────────

    def get_artists(self, limit: int = 200, start_index: int = 0) -> List[Dict]:
        params = {
            "UserId": self.user_id, "Limit": limit, "StartIndex": start_index,
            "SortBy": "SortName", "SortOrder": "Ascending",
            "Fields": "PrimaryImageAspectRatio",
        }
        return self._get("/Artists/AlbumArtists", params).get("Items", [])

    def get_artist_albums(self, artist_id: str) -> List[Dict]:
        def _fetch():
            params = {
                "AlbumArtistIds": artist_id, "UserId": self.user_id,
                "IncludeItemTypes": "MusicAlbum", "Recursive": True,
                "SortBy": "PremiereDate,SortName", "SortOrder": "Descending",
                "Fields": "PrimaryImageAspectRatio,ProductionYear,ChildCount",
            }
            return self._get(f"/Users/{self.user_id}/Items", params).get("Items", [])
        return self._cached("artist_albums", artist_id, _fetch)

    def get_album_tracks(self, album_id: str) -> List[Dict]:
        def _fetch():
            params = {
                "ParentId": album_id, "UserId": self.user_id,
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

    def get_albums(self, limit: int = 200, sort: str = "SortName") -> List[Dict]:
        params = {
            "UserId": self.user_id, "IncludeItemTypes": "MusicAlbum",
            "Recursive": True, "Limit": limit,
            "SortBy": sort, "SortOrder": "Ascending",
            "Fields": "PrimaryImageAspectRatio,ProductionYear,AlbumArtist",
        }
        return self._get(f"/Users/{self.user_id}/Items", params).get("Items", [])

    def get_genres(self) -> List[Dict]:
        params = {"UserId": self.user_id, "IncludeItemTypes": "Audio,MusicAlbum",
                  "Recursive": True}
        return self._get("/MusicGenres", params).get("Items", [])

    def get_lyrics(self, item_id: str) -> Optional[Dict]:
        try:
            return self._get(f"/Audio/{item_id}/Lyrics")
        except Exception:
            return None

    # ── TV ──────────────────────────────────────────────────────────────────

    def get_seasons(self, series_id: str) -> List[Dict]:
        return self._get(f"/Shows/{series_id}/Seasons",
                          {"UserId": self.user_id}).get("Items", [])

    def get_episodes(self, series_id: str, season_id: str = "") -> List[Dict]:
        params = {"UserId": self.user_id, "Fields": "PrimaryImageAspectRatio,Overview"}
        if season_id:
            params["SeasonId"] = season_id
        return self._get(f"/Shows/{series_id}/Episodes", params).get("Items", [])

    # ── Item details ────────────────────────────────────────────────────────

    def get_item(self, item_id: str) -> Dict:
        return self._cached(
            "item", item_id,
            lambda: self._get(f"/Users/{self.user_id}/Items/{item_id}"),
        )

    def get_playback_info(self, item_id: str) -> Dict:
        params = {"UserId": self.user_id}
        return self._get(f"/Items/{item_id}/PlaybackInfo", params)

    # ── Stream URLs ─────────────────────────────────────────────────────────

    def get_audio_stream_url(self, item_id: str, container_priority: bool = True) -> str:
        """
        Direct audio stream — bit-perfect when format is supported.
        Uses /Audio/{id}/stream which honors static=true for direct play.

        Session-binding query params (UserId, DeviceId, MediaSourceId)
        let the server's stream tracker attribute the bytes to the
        same session our /Sessions/Playing reports use. Without them
        the server treats the stream as anonymous and play-count
        attribution drifts.
        """
        quality = self.settings.audio_quality
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
            return (
                f"{self.server_url}/Audio/{item_id}/stream"
                f"?{common}&static=true"
            )
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

    def get_hls_url(self, item_id: str, max_bitrate: int = 40_000_000) -> str:
        params = {
            "DeviceId": self.device_id, "api_key": self.token,
            "VideoCodec": "h264", "AudioCodec": "aac",
            "MaxStreamingBitrate": max_bitrate, "SegmentContainer": "ts",
            "MinSegments": 2, "BreakOnNonKeyFrames": "True",
        }
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        return f"{self.server_url}/Videos/{item_id}/master.m3u8?{qs}"

    def get_image_url(self, item_id: str, image_type: str = "Primary",
                      width: int = 400, fill: bool = False) -> str:
        if not item_id:
            return ""
        path = "FillWidth" if fill else "width"
        return (f"{self.server_url}/Items/{item_id}/Images/{image_type}"
                f"?{path}={width}&quality=90&api_key={self.token}")

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

    def report_playback_start(self, item_id: str, position_ticks: int = 0,
                              play_session_id: str = "",
                              play_method: str = "DirectStream",
                              media_source_id: str = ""):
        self._post("/Sessions/Playing", {
            "ItemId": item_id,
            "MediaSourceId": media_source_id or item_id,
            "PlaySessionId": play_session_id,
            "CanSeek": True,
            "PlayMethod": play_method,
            "PositionTicks": position_ticks,
        })

    def report_playback_progress(self, item_id: str, position_ticks: int,
                                  is_paused: bool = False,
                                  play_session_id: str = "",
                                  play_method: str = "DirectStream",
                                  media_source_id: str = "",
                                  event_name: str = ""):
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

    def report_playback_stopped(self, item_id: str, position_ticks: int,
                                play_session_id: str = "",
                                play_method: str = "DirectStream",
                                media_source_id: str = ""):
        self._post("/Sessions/Playing/Stopped", {
            "ItemId": item_id,
            "MediaSourceId": media_source_id or item_id,
            "PlaySessionId": play_session_id,
            "PlayMethod": play_method,
            "PositionTicks": position_ticks,
        })

    def mark_played(self, item_id: str):
        self._post(f"/Users/{self.user_id}/PlayedItems/{item_id}")

    def mark_unplayed(self, item_id: str):
        try:
            self.session.delete(f"{self.server_url}/Users/{self.user_id}/PlayedItems/{item_id}",
                                headers=self._headers(), timeout=5)
        except Exception:
            pass

    def toggle_favorite(self, item_id: str, favorite: bool):
        if favorite:
            self._post(f"/Users/{self.user_id}/FavoriteItems/{item_id}")
        else:
            try:
                self.session.delete(
                    f"{self.server_url}/Users/{self.user_id}/FavoriteItems/{item_id}",
                    headers=self._headers(), timeout=5)
            except Exception:
                pass
        # The cached `get_item` snapshot for this id carries a stale
        # `UserData.IsFavorite` until we drop it.
        self.invalidate_meta_cache(item_id)


# ── Singleton accessor ──────────────────────────────────────────────────────

_api: Optional[JellyfinAPI] = None


def get_api() -> JellyfinAPI:
    global _api
    if _api is None:
        _api = JellyfinAPI()
    return _api
