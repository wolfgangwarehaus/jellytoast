"""Multi-library selection — which Navidrome/Jellyfin music libraries are
loaded into jellytoast right now.

Navidrome (and Jellyfin) can expose several music libraries on one server.
A user who partitions, say, a curated "Music" library from a churning
"Discover" download dump wants to browse them independently *or* together.
This module owns that selection and the logic to turn it into provider
fetches.

Design (all browse surfaces, provider-agnostic — see the session memory):

* The **available** libraries come from ``provider.get_libraries()`` —
  Subsonic's ``getMusicFolders``, Jellyfin's music ``Views``.
* The **selection** is a set of library ids persisted in
  ``Settings.selected_library_ids``. **Empty = all** (the pre-feature
  default: no filter, the server returns everything).
* A selection is resolved to a **fetch plan** — a list of ``parent_id``
  values to query and merge:
    - empty selection, OR every available library selected
      → ``[""]`` (one unfiltered query — the server already returns the
        union, globally sorted; no client-side merge, no pagination risk).
    - exactly one library selected
      → ``[that_id]`` (one ``musicFolderId`` query — today's single-folder
        path, already battle-tested).
    - a *partial* subset of 2+ of 3+ available
      → ``[id, id, …]`` (fetch each, merge + dedupe + re-sort client-side).
  august's setup has 2 libraries, so he only ever hits the first two
  (cheap, single-query) cases; the merge path is for 3+-library servers.

The selection state is a process-global (mirrors the provider singleton
and the connectivity module) reset on sign-out / server change so a stale
selection can't leak across servers.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

# ── Module state ─────────────────────────────────────────────────────────
# Available libraries for the active server, cached after the first
# successful provider.get_libraries() so the dropdown + every resolve
# don't re-hit the network. ``None`` = not yet fetched this session.
_available: Optional[List[Dict[str, Any]]] = None


def reset_after_server_change() -> None:
    """Drop the cached available-libraries list AND the persisted
    selection so the next server starts from 'all libraries'. Called on
    sign-out and on the sign-in completion path (alongside
    ``providers.reset_provider`` / ``offline.reset_after_server_change``)
    so a selection made against server A can never scope server B."""
    global _available
    _available = None
    try:
        from modules.settings import get_settings

        get_settings().selected_library_ids = []
    except Exception as e:  # pragma: no cover - settings always present in app
        logger.warning("couldn't clear selected_library_ids: %s", e)


def set_available_libraries(libs: List[Dict[str, Any]]) -> None:
    """Cache the available music libraries (called by the host once after
    sign-in, with ``provider.get_libraries()`` filtered to music). Stored
    as a list of ``{"Id", "Name"}`` dicts in server order."""
    global _available
    cleaned: List[Dict[str, Any]] = []
    seen: set = set()
    for lib in libs or []:
        lid = str(lib.get("Id") or "").strip()
        if not lid or lid in seen:
            continue
        seen.add(lid)
        cleaned.append({"Id": lid, "Name": str(lib.get("Name") or lid)})
    _available = cleaned


def available_libraries() -> List[Dict[str, Any]]:
    """The cached available music libraries (``[]`` if not yet fetched).
    Order is server order, which is the order the dropdown renders."""
    return list(_available or [])


def has_multiple_libraries() -> bool:
    """True when the server exposes 2+ music libraries — the gate for
    showing the top-bar dropdown at all. A single-library server (the
    common case) never sees the new affordance."""
    return len(_available or []) >= 2


def _available_ids() -> List[str]:
    return [lib["Id"] for lib in (_available or [])]


def selected_ids() -> List[str]:
    """The user's selected library ids, filtered to ids that still exist
    on the current server. An empty result means 'all' — either the user
    picked nothing/everything, or their stored ids are all stale (a
    library was removed server-side); both degrade safely to the whole
    collection rather than an empty grid."""
    from modules.settings import get_settings

    stored = get_settings().selected_library_ids
    if not stored:
        return []
    avail = set(_available_ids())
    if not avail:
        # We don't know the server's libraries yet — trust the stored
        # ids as-is (they were valid when written; the resolve layer
        # still treats unknown ids conservatively).
        return list(stored)
    valid = [sid for sid in stored if sid in avail]
    return valid


def set_selected_ids(ids: List[str]) -> bool:
    """Persist a new selection (filtered to known libraries, de-duped,
    order-preserving). Returns True if the *effective* selection changed
    so the caller can skip emitting ``libraries_changed`` on a no-op.

    'Effective' compares the normalized selections: selecting every
    library is equivalent to selecting none (both mean 'all'), so toggling
    between those two does not churn the grids."""
    from modules.settings import get_settings

    avail = _available_ids()
    avail_set = set(avail)
    # Normalize the incoming ids: keep only known libraries, de-dupe,
    # preserve order.
    norm: List[str] = []
    seen: set = set()
    for sid in ids or []:
        s = str(sid or "").strip()
        if s and s in avail_set and s not in seen:
            seen.add(s)
            norm.append(s)
    # 'All selected' collapses to 'none' (the canonical 'all' form) so the
    # stored value and the fetch plan stay in the cheap single-query case.
    if avail and len(norm) == len(avail):
        norm = []
    before = _effective_key(selected_ids())
    get_settings().selected_library_ids = norm
    after = _effective_key(norm)
    return before != after


def _effective_key(ids: List[str]) -> frozenset:
    """Order-independent identity of an *effective* selection. Empty and
    'all libraries' both map to the same key so they compare equal."""
    avail = _available_ids()
    if not ids or (avail and set(ids) >= set(avail)):
        return frozenset()  # 'all'
    return frozenset(ids)


def is_filtered() -> bool:
    """True when a non-trivial subset is active (i.e. NOT 'all'). Drives
    whether the title shows library names + whether the cache scope needs
    the selection key."""
    return bool(_effective_key(selected_ids()))


def selection_cache_key() -> str:
    """A stable string identifying the active selection for the disk-cache
    scope, so different selections cache to different files and switching
    back doesn't re-fetch. ``""`` for the 'all' case keeps the existing
    cache files valid (no scope change when the feature is unused)."""
    key = _effective_key(selected_ids())
    if not key:
        return ""
    return ",".join(sorted(key))


def selection_title(default: str = "Music") -> str:
    """The top-left title for the active selection.

    * 'all' (or single-library server) → ``default`` ("Music").
    * one library → that library's name ("Discover").
    * two → "A + B" ("Music + Discover").
    * 3+ → "A +N" ("Music +2") to stay compact.
    """
    ids = selected_ids()
    if not _effective_key(ids):
        return default
    by_id = {lib["Id"]: lib["Name"] for lib in (_available or [])}
    names = [by_id.get(sid, sid) for sid in ids]
    if not names:
        return default
    if len(names) == 1:
        return names[0]
    if len(names) == 2:
        return f"{names[0]} + {names[1]}"
    return f"{names[0]} +{len(names) - 1}"


def music_libraries(libs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Filter a raw ``provider.get_libraries()`` list down to music
    libraries only. Subsonic fakes ``CollectionType="music"`` for every
    folder; Jellyfin tags views with their real collection type, so this
    drops Movies/TV/Books views. Entries with no CollectionType are kept
    (defensive — a backend that omits it is assumed music in this
    music-only app)."""
    out: List[Dict[str, Any]] = []
    for lib in libs or []:
        ct = (lib.get("CollectionType") or "music").lower()
        if ct == "music":
            out.append(lib)
    return out


