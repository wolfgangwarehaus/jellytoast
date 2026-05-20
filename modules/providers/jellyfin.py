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

from __future__ import annotations

import copy
import uuid
from typing import Any, Dict, List, Optional, Tuple

from modules.providers.base import MediaProvider, ServerInfo, AuthResult
from modules.jellyfin_api import get_api, JellyfinAPI


# Sort-field map: schema field → Jellyfin SortBy key. Anything not in
# the map falls back to SortName so the request still completes.
_JF_SORT_MAP = {
    "year": "ProductionYear",
    "artist": "AlbumArtist",
    "album": "Album",
    "play_count": "PlayCount",
    "rating": "CommunityRating",
    "genre": "SortName",
    # Jellyfin's /Items SortBy exposes both date columns natively;
    # the Python refine pass re-sorts anyway, but emitting the right
    # SortBy keeps the server-side order sensible before refinement.
    "date_added": "DateCreated",
    "last_played": "DatePlayed",
}


def _build_jf_query(
    rules: List[Dict[str, Any]], sort: Optional[str], sort_desc: bool
) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """Translate a flat rule list into Jellyfin /Items query params.

    Returns ``(params, satisfied)``. ``satisfied`` is the subset of
    ``rules`` that the server-side params fully enforce — the caller
    can skip re-applying those during the Python refine pass since
    every response item is guaranteed to match them. Any rule *not*
    in ``satisfied`` needs the Python refine pass to enforce.
    """
    params: Dict[str, Any] = {
        "IncludeItemTypes": "Audio",
        "Recursive": "true",
        # DateCreated is requested explicitly so the date_added rule
        # has a value to refine on; UserData (always returned) carries
        # LastPlayedDate for the last_played rule.
        "Fields": "PrimaryImageAspectRatio,ProductionYear,Artists,"
        "AlbumArtist,Album,Genres,CommunityRating,"
        "UserData,RunTimeTicks,DateCreated",
    }
    satisfied: List[Dict[str, Any]] = []
    genre_seen = False

    for rule in rules:
        field, op, value = rule["field"], rule["op"], rule["value"]

        if field == "genre" and op == "equals":
            # Genres= accepts a pipe-delimited list; we set a single
            # entry. If a later rule also targets genre, the server's
            # last-wins behavior means we'd lose the first — refine
            # the later one client-side instead.
            if not genre_seen:
                params["Genres"] = value
                genre_seen = True
                satisfied.append(rule)

        elif field == "year":
            if op == "equals":
                _accumulate_years(params, [int(value)])
                satisfied.append(rule)
            elif op == "between":
                lo, hi = int(value[0]), int(value[1])
                lo, hi = min(lo, hi), max(lo, hi)
                _accumulate_years(params, list(range(lo, hi + 1)))
                satisfied.append(rule)
            # greater_than / less_than: refine client-side. An open-
            # ended >X would mean enumerating thousands of years for
            # Jellyfin's Years filter.

        elif field == "play_count":
            if op == "greater_than":
                params["MinUserPlayCount"] = int(value) + 1
                satisfied.append(rule)
            elif op == "equals":
                # Min...= X widens to play_count >= X; refine to drop > X.
                params["MinUserPlayCount"] = int(value)

        elif field == "rating":
            if op == "greater_than":
                # MinCommunityRating is inclusive of the float bound;
                # bump by a small epsilon so an int "greater_than 3"
                # excludes exactly 3.
                params["MinCommunityRating"] = float(value) + 0.001
                satisfied.append(rule)

        elif field == "is_favorite" and op == "equals":
            # Jellyfin /Items exposes the IsFavorite filter directly.
            # `is_favorite equals True`  → IsFavorite=true (server drops
            #   everything else). `equals False` → IsFavorite=false.
            # Either way the server fully enforces the rule.
            params["IsFavorite"] = "true" if value else "false"
            satisfied.append(rule)

        # artist / album: no resolved ArtistIds / AlbumIds in v1, so
        # equals / contains / starts_with / ends_with on those fields
        # always refine client-side.

    params["SortBy"] = _JF_SORT_MAP.get(sort or "", "SortName")
    params["SortOrder"] = "Descending" if sort_desc else "Ascending"
    return params, satisfied


