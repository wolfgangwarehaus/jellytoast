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
  A typical setup has 1-2 libraries and only ever hits the first two
  (cheap, single-query) cases; the merge path is for 3+-library servers.

.. note::
   Phase 2 (2026-07-05): the plan IS the production browse path now.
   The grids, Songs view, and Suggestions rails resolve scope through
   ``library_selection_controller._music_fetch_plan()`` → :func:`fetch_plan`,
   and a 2+-folder plan fetches every folder and merges client-side via
   :func:`fetch_union` (grids/songs) or a per-rail bounded merge
   (suggestions). The old degrade-to-'all' toast is gone. The windowed
   ``merge_paged`` helper this module used to carry was dropped in the
   same change: every browse surface background-loads its *entire* scope
   anyway (auto-paginate / silent fill), so a windowed union re-fetching
   ``start+limit`` rows per folder per page was strictly worse than
   draining each folder once.

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
        from jellytoast.settings import get_settings

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
    from jellytoast.settings import get_settings

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
    from jellytoast.settings import get_settings

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
    """True when a non-trivial subset is active (i.e. NOT 'all'). Intended
    to drive whether the title shows library names + whether the cache
    scope needs the selection key.

    Unwired Phase-2 scaffolding — NOT on the browse path (tests only); see
    the module docstring."""
    return bool(_effective_key(selected_ids()))


def selection_cache_key() -> str:
    """A stable string identifying the active selection for the disk-cache
    scope, so different selections cache to different files and switching
    back doesn't re-fetch. ``""`` for the 'all' case keeps the existing
    cache files valid (no scope change when the feature is unused).

    Unwired Phase-2 scaffolding — NOT on the browse path (tests only); see
    the module docstring."""
    key = _effective_key(selected_ids())
    if not key:
        return ""
    return ",".join(sorted(key))


def selection_title_forms(default: str = "Music") -> List[str]:
    """Top-left title candidates for the active selection, MOST → LEAST
    informative. The top bar picks the widest form that fits before the
    centred view dropdown, so a long multi-library title
    ("Music Library + Discovery") degrades to "Music Library +1" and then
    "2 libraries" instead of overrunning the "Albums" dropdown.

    * 'all' / single-library server → ``[default]`` ("Music").
    * one library → ``[name]`` (a single name can't sensibly shorten).
    * two → ``["A + B", "A +1", "2 libraries"]``.
    * 3+ → ``["A +N", "N libraries"]`` (already compact; just the fallback).
    """
    if not _effective_key(selected_ids()):
        return [default]
    ids = set(selected_ids())
    # Render names in SERVER order (the order get_libraries returned, which
    # the dropdown also shows), not click order — so the primary library
    # ("Music", the first folder) always leads when it's part of the selection.
    names = [lib["Name"] for lib in (_available or []) if lib["Id"] in ids]
    if not names:
        return [default]
    if len(names) == 1:
        return [names[0]]
    n = len(names)
    forms: List[str] = []
    if n == 2:
        forms.append(f"{names[0]} + {names[1]}")
    forms.append(f"{names[0]} +{n - 1}")
    forms.append(f"{n} libraries")
    # Dedupe, preserving order (n==2 never collides; belt-and-braces).
    seen: set = set()
    return [f for f in forms if not (f in seen or seen.add(f))]


