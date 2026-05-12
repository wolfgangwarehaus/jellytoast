"""JellyfinProvider — wraps the existing JellyfinAPI behind the
MediaProvider interface.

Phase 1 of the provider abstraction. The browse / playback methods
are pure delegations; the auth tier (probe / authenticate / verify /
logout) projects API responses into the normalized dataclasses
declared in ``base``. Existing callers of ``modules.jellyfin_api.get_api()``
keep working unchanged — the provider is an additional surface, not
a replacement.

When SubsonicProvider lands alongside this, the browse / playback
methods will need normalized return shapes so the views don't have
to switch on provider kind. That refactor is its own commit; for now
keeping JellyfinProvider thin means zero behavioral changes.
"""

from typing import Any, Dict, List, Optional

from modules.providers.base import MediaProvider, ServerInfo, AuthResult
from modules.jellyfin_api import get_api, JellyfinAPI


class JellyfinProvider(MediaProvider):
    """The Jellyfin backend. Holds a ``JellyfinAPI`` instance under
    ``self.api`` for the rare callers (mostly tests) that need direct
    access to the underlying HTTP client."""

    def __init__(self):
        self.api: JellyfinAPI = get_api()

    # ── Identity ──────────────────────────────────────────────────────

    @property
    def kind(self) -> str:
        return "jellyfin"

    @property
    def server_url(self) -> str:
        return self.api.server_url

    @property
    def user_id(self) -> str:
        return self.api.user_id

    @property
    def access_token(self) -> str:
        return self.api.token

    @property
    def device_id(self) -> str:
        return self.api.device_id

    @property
    def is_authenticated(self) -> bool:
        return self.api.is_authenticated

    # ── Auth tier ─────────────────────────────────────────────────────

    def probe(self, server_url: str) -> Optional[ServerInfo]:
        try:
            info = self.api.server_info(server_url)
        except Exception:
            return None
        if not info or "Id" not in info or "ProductName" not in info:
            return None
        return ServerInfo(
            server_id=info.get("Id", ""),
            name=info.get("ServerName", "") or info.get("Name", ""),
            product_name=info.get("ProductName", ""),
            version=info.get("Version", ""),
            raw=info,
        )

    def authenticate(self, server_url: str, username: str,
                     password: str) -> AuthResult:
        # JellyfinAPI.authenticate raises on failure (HTTPError);
        # the LoginView catches and translates into friendly text.
        # The returned `data` payload (raw server response) isn't used
        # by callers — they read everything off the api object's
        # state, populated as a side effect of authenticate().
        self.api.authenticate(server_url, username, password)
        return AuthResult(
            server_url=self.api.server_url,
            user_id=self.api.user_id,
            username=username,
            access_token=self.api.token,
        )

    def verify_session(self) -> bool:
        return self.api.verify_session()

    def server_logout(self) -> bool:
        return self.api.server_logout()

    # ── Browse tier (delegations) ─────────────────────────────────────

    def get_libraries(self) -> List[Dict[str, Any]]:
        return self.api.get_libraries()

    def get_items(self, parent_id: str = "", item_type: str = "",
                  limit: int = 100, start_index: int = 0,
                  sort_by: str = "SortName",
                  sort_order: str = "Ascending",
                  recursive: bool = False, genre_ids: str = "",
                  filters: str = "", years: str = "") -> Dict[str, Any]:
        return self.api.get_items(
            parent_id, item_type, limit, start_index, sort_by,
            sort_order, recursive, genre_ids, filters, years,
        )

    def get_item(self, item_id: str) -> Dict[str, Any]:
        return self.api.get_item(item_id)

    def get_album_tracks(self, album_id: str) -> List[Dict[str, Any]]:
        return self.api.get_album_tracks(album_id)

    def get_artist_albums(self, artist_id: str) -> List[Dict[str, Any]]:
        return self.api.get_artist_albums(artist_id)

    def get_artists(self, limit: int = 200,
                    start_index: int = 0) -> List[Dict[str, Any]]:
        return self.api.get_artists(limit, start_index)

    def get_playlist_items(self, playlist_id: str) -> List[Dict[str, Any]]:
        return self.api.get_playlist_items(playlist_id)

    def get_genres(self) -> List[Dict[str, Any]]:
        return self.api.get_genres()

    def get_resume_items(self, limit: int = 12,
                         media_type: str = "") -> List[Dict[str, Any]]:
        return self.api.get_resume_items(limit, media_type)

    def get_latest_media(self, library_id: str = "",
                         limit: int = 16) -> List[Dict[str, Any]]:
        return self.api.get_latest_media(library_id, limit)

    def search(self, term: str, limit: int = 50,
               item_types: str = "") -> List[Dict[str, Any]]:
        return self.api.search(term, limit, item_types)

    def search_all(self, term: str, songs: int = 12,
                   albums: int = 14,
                   artists: int = 14) -> Dict[str, List[Dict[str, Any]]]:
        # Jellyfin's /Items?SearchTerm=... returns one type per call,
        # so we make up to three round-trips. Skipping a bucket when
        # its cap is 0 saves a request when the caller wants only
        # albums/artists/etc.
        out: Dict[str, List[Dict[str, Any]]] = {
            "Audio": [], "MusicAlbum": [], "MusicArtist": [],
        }
        for type_key, limit in (("Audio", songs), ("MusicAlbum", albums),
                                ("MusicArtist", artists)):
            if limit > 0:
                out[type_key] = self.api.search(term, limit, type_key)
        # Expand artist matches: Jellyfin's SearchTerm matches on item
        # Name only, so a query like "feist" returns the artist record
        # but no albums or tracks (their names don't contain "feist").
        # If we got at least one artist hit and the album/track buckets
        # are sparse, pull the top artist's discography + a sample of
        # their tracks and merge. Subsonic's search3 already does this
        # server-side; this brings Jellyfin parity.
        if out["MusicArtist"]:
            top_artist = out["MusicArtist"][0]
            artist_id = (top_artist or {}).get("Id", "")
            if artist_id:
                if albums > 0 and len(out["MusicAlbum"]) < albums:
                    try:
                        more_albums = self.api.get_artist_albums(artist_id)
                    except Exception:
                        more_albums = []
                    seen = {a.get("Id") for a in out["MusicAlbum"]}
                    for a in more_albums:
                        if len(out["MusicAlbum"]) >= albums:
                            break
                        if a.get("Id") not in seen:
                            out["MusicAlbum"].append(a)
                            seen.add(a.get("Id"))
                if songs > 0 and len(out["Audio"]) < songs:
                    try:
                        tracks_resp = self.api._get(
                            f"/Users/{self.api.user_id}/Items",
                            {
                                "ArtistIds": artist_id,
                                "IncludeItemTypes": "Audio",
                                "Recursive": "true",
                                "Limit": songs,
                                "SortBy": "Album,SortName",
                                "Fields": "RunTimeTicks,Artists,"
                                          "AlbumArtist,IndexNumber,"
                                          "ParentIndexNumber",
                            },
                        )
                        more_tracks = tracks_resp.get("Items", []) or []
                    except Exception:
                        more_tracks = []
                    seen = {t.get("Id") for t in out["Audio"]}
                    for t in more_tracks:
                        if len(out["Audio"]) >= songs:
                            break
                        if t.get("Id") not in seen:
                            out["Audio"].append(t)
                            seen.add(t.get("Id"))
        return out

    def get_random_audio_items(self, parent_id: str,
                               limit: int = 500) -> List[Dict[str, Any]]:
        return self.api.get_random_audio_items(parent_id, limit)

    # ── Stream URLs ────────────────────────────────────────────────────

    def get_audio_stream_url(self, item_id: str) -> str:
        return self.api.get_audio_stream_url(item_id)

    def get_video_stream_url(self, item_id: str) -> str:
        return self.api.get_video_stream_url(item_id)

    def get_audio_transcode_url(self, item_id: str,
                                 max_bitrate_kbps: int = 320,
                                 codec: str = "mp3") -> str:
        # Force the server-side transcode via /Audio/{id}/stream.{ext}
        # — distinct from the user's audio_quality setting which only
        # affects mpv local playback. Chromecast cast paths call here
        # for any container Cast can't direct-play.
        bitrate = max_bitrate_kbps * 1000
        return (
            f"{self.api.server_url}/Audio/{item_id}/stream.{codec}"
            f"?api_key={self.api.token}"
            f"&MaxStreamingBitrate={bitrate}&AudioCodec={codec}"
        )

    def get_image_url(self, item_id: str, image_type: str = "Primary",
                      width: int = 400, fill: bool = False) -> str:
        return self.api.get_image_url(item_id, image_type, width, fill)

    def keep_alive_url(self) -> str:
        """Cheap GET URL for periodic heartbeats — keeps QNAM's TCP
        connection warm so the next real request skips fresh TCP/TLS
        handshake. Public endpoint that needs no auth, returns small."""
        if not self.server_url:
            return ""
        return f"{self.server_url.rstrip('/')}/System/Info/Public"

    # ── Playback reporting ─────────────────────────────────────────────

    def report_playback_start(self, item_id: str, position_ticks: int = 0,
                              play_session_id: str = "",
                              play_method: str = "DirectStream",
                              media_source_id: str = "") -> None:
        self.api.report_playback_start(
            item_id, position_ticks,
            play_session_id=play_session_id,
            play_method=play_method,
            media_source_id=media_source_id,
        )

    def report_playback_progress(self, item_id: str, position_ticks: int,
                                  is_paused: bool = False,
                                  play_session_id: str = "",
                                  play_method: str = "DirectStream",
                                  media_source_id: str = "",
                                  event_name: str = "") -> None:
        self.api.report_playback_progress(
            item_id, position_ticks, is_paused,
            play_session_id=play_session_id,
            play_method=play_method,
            media_source_id=media_source_id,
            event_name=event_name,
        )

    def report_playback_stopped(self, item_id: str, position_ticks: int,
                                play_session_id: str = "",
                                play_method: str = "DirectStream",
                                media_source_id: str = "") -> None:
        self.api.report_playback_stopped(
            item_id, position_ticks,
            play_session_id=play_session_id,
            play_method=play_method,
            media_source_id=media_source_id,
        )

    def mark_played(self, item_id: str) -> None:
        self.api.mark_played(item_id)

    def mark_unplayed(self, item_id: str) -> None:
        self.api.mark_unplayed(item_id)

    def toggle_favorite(self, item_id: str, favorite: bool) -> None:
        self.api.toggle_favorite(item_id, favorite)

    def get_lyrics(self, item_id: str) -> Optional[Dict[str, Any]]:
        return self.api.get_lyrics(item_id)

    # ── Cache control ──────────────────────────────────────────────────

    def invalidate_meta_cache(self, item_id: str = "") -> None:
        self.api.invalidate_meta_cache(item_id)
