"""MediaProvider — abstract backend interface.

A native client should be able to talk to any music backend with the
same UI. This module defines the contract every backend implements;
``modules.providers.jellyfin.JellyfinProvider`` and (later)
``modules.providers.subsonic.SubsonicProvider`` plug in behind it.

Phase 1 of this work covers the *auth tier* concretely — sign-in,
session verify, server probe, sign-out — because that's where
provider selection matters first. The browse / playback methods are
declared for completeness and will be wired up across the codebase
incrementally; for now they delegate to JellyfinAPI inside
JellyfinProvider so existing call sites keep working without changes.

Data shapes:

* Auth-tier methods return small, normalized dataclasses
  (``ServerInfo``, ``AuthResult``) — these never leak provider-
  specific JSON to callers.
* Browse-tier methods currently return raw provider dicts (Jellyfin
  PascalCase). A future phase normalizes these to a JellyToast-
  internal schema (``id``, ``title``, ``album_artist``, ``track``,
  …) so views don't need provider-aware code.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Dict, List, Optional


# ── Auth-tier normalized shapes ──────────────────────────────────────


@dataclass(frozen=True)
class ServerInfo:
    """Pre-auth server metadata. Returned by ``probe()``; used by the
    login view to confirm a URL is actually a music backend before
    sending the password."""
    server_id: str
    name: str
    product_name: str
    version: str
    raw: Dict[str, Any]


@dataclass(frozen=True)
class AuthResult:
    """Outcome of a successful authenticate. Persisted credentials
    derive from this."""
    server_url: str
    user_id: str
    username: str
    access_token: str


# ── Provider ABC ─────────────────────────────────────────────────────


class MediaProvider(ABC):
    """Interface every backend implements. Methods are grouped by
    concern; concrete implementations add caching / async semantics
    as appropriate (Jellyfin uses an LRU + disk cache; Subsonic will
    use per-request auth params and probably a similar cache)."""

    # ── Identity ──────────────────────────────────────────────────────

    @property
    @abstractmethod
    def kind(self) -> str:
        """Stable backend identifier — ``"jellyfin"`` or
        ``"subsonic"``. Used by the settings layer to remember which
        provider to instantiate."""

    @property
    @abstractmethod
    def server_url(self) -> str: ...

    @property
    @abstractmethod
    def user_id(self) -> str: ...

    @property
    @abstractmethod
    def access_token(self) -> str: ...

    @property
    @abstractmethod
    def device_id(self) -> str: ...

    @property
    @abstractmethod
    def is_authenticated(self) -> bool: ...

    # ── Auth tier ─────────────────────────────────────────────────────

    @abstractmethod
    def probe(self, server_url: str) -> Optional[ServerInfo]:
        """Pre-auth server metadata fetch. Returns None if the URL
        doesn't look like a backend of this kind. Should not raise
        on network errors — return None instead."""

    @abstractmethod
    def authenticate(self, server_url: str, username: str,
                     password: str) -> AuthResult:
        """Sign in. On success persists credentials to settings and
        returns the AuthResult. On failure raises (the LoginView
        translates exceptions into friendly messages)."""

    @abstractmethod
    def verify_session(self) -> bool:
        """True if the current credentials still validate against the
        server. False ONLY on definitive rejection (HTTP 401/403 for
        Jellyfin; equivalent error code for Subsonic). Network errors
        / timeouts should return True (assume valid; let the next
        real call surface a 401 if there is one)."""

    @abstractmethod
    def server_logout(self) -> bool:
        """Tell the server to revoke this device's session. Best-
        effort; failures don't block local sign-out."""

    # ── Browse tier (raw passthrough for now) ─────────────────────────
    #
    # These currently return provider-native dicts (Jellyfin's
    # PascalCase items). A future phase normalizes them to a
    # JellyToast-internal schema. Until then the views read these as
    # they always have, and JellyfinProvider just delegates.

    @abstractmethod
    def get_libraries(self) -> List[Dict[str, Any]]: ...

    @abstractmethod
    def get_items(self, parent_id: str = "", item_type: str = "",
                  limit: int = 100, start_index: int = 0,
                  sort_by: str = "SortName",
                  sort_order: str = "Ascending",
                  recursive: bool = False, genre_ids: str = "",
                  filters: str = "", years: str = "") -> Dict[str, Any]: ...

    @abstractmethod
    def get_item(self, item_id: str) -> Dict[str, Any]: ...

    @abstractmethod
    def get_album_tracks(self, album_id: str) -> List[Dict[str, Any]]: ...

    @abstractmethod
    def get_artist_albums(self, artist_id: str) -> List[Dict[str, Any]]: ...

    @abstractmethod
    def get_artists(self, limit: int = 200,
                    start_index: int = 0) -> List[Dict[str, Any]]: ...

    @abstractmethod
    def get_playlist_items(self, playlist_id: str) -> List[Dict[str, Any]]: ...

    @abstractmethod
    def get_genres(self) -> List[Dict[str, Any]]: ...

    @abstractmethod
    def get_resume_items(self, limit: int = 12,
                         media_type: str = "") -> List[Dict[str, Any]]: ...

    @abstractmethod
    def get_latest_media(self, library_id: str = "",
                         limit: int = 16) -> List[Dict[str, Any]]: ...

    @abstractmethod
    def search(self, term: str, limit: int = 50,
               item_types: str = "") -> List[Dict[str, Any]]: ...

    @abstractmethod
    def search_all(self, term: str, songs: int = 12,
                   albums: int = 14,
                   artists: int = 14) -> Dict[str, List[Dict[str, Any]]]:
        """Multi-type search returning all three buckets in one call.
        Result shape: ``{"Audio": [...], "MusicAlbum": [...],
        "MusicArtist": [...]}``. Implementations are free to round-trip
        once (Subsonic's search3 returns all three natively) or
        multiple times (Jellyfin's API is per-type). Per-bucket caps
        let callers tune the visual density; passing 0 for a bucket
        skips that fetch entirely."""

    @abstractmethod
    def get_random_audio_items(self, parent_id: str,
                               limit: int = 500) -> List[Dict[str, Any]]: ...

    # ── Stream URLs ────────────────────────────────────────────────────

    @abstractmethod
    def get_audio_stream_url(self, item_id: str) -> str: ...

    @abstractmethod
    def get_video_stream_url(self, item_id: str) -> str:
        """Bit-perfect video URL. Music-only providers (Subsonic /
        Navidrome) return empty string — the host's queue manager
        routes here only when np.is_audio is False, which doesn't
        happen for music libraries."""

    @abstractmethod
    def get_audio_transcode_url(self, item_id: str,
                                 max_bitrate_kbps: int = 320,
                                 codec: str = "mp3") -> str:
        """URL that streams the item transcoded to ``codec`` capped at
        ``max_bitrate_kbps``. Used as the Chromecast direct-play
        fallback when the source container isn't in Cast's supported
        list. Caller is responsible for setting the matching MIME
        (audio/mpeg for mp3, etc.) on the cast metadata."""

    @abstractmethod
    def get_image_url(self, item_id: str, image_type: str = "Primary",
                      width: int = 400, fill: bool = False) -> str: ...

    # ── Playback reporting ─────────────────────────────────────────────

    @abstractmethod
    def report_playback_start(self, item_id: str, position_ticks: int = 0,
                              play_session_id: str = "",
                              play_method: str = "DirectStream",
                              media_source_id: str = "") -> None: ...

    @abstractmethod
    def report_playback_progress(self, item_id: str, position_ticks: int,
                                  is_paused: bool = False,
                                  play_session_id: str = "",
                                  play_method: str = "DirectStream",
                                  media_source_id: str = "",
                                  event_name: str = "") -> None: ...

    @abstractmethod
    def report_playback_stopped(self, item_id: str, position_ticks: int,
                                play_session_id: str = "",
                                play_method: str = "DirectStream",
                                media_source_id: str = "") -> None: ...

    @abstractmethod
    def mark_played(self, item_id: str) -> None: ...

    @abstractmethod
    def mark_unplayed(self, item_id: str) -> None: ...

    @abstractmethod
    def toggle_favorite(self, item_id: str, favorite: bool) -> None: ...

    @abstractmethod
    def get_lyrics(self, item_id: str) -> Optional[Dict[str, Any]]: ...

    # ── Cache control ──────────────────────────────────────────────────

    @abstractmethod
    def invalidate_meta_cache(self, item_id: str = "") -> None: ...