def selection_title(default: str = "Music") -> str:
    """The single best (widest) title form — back-compat + the plain-label
    path where width never matters. See :func:`selection_title_forms`."""
    return selection_title_forms(default)[0]


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
    agnostic. Returns ``""`` if no music library is known yet.

    Only meaningful for the 0-or-1 music-library case — a *multi*-view
    Jellyfin server has no single union parent, which is why
    :func:`fetch_plan` returns every view id there instead of this."""
    if not getattr(provider, "scopes_music_by_library", True):
        return ""
    libs = _available or []
    return libs[0]["Id"] if libs else ""


def fetch_plan(provider: Any = None) -> List[str]:
    """Resolve the active selection to the list of ``parent_id`` values a
    browse surface should query and merge. THE production scope resolver
    (via ``library_selection_controller._music_fetch_plan()``).

    * ``[<all-id>]`` — one query for "all libraries". ``<all-id>`` is
      ``""`` on Subsonic (empty parent = whole server already unions
      every folder) and the music view id on a single-music-view
      Jellyfin server (so non-music isn't pulled in). When ``provider``
      is None the legacy empty-parent is used (callers that already
      resolve their own music id, e.g. via ``_resolve_library_id``,
      pass None and substitute it themselves).
    * ``[id, id, …]`` for 'all' on a **multi**-music-view Jellyfin
      server — there is no single parent that unions the views, so the
      plan lists every view and the surfaces merge (this was the
      documented Phase-1 gap where 'all' silently showed only the
      first view).
    * ``[id]`` — one folder-scoped query (single selection).
    * ``[id, id, …]`` — fetch each + merge client-side (partial subset).
    """
    key = _effective_key(selected_ids())
    if not key:
        if provider is None:
            return [""]
        if getattr(provider, "scopes_music_by_library", True):
            # Jellyfin-style: 'all' must not leak non-music items, so it
            # scopes to the music view(s). 2+ views have no union parent
            # → plan them all and let the surface merge.
            libs = _available or []
            if len(libs) >= 2:
                return [lib["Id"] for lib in libs]
        return [all_libraries_parent_id(provider)]
    return list(selected_ids())


# ── Union fetch for the multi-folder plan ──────────────────────────────────


def union_sort_key(sort_by: str) -> Callable[[Dict[str, Any]], Any]:
    """A client-side sort key matching the server sort ``sort_by`` would
    have produced, for re-sorting a merged multi-folder union.

    ``sort_by`` is the grids' comma-composite server sort string (e.g.
    ``"AlbumArtist,SortName"``). Name-ish fields use the same
    article-stripped collation the grids' own client resort uses, so a
    merged list is indistinguishable from a single globally-sorted
    query. Numeric fields coerce to float, date fields compare as ISO
    strings (both with missing-value defaults so a sparse field can't
    raise on None < str). Unknown fields fall back to SortName so the
    result is always *some* stable global order — never a folder-
    concatenation artifact. ``Random`` deliberately maps to SortName
    too: callers that want a shuffled union shuffle after the merge.
    """
    from jellytoast.sort_utils import article_stripped_key

    def _name(it: Dict[str, Any]) -> str:
        return article_stripped_key(str(it.get("SortName") or it.get("Name") or ""))

    def _field_key(field: str) -> Callable[[Dict[str, Any]], Any]:
        if field == "AlbumArtist":

            def k(it):
                v = it.get("AlbumArtist", "") or ""
                if isinstance(v, list):
                    v = v[0] if v else ""
                return article_stripped_key(str(v))

            return k
        if field == "Name":
            return lambda it: article_stripped_key(str(it.get("Name") or ""))
        if field == "Album":
            return lambda it: article_stripped_key(str(it.get("Album") or ""))
        if field in ("PremiereDate", "DateCreated", "DatePlayed"):
            user_key = {"DatePlayed": "LastPlayedDate"}.get(field)

            def k2(it, f=field, uk=user_key):
                v = it.get(f)
                if v is None and uk:
                    v = (it.get("UserData") or {}).get(uk)
                if v is None and f == "PremiereDate":
                    y = it.get("ProductionYear")
                    v = f"{y:04d}" if isinstance(y, int) else ""
                return str(v or "")

            return k2
        if field == "ProductionYear":
            return lambda it: float(it.get("ProductionYear") or 0)
        if field == "PlayCount":
            return lambda it: float((it.get("UserData") or {}).get("PlayCount") or 0)
        if field == "RunTimeTicks":
            return lambda it: float(it.get("RunTimeTicks") or 0)
        # SortName, Random, and anything unrecognised.
        return _name

    fields = [f for f in (sort_by or "").split(",") if f] or ["SortName"]
    keys = [_field_key(f) for f in fields]
    if len(keys) == 1:
        return keys[0]
    return lambda it: tuple(k(it) for k in keys)


def fetch_union(
    fetch: Callable[[str, int, int], List[Dict[str, Any]]],
    parent_ids: List[str],
    *,
    sort_key: Callable[[Dict[str, Any]], Any],
    reverse: bool = False,
    id_key: str = "Id",
    page_size: int = 500,
) -> List[Dict[str, Any]]:
    """Fetch EVERY row of every folder in ``parent_ids``, merge, dedupe
    by id, and return the union globally sorted.

    Runs synchronously — call it from an ``async_io.run_async`` worker,
    never the GUI thread. ``fetch(parent_id, offset, count)`` returns
    one adapted-item page for that folder; each folder is drained with
    sequential ``page_size`` pages until a short page signals its tail
    (the same tail-stop every paginating surface uses).

    Drain-everything is deliberate, not lazy: every browse surface
    already background-loads its entire scope (grid auto-paginate,
    songs silent fill), so a windowed merge that re-fetched
    ``start+limit`` rows per folder on every page would do strictly
    more I/O for the same end state. The result renders as one
    complete, cache-ready list.
    """
    merged: List[Dict[str, Any]] = []
    seen: set = set()
    for pid in parent_ids:
        offset = 0
        while True:
            page = fetch(pid, offset, page_size)
            for it in page:
                iid = str(it.get(id_key) or "")
                if iid and iid in seen:
                    continue
                if iid:
                    seen.add(iid)
                merged.append(it)
            if len(page) < page_size:
                break
            offset += page_size
    merged.sort(key=sort_key, reverse=reverse)
    return merged