def _accumulate_years(params: Dict[str, Any], years: List[int]) -> None:
    """Merge a list of years into the Years= param, deduped + comma-
    joined. Multiple year rules compose into a single Years= filter
    so Jellyfin sees the union."""
    existing = params.get("Years", "")
    seen = set()
    if existing:
        for s in existing.split(","):
            try:
                seen.add(int(s))
            except ValueError:
                pass
    seen.update(years)
    params["Years"] = ",".join(str(y) for y in sorted(seen))


def _recent_date_bound(
    rules: List[Dict[str, Any]],
) -> Optional[Tuple[str, str, Any]]:
    """Find the tightest "items must be at least this recent" bound a
    rule list implies via an ``in_the_last`` / ``after`` date rule.

    Returns ``(field, jf_sort_key, cutoff_datetime)`` — the field, its
    Jellyfin ``SortBy`` key, and the earliest datetime a matching item
    can carry — or None when no such rule is present.

    Used only on the match=all (or single-rule) fetch path: every rule
    there must hold, so the result can only contain items at or after
    the latest cutoff, which lets the fetch stop paging early. When
    several date rules apply, the latest cutoff wins (tightest bound)."""
    from datetime import datetime, timedelta

    from modules.providers.smart_rule_schema import parse_iso_date

    best: Optional[Tuple[str, str, Any]] = None
    for rule in rules:
        field = rule.get("field")
        op = rule.get("op")
        value = rule.get("value")
        if field not in ("date_added", "last_played"):
            continue
        if op == "in_the_last" and isinstance(value, int) and not isinstance(value, bool):
            cutoff = datetime.now() - timedelta(days=value)
        elif op == "after":
            try:
                cutoff = parse_iso_date(value)
            except (ValueError, TypeError):
                continue
        else:
            continue
        jf_key = _JF_SORT_MAP.get(field)
        if jf_key and (best is None or cutoff > best[2]):
            best = (field, jf_key, cutoff)
    return best


