"""The library as the downloads see it — what the Qt Quick models show
when offline mode is on (by choice, or automatically when the server is
unreachable). Same sources and fallbacks as the Widgets offline views
(``library_paginator._render_offline_items``, ``artist_page``,
``search_view``)."""

from __future__ import annotations

from typing import Dict, List


def active() -> bool:
    try:
        from jellytoast import offline

        return bool(offline.is_offline_mode())
    except Exception:
        return False


def _complete(kind: str) -> List[Dict]:
    from jellytoast import offline

    return [it for it in (offline.list_complete_items(kind) or []) if it.get("Id")]


def _by_name(items: List[Dict]) -> List[Dict]:
    return sorted(items, key=lambda it: (it.get("SortName") or it.get("Name") or "").lower())


def _artists() -> List[Dict]:
    # Real artist nodes + every downloaded album's AlbumArtists (downloading
    # an album alone never creates an artist node).
    by_id = {a["Id"]: a for a in _complete("artist")}
    for album in _complete("album"):
        for entry in album.get("AlbumArtists") or []:
            if isinstance(entry, dict) and entry.get("Id") and entry["Id"] not in by_id:
                by_id[entry["Id"]] = {
                    "Id": entry["Id"],
                    "Name": entry.get("Name") or "",
                    "Type": "MusicArtist",
                }
    return list(by_id.values())


def _fav(it: Dict) -> bool:
    return bool((it.get("UserData") or {}).get("IsFavorite"))


def items(kind: str, parent_id: str = "") -> List[Dict]:
    """One library section from the downloads, A–Z (artist pages newest
    first). ``kind`` as ``LibraryModel.kind``."""
    if kind == "album":
        return _by_name(_complete("album"))
    if kind == "artist":
        return _by_name(_artists())
    if kind == "playlist":
        return _by_name(_complete("playlist"))
    if kind == "song":
        return _by_name(_complete("track"))
    if kind == "favAlbums":
        return _by_name([a for a in _complete("album") if _fav(a)])
    if kind == "favSongs":
        return _by_name([t for t in _complete("track") if _fav(t)])
    if kind == "genre":
        names = set()
        for a in _complete("album"):
            for g in a.get("Genres") or []:
                if isinstance(g, str) and g:
                    names.add(g)
        return [{"Id": g, "Name": g, "Type": "MusicGenre"} for g in sorted(names, key=str.lower)]
    if kind == "genreAlbums":
        return _by_name([a for a in _complete("album") if parent_id in (a.get("Genres") or [])])
    if kind == "artistAlbums":
        from jellytoast.artist_page import _resolve_offline_artist

        _meta, albums = _resolve_offline_artist(parent_id)
        return sorted(albums, key=lambda a: -(int(a.get("ProductionYear") or 0)))
    return []


def tracks(container_id: str, kind: str = "album") -> List[Dict]:
    """A downloaded album's / playlist's tracks (album: disc + track order;
    playlist: the order they were saved in)."""
    from jellytoast import offline

    found = [t for t in (offline.child_snapshots(container_id, kind="track") or []) if t.get("Id")]
    if kind == "album":
        found.sort(
            key=lambda t: (int(t.get("ParentIndexNumber") or 1), int(t.get("IndexNumber") or 0))
        )
    return found


def search(
    term: str, songs: int = 20, albums: int = 14, artists: int = 10
) -> Dict[str, List[Dict]]:
    """``search_all``'s shape, over the downloads."""
    q = (term or "").strip().lower()

    def hay(*parts) -> str:
        out = []
        for p in parts:
            if isinstance(p, str):
                out.append(p)
            elif isinstance(p, list):
                out.extend(
                    x if isinstance(x, str) else (x or {}).get("Name", "")
                    for x in p
                    if isinstance(x, (str, dict))
                )
        return " ".join(out).lower()

    return {
        "MusicArtist": [a for a in _artists() if q in hay(a.get("Name"))][:artists],
        "MusicAlbum": [
            a
            for a in _complete("album")
            if q in hay(a.get("Name"), a.get("AlbumArtist"), a.get("AlbumArtists"))
        ][:albums],
        "Audio": [
            t
            for t in _complete("track")
            if q in hay(t.get("Name"), t.get("Album"), t.get("AlbumArtist"), t.get("Artists"))
        ][:songs],
    }