def all_libraries_parent_id(provider: Any) -> str:
    """The ``parent_id`` that means "all music" for ``provider``.

    On Subsonic an empty parent already unions every music folder, so
    ``""`` is correct and cheapest. On Jellyfin an empty parent would pull
    non-music items, so "all music" must scope to the (single) music
    view's id. Reads the provider's ``scopes_music_by_library`` capability
    rather than branching on kind, keeping the call site provider-
    agnostic. Returns ``""`` if no music library is known yet."""
    if not getattr(provider, "scopes_music_by_library", True):
        return ""
    libs = _available or []
    return libs[0]["Id"] if libs else ""


def fetch_plan(provider: Any = None) -> List[str]:
    """Resolve the active selection to the list of ``parent_id`` values a
    browse surface should query and merge.

    * ``[<all-id>]`` — one query for "all libraries". ``<all-id>`` is
      ``""`` on Subsonic (empty parent = whole server) and the music
      view id on Jellyfin (so non-music isn't pulled in). When
      ``provider`` is None the legacy empty-parent is used (callers that
      already resolve their own music id, e.g. via ``_resolve_library_id``,
      pass None and substitute it themselves).
    * ``[id]`` — one folder-scoped query (single selection).
    * ``[id, id, …]`` — fetch each + merge client-side (partial subset).
    """
    key = _effective_key(selected_ids())
    if not key:
        return [all_libraries_parent_id(provider) if provider is not None else ""]
    return list(selected_ids())


# ── Merge helper for the multi-folder (3+) partial-subset case ─────────────


def merge_paged(
    fetch: Callable[[str, int, int], List[Dict[str, Any]]],
    parent_ids: List[str],
    *,
    start_index: int,
    limit: int,
    sort_key: Callable[[Dict[str, Any]], Any],
    reverse: bool = False,
    id_key: str = "Id",
) -> List[Dict[str, Any]]:
    """Merge a page across several library folders, preserving a global
    sort and correct pagination across the union.

    The problem: each folder paginates independently, but the UI wants one
    globally-sorted, gap-free, dupe-free stream. We can't just concatenate
    per-folder pages (the boundaries wouldn't interleave by sort key) or
    request ``start_index`` from each folder (that skips the wrong rows).

    The approach for a bounded number of folders: over-fetch a prefix from
    each folder large enough to cover the requested window
    (``start_index + limit`` rows globally can't require more than that
    many rows from any single folder), merge, sort, dedupe by id, then
    slice the requested window. This is O((start+limit) · folders) per
    page — fine for a handful of folders and the human-scale windows the
    grid asks for, and it keeps the result identical to what a single
    globally-sorted query would return.

    ``fetch(parent_id, offset, count)`` returns adapted item dicts for that
    folder's page. ``sort_key`` / ``reverse`` define the global order
    (the same article-stripped key the grid re-sorts by). Returns the
    requested window; an empty return signals the tail (no more rows),
    which the grid's ``len < PAGE_SIZE`` stop already understands.
    """
    if not parent_ids:
        return []
    if len(parent_ids) == 1:
        # Degenerate: a single folder needs no merge — page it directly.
        return fetch(parent_ids[0], start_index, limit)

    need = start_index + limit
    pooled: List[Dict[str, Any]] = []
    seen: set = set()
    for pid in parent_ids:
        # Pull a prefix of `need` rows from this folder. Any row in the
        # global window [start, start+limit) ranks within the first
        # `need` rows of its own folder too, so this prefix is sufficient.
        rows = fetch(pid, 0, need)
        for it in rows:
            iid = str(it.get(id_key) or "")
            if iid and iid in seen:
                continue
            if iid:
                seen.add(iid)
            pooled.append(it)
    pooled.sort(key=sort_key, reverse=reverse)
    return pooled[start_index : start_index + limit]