class JellyfinProvider(MediaProvider):
    """The Jellyfin backend. Holds a ``JellyfinAPI`` instance under
    ``self.api`` for the rare callers (mostly tests) that need direct
    access to the underlying HTTP client."""

    # Capability surface — Jellyfin exposes the item-update endpoint at
    # the API layer. Whether the *current user* is permitted to call it
    # is a runtime UserPolicy check the UI layer adds on top of this
    # boolean (see docs/research/tag_editing.md § 5).
    can_edit_metadata = True

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

    def authenticate(self, server_url: str, username: str, password: str) -> AuthResult:
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

    def with_url(self, new_url: str) -> "JellyfinProvider":
        """Re-point the underlying JellyfinAPI at ``new_url``. Auth
        state (token / user_id / device_id) is preserved — same
        identity, different hostname. Mutates in place to match the
        rest of the singleton model. Returns ``self`` so callers can
        chain. The metadata cache is cleared because cache keys aren't
        host-namespaced and a host switch could change which items
        exist (rare, but cheap insurance)."""
        self.api.server_url = (new_url or "").rstrip("/")
        # Drop the LRU — cache keys don't carry the host, and on the
        # off chance the alternate URL points at a different server
        # entirely (mis-configuration) we don't want stale entries.
        try:
            self.api._meta_cache.clear()
        except Exception:
            pass
        return self

    # ── Browse tier (delegations) ─────────────────────────────────────

    def get_libraries(self) -> List[Dict[str, Any]]:
        return self.api.get_libraries()

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
    ) -> Dict[str, Any]:
        return self.api.get_items(
            parent_id,
            item_type,
            limit,
            start_index,
            sort_by,
            sort_order,
            recursive,
            genre_ids,
            filters,
            years,
        )

    def get_item(self, item_id: str) -> Dict[str, Any]:
        return self.api.get_item(item_id)

    def get_album_tracks(self, album_id: str) -> List[Dict[str, Any]]:
        return self.api.get_album_tracks(album_id)

    def get_artist_albums(self, artist_id: str) -> List[Dict[str, Any]]:
        return self.api.get_artist_albums(artist_id)

    def get_artists(self, limit: int = 200, start_index: int = 0) -> List[Dict[str, Any]]:
        return self.api.get_artists(limit, start_index)

    def get_playlist_items(self, playlist_id: str) -> List[Dict[str, Any]]:
        return self.api.get_playlist_items(playlist_id)

    def get_genres(self) -> List[Dict[str, Any]]:
        return self.api.get_genres()

    def get_resume_items(self, limit: int = 12, media_type: str = "") -> List[Dict[str, Any]]:
        return self.api.get_resume_items(limit, media_type)

    def get_latest_media(self, library_id: str = "", limit: int = 16) -> List[Dict[str, Any]]:
        return self.api.get_latest_media(library_id, limit)

    def search(self, term: str, limit: int = 50, item_types: str = "") -> List[Dict[str, Any]]:
        return self.api.search(term, limit, item_types)

    def search_all(
        self, term: str, songs: int = 12, albums: int = 14, artists: int = 14
    ) -> Dict[str, List[Dict[str, Any]]]:
        # Jellyfin's /Items?SearchTerm=... returns one type per call,
        # so we make up to three round-trips. Skipping a bucket when
        # its cap is 0 saves a request when the caller wants only
        # albums/artists/etc.
        out: Dict[str, List[Dict[str, Any]]] = {
            "Audio": [],
            "MusicAlbum": [],
            "MusicArtist": [],
        }
        for type_key, limit in (("Audio", songs), ("MusicAlbum", albums), ("MusicArtist", artists)):
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

    def get_random_audio_items(self, parent_id: str, limit: int = 500) -> List[Dict[str, Any]]:
        return self.api.get_random_audio_items(parent_id, limit)

    # ── Seeded radio ──────────────────────────────────────────────────

    def get_similar_songs(self, item_id: str, count: int = 50) -> List[Dict[str, Any]]:
        """``/Items/{id}/Similar`` — Jellyfin's recommendation engine
        keyed off MusicBrainz tags and play history. Accepts track /
        album / artist IDs; server returns whichever mix it thinks is
        relevant. Fields request matches the rest of the audio paths
        so the returned dicts plug into the queue with no extra fetches."""
        if not item_id:
            return []
        params = {
            "UserId": self.api.user_id,
            "Limit": count,
            "Fields": ("RunTimeTicks,Artists,AlbumArtist,AlbumId,IndexNumber,ParentIndexNumber"),
        }
        try:
            resp = self.api._get(f"/Items/{item_id}/Similar", params)
        except Exception:
            return []
        return resp.get("Items", []) or []

    def get_instant_mix(self, item_id: str, count: int = 50) -> List[Dict[str, Any]]:
        """``/Items/{id}/InstantMix`` — Jellyfin's curated radio
        sequence. Distinct from ``/Items/{id}/Similar``: InstantMix is
        an ordered playlist Jellyfin builds for lean-back playback,
        while Similar is an unordered bag. Subsonic doesn't make this
        distinction; both providers expose both entry points for
        parity, the queue manager picks which fits the UX."""
        if not item_id:
            return []
        params = {
            "UserId": self.api.user_id,
            "Limit": count,
            "Fields": ("RunTimeTicks,Artists,AlbumArtist,AlbumId,IndexNumber,ParentIndexNumber"),
        }
        try:
            resp = self.api._get(f"/Items/{item_id}/InstantMix", params)
        except Exception:
            return []
        return resp.get("Items", []) or []

    def get_genre_radio(
        self, genre_name: str, count: int = 50, offset: int = 0
    ) -> List[Dict[str, Any]]:
        """Filter ``/Items`` by genre name + random sort. Jellyfin
        accepts the genre as a string here (the ``Genres`` query param,
        pipe-separated for multi-genre); no need to resolve to a genre
        item id first. ``offset`` maps to ``StartIndex`` so the
        seeded-radio feeder can page past tracks already heard."""
        if not genre_name:
            return []
        params = {
            "UserId": self.api.user_id,
            "Genres": genre_name,
            "IncludeItemTypes": "Audio",
            "Recursive": True,
            "SortBy": "Random",
            "Limit": count,
            "Fields": ("RunTimeTicks,Artists,AlbumArtist,AlbumId,IndexNumber,ParentIndexNumber"),
        }
        if offset > 0:
            params["StartIndex"] = offset
        try:
            resp = self.api._get(
                f"/Users/{self.api.user_id}/Items",
                params,
            )
        except Exception:
            return []
        return resp.get("Items", []) or []

    # ── Stream URLs ────────────────────────────────────────────────────

    def get_audio_stream_url(self, item_id: str, quality: "str | None" = None) -> str:
        return self.api.get_audio_stream_url(item_id, quality=quality)

    def get_video_stream_url(self, item_id: str) -> str:
        return self.api.get_video_stream_url(item_id)

    def get_audio_transcode_url(
        self, item_id: str, max_bitrate_kbps: int = 320, codec: str = "mp3"
    ) -> str:
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

    def get_image_url(
        self, item_id: str, image_type: str = "Primary", width: int = 400, fill: bool = False
    ) -> str:
        return self.api.get_image_url(item_id, image_type, width, fill)

    def keep_alive_url(self) -> str:
        """Cheap GET URL for periodic heartbeats — keeps QNAM's TCP
        connection warm so the next real request skips fresh TCP/TLS
        handshake. Public endpoint that needs no auth, returns small."""
        if not self.server_url:
            return ""
        return f"{self.server_url.rstrip('/')}/System/Info/Public"

    # ── Playback reporting ─────────────────────────────────────────────

    def report_playback_start(
        self,
        item_id: str,
        position_ticks: int = 0,
        play_session_id: str = "",
        play_method: str = "DirectStream",
        media_source_id: str = "",
    ) -> None:
        self.api.report_playback_start(
            item_id,
            position_ticks,
            play_session_id=play_session_id,
            play_method=play_method,
            media_source_id=media_source_id,
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
    ) -> None:
        self.api.report_playback_progress(
            item_id,
            position_ticks,
            is_paused,
            play_session_id=play_session_id,
            play_method=play_method,
            media_source_id=media_source_id,
            event_name=event_name,
        )

    def report_playback_stopped(
        self,
        item_id: str,
        position_ticks: int,
        play_session_id: str = "",
        play_method: str = "DirectStream",
        media_source_id: str = "",
    ) -> None:
        self.api.report_playback_stopped(
            item_id,
            position_ticks,
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

    # ── Internet radio ────────────────────────────────────────────────
    #
    # Jellyfin has no native internet-radio CRUD endpoint. The only
    # server-side path is the Live TV M3U tuner — a kludge most stations
    # break against (no #EXTINF directive). To preserve cross-provider
    # parity (the UI must never branch on provider kind) all four CRUD
    # methods are implemented locally against a QSettings-backed JSON
    # list at ``radio/stations``. Calls never reach a Jellyfin server
    # endpoint; the "settings-tier helper" is purely an implementation
    # detail behind the MediaProvider interface. Returned-dict shape
    # matches Subsonic exactly — ``{id, name, streamUrl, homePageUrl}``
    # — so callers can flow either provider through the same code path.
    # IDs are locally-minted (``local-<8 hex>``) so they can't collide
    # with any future server-side scheme.

    def get_internet_radio_stations(self) -> List[Dict[str, Any]]:
        """Return the locally-stored station list as a deep copy."""
        from modules.settings import get_settings

        return copy.deepcopy(get_settings().radio_stations)

    def create_internet_radio_station(
        self, name: str, stream_url: str, home_page_url: Optional[str] = None
    ) -> Dict[str, Any]:
        """Append a new station with a freshly-minted local id; persist
        and return the new dict."""
        from modules.settings import get_settings

        settings = get_settings()
        stations = settings.radio_stations
        new_station = {
            "id": f"local-{uuid.uuid4().hex[:8]}",
            "name": str(name),
            "streamUrl": str(stream_url),
            "homePageUrl": str(home_page_url or ""),
        }
        stations.append(new_station)
        settings.radio_stations = stations
        settings.flush()
        return dict(new_station)

    def update_internet_radio_station(
        self, station_id: str, name: str, stream_url: str, home_page_url: Optional[str] = None
    ) -> Dict[str, Any]:
        """Mutate fields on the station matching ``station_id``; raises
        ``ValueError`` if no station has that id."""
        from modules.settings import get_settings

        settings = get_settings()
        stations = settings.radio_stations
        for entry in stations:
            if str(entry.get("id")) == str(station_id):
                entry["name"] = str(name)
                entry["streamUrl"] = str(stream_url)
                entry["homePageUrl"] = str(home_page_url or "")
                settings.radio_stations = stations
                settings.flush()
                return dict(entry)
        raise ValueError(f"no local internet-radio station with id {station_id!r}")

    def delete_internet_radio_station(self, station_id: str) -> None:
        """Idempotent delete — missing id is silently ignored to match
        Subsonic's ``deleteInternetRadioStation`` semantics."""
        from modules.settings import get_settings

        settings = get_settings()
        stations = settings.radio_stations
        filtered = [s for s in stations if str(s.get("id")) != str(station_id)]
        if len(filtered) != len(stations):
            settings.radio_stations = filtered
            settings.flush()

    # ── Smart playlists ────────────────────────────────────────────────

    def query_items(self, rules: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Evaluate a smart-playlist rule set against Jellyfin's
        ``/Items`` endpoint.

        v1 builds a single ``/Items`` query from the rule set,
        translating each rule into the matching Jellyfin filter param.
        The server's natively-supported filters cover most of v1's
        field catalogue; ``contains`` operators that aren't
        server-side filters fall through to client-side filtering on
        the returned page.

        Native server-side coverage::

            genre equals X         → Genres=X
            year equals X          → Years=X
            year between [lo,hi]   → Years=lo,lo+1,...,hi
            play_count gt X        → MinUserPlayCount=X+1
            play_count equals X    → MinUserPlayCount=X (+ client refine
                                     to drop play_count > X)
            rating gt X            → MinCommunityRating=X+0.001
            is_favorite equals X   → IsFavorite=true|false
            artist equals X        → ArtistIds=<resolved id>  (skipped
                                     in v1: server-side resolution
                                     deferred to the engine)
            album equals X         → AlbumIds=<resolved id>   (same)

        Client-side refines (applied after the server call returns)::

            artist contains X      → substring match on Artists / AlbumArtist
            artist starts_with X   → prefix match on Artists / AlbumArtist
            artist ends_with X     → suffix match on Artists / AlbumArtist
            album contains X       → substring match on Album
            album starts_with X    → prefix match on Album
            album ends_with X      → suffix match on Album
            genre not_equals X     → drop items whose Genres include X
            year less_than X       → drop items with ProductionYear >= X
                                     (Jellyfin's Years filter is
                                     enumerative; we widen to all <X
                                     and trim client-side)
            play_count less_than X → drop items with PlayCount >= X
            rating less_than X     → drop items with CommunityRating >= X
            date_added  (any op)   → Jellyfin has no "added in last N
                                     days" filter; DateCreated comes
                                     back on every item and the rule
                                     refines client-side.
            last_played (any op)   → no server-side last-played filter;
                                     UserData.LastPlayedDate refines
                                     client-side.

        Multi-rule sets honor the top-level ``match``:

        * ``match="all"`` — every rule must pass. Server-side filters
          compose naturally (Jellyfin AND's its params); client-side
          refines are applied in series.
        * ``match="any"`` — each rule is evaluated independently and
          the union of items is returned. v1 caps this at 500 items
          per rule to keep the round-trips bounded.

        Sort: ``sort`` → ``SortBy=<mapped>``; ``sort_desc`` →
        ``SortOrder=Descending``. Unmapped sort fields fall back to
        ``SortName``.
        """
        from modules.providers.smart_rule_schema import validate_rules
        from modules.providers.smart_rule_eval import refine_items

        errors = validate_rules(rules)
        if errors:
            raise ValueError("invalid smart-playlist rule set: " + "; ".join(errors))

        raw_rules = rules.get("rules") or []
        if not raw_rules:
            return []

        match = rules.get("match", "all")
        limit = rules.get("limit")
        sort = rules.get("sort")
        sort_desc = bool(rules.get("sort_desc", False))

        if match == "any" and len(raw_rules) > 1:
            # OR semantics: fire one server query per rule (each
            # leg gets to push its own filter into /Items), union the
            # responses by Id. Each leg's items are guaranteed to
            # satisfy at least one rule from the OR by construction,
            # so we skip re-applying the predicate set and let
            # refine_items just enforce sort + limit (empty rule list).
            seen = set()
            merged: List[Dict[str, Any]] = []
            per_rule_cap = 500
            for rule in raw_rules:
                sub = self._query_jf_single(
                    rule,
                    limit=per_rule_cap,
                    sort=sort,
                    sort_desc=sort_desc,
                )
                for item in sub:
                    iid = item.get("Id")
                    if iid and iid not in seen:
                        seen.add(iid)
                        merged.append(item)
            sort_only = {
                "match": "all",
                "rules": [],
                "sort": sort,
                "sort_desc": sort_desc,
                "limit": limit,
            }
            return refine_items(merged, sort_only)

        # match == "all" (or single-rule): translate every rule into
        # one query so Jellyfin AND's them server-side; refine_items
        # then enforces the bits the server couldn't express. Skip
        # the satisfied rules in the Python refine pass — the server
        # already guarantees they hold for every returned item, and
        # re-applying them against the response would force the
        # response to carry every tag verbatim (which Jellyfin's
        # field-selection sometimes elides).
        params, satisfied = _build_jf_query(
            raw_rules,
            sort=sort,
            sort_desc=sort_desc,
        )
        needs_refine = len(satisfied) < len(raw_rules)

        try:
            if needs_refine:
                # A rule the server can't filter (date_added /
                # last_played, artist/album substring, …) means every
                # item must be refined in Python, so the whole
                # server-filtered set has to come back. Fetching it in
                # one unbounded request makes Jellyfin spend long
                # enough building the payload that the 15s HTTP timeout
                # trips on a non-trivial library — page it so each
                # request stays small and fast.
                #
                # When a date rule bounds the result to recent items
                # ("in the last N days" / "after D"), sort the fetch by
                # that date descending and stop paging the moment a
                # page falls before the cutoff: the rest of the library
                # can't match, so there's no point dragging it across.
                # refine_items re-sorts to the playlist's own sort
                # afterwards, so overriding SortBy here is invisible.
                bound = _recent_date_bound(raw_rules)
                if bound is not None:
                    _field, jf_key, cutoff = bound
                    params["SortBy"] = jf_key
                    params["SortOrder"] = "Descending"
                    items = self._fetch_paged(
                        params, stop_field=_field, stop_cutoff=cutoff
                    )
                else:
                    items = self._fetch_paged(params)
            else:
                # Every rule is satisfied server-side — Jellyfin can
                # cap the payload directly.
                if isinstance(limit, int) and limit > 0:
                    params["Limit"] = limit
                resp = self.api._get(
                    f"/Users/{self.api.user_id}/Items",
                    params,
                )
                items = resp.get("Items") or []
        except Exception:
            return []

        remaining = [r for r in raw_rules if r not in satisfied]
        refine_rules = dict(rules)
        refine_rules["rules"] = remaining
        return refine_items(items, refine_rules)

    def _fetch_paged(
        self,
        params: Dict[str, Any],
        *,
        page: int = 500,
        cap: int = 50000,
        stop_field: Optional[str] = None,
        stop_cutoff: Optional[Any] = None,
    ) -> List[Dict[str, Any]]:
        """Fetch ``/Items`` in ``StartIndex`` pages and concatenate.

        A single unbounded fetch of a large library makes Jellyfin take
        long enough to build the response that the 15s request timeout
        trips. Paging keeps each request small; ``cap`` bounds the
        worst case so a pathologically large library can't loop
        forever. The caller's ``SortBy`` is preserved across pages, so
        the concatenated order is stable.

        ``stop_field`` / ``stop_cutoff``: when set, the response is
        assumed sorted newest-first on ``stop_field``; paging stops as
        soon as a page's last item falls before ``stop_cutoff`` (every
        later item is older still, so it can't match a "recent" date
        rule)."""
        from modules.providers.smart_rule_eval import _field_value

        out: List[Dict[str, Any]] = []
        start = 0
        while start < cap:
            page_params = dict(params)
            page_params["Limit"] = page
            page_params["StartIndex"] = start
            resp = self.api._get(
                f"/Users/{self.api.user_id}/Items",
                page_params,
            )
            batch = resp.get("Items") or []
            out.extend(batch)
            if len(batch) < page:
                break
            if stop_field and stop_cutoff is not None and batch:
                tail = _field_value(batch[-1], stop_field)
                if tail is not None and tail < stop_cutoff:
                    break
            start += page
        return out

    def _query_jf_single(
        self, rule: Dict[str, Any], limit: int, sort: Optional[str], sort_desc: bool
    ) -> List[Dict[str, Any]]:
        """One-rule resolver used by the ``match=any`` union path.

        Fires a single ``/Items`` query for ``rule`` and returns the
        adapted item list without any Python refinement — the caller
        runs sort+limit over the merged union via ``refine_items``.
        """
        params, _satisfied = _build_jf_query(
            [rule],
            sort=sort,
            sort_desc=sort_desc,
        )
        params["Limit"] = limit
        try:
            resp = self.api._get(
                f"/Users/{self.api.user_id}/Items",
                params,
            )
        except Exception:
            return []
        return resp.get("Items") or []

    # ── Metadata editing ───────────────────────────────────────────────

    def can_edit_metadata_on_account(self) -> bool:
        """True only when the signed-in Jellyfin user is an admin —
        Jellyfin gates item-metadata edits on ``Policy.IsAdministrator``.
        The flag is captured from the authenticate / verify_session
        response Policy block (see ``JellyfinAPI.is_admin``)."""
        return bool(getattr(self.api, "is_admin", False))

    def update_track_metadata(self, item_id: str, edits: Dict[str, Any]) -> Dict[str, Any]:
        """Apply ``edits`` to ``item_id`` and return the merged item.
        Delegates to ``JellyfinAPI.update_item_metadata`` — that's where
        the GET-merge-POST-with-LockedFields workaround for
        jellyfin/jellyfin#10724 lives."""
        return self.api.update_item_metadata(item_id, edits)

    # Cover upload is a follow-up branch.

    # ── Cache control ──────────────────────────────────────────────────

    def invalidate_meta_cache(self, item_id: str = "") -> None:
        self.api.invalidate_meta_cache(item_id)
