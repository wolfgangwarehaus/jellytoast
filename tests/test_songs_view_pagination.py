"""Pagination-cancel guard for SongsView (#790).

SongsView background-paginates large libraries: load_songs fetches page 1,
then a cascade of _load_next_page -> _on_page_loaded -> model.append_items
runs for up to FETCH_TIMEOUT_S. If the list is re-seeded mid-cascade — the
offline-mode chip flips, the sort changes, or a refresh re-cold-loads — a
page fetch that was already dispatched would resolve and append STALE rows
onto the freshly-rendered list (the literal "offline-toggle append"
symptom). _on_page_loaded now carries the load generation captured at
dispatch and drops the page if the list has since been re-seeded.
"""

from __future__ import annotations

from modules.songs_view import SongsView


def test_stale_page_dropped_after_reseed(qapp):
    sv = SongsView()
    sv._model.set_items([{"Id": "s1"}, {"Id": "s2"}])
    gen_at_dispatch = sv._load_gen
    # A re-seed (offline toggle / sort change / refresh) bumps the gen.
    sv._load_gen += 1
    # The stale in-flight page resolves carrying the OLD generation.
    sv._on_page_loaded({"Items": [{"Id": "s3"}, {"Id": "s4"}]}, gen_at_dispatch)
    # It must be dropped, not appended — the list still holds only the
    # re-seeded rows. (Pre-fix this grew to 4 rows.)
    assert [it.get("Id") for it in sv._model.items()] == ["s1", "s2"]


def test_current_gen_page_appends(qapp, monkeypatch):
    sv = SongsView()
    monkeypatch.setattr(sv, "_save_cache_async", lambda *a, **k: None)
    sv._model.set_items([{"Id": "s1"}])
    sv._refresh_scope = {"sort_by": "SortName"}
    # A page resolving with the CURRENT generation appends normally — the
    # guard must not over-reach and drop live pagination.
    sv._on_page_loaded({"Items": [{"Id": "s2"}]}, sv._load_gen)
    assert [it.get("Id") for it in sv._model.items()] == ["s1", "s2"]


def test_no_gen_arg_still_appends(qapp, monkeypatch):
    # Back-compat: a call with no generation (gen=None) is never treated as
    # stale, so any direct/legacy invocation keeps working.
    sv = SongsView()
    monkeypatch.setattr(sv, "_save_cache_async", lambda *a, **k: None)
    sv._model.set_items([{"Id": "s1"}])
    sv._on_page_loaded({"Items": [{"Id": "s2"}]})
    assert [it.get("Id") for it in sv._model.items()] == ["s1", "s2"]


def test_load_songs_bumps_generation(qapp, monkeypatch):
    # Both the offline short-circuit and the server path must bump the gen
    # so a prior cascade's in-flight fetch is invalidated on every reload.
    from modules import offline as _offline

    sv = SongsView()
    monkeypatch.setattr(_offline, "is_offline_mode", lambda: True)
    monkeypatch.setattr(_offline, "list_complete_items", lambda *a, **k: [])
    before = sv._load_gen
    sv.load_songs("")
    assert sv._load_gen > before


# ── Cross-page dedup guard (#10) ───────────────────────────────────────────
# A Subsonic random-songs feed that ignored the offset used to re-roll an
# overlapping batch each page → duplicate rows + endless pagination.
# _on_page_loaded now drops rows already shown and stops if a page is all
# duplicates, independent of the provider fix, while leaving deterministic
# (non-overlapping) pagination untouched.


def test_overlapping_page_dedupes_appended_rows(qapp, monkeypatch):
    sv = SongsView()
    monkeypatch.setattr(sv, "_save_cache_async", lambda *a, **k: None)
    monkeypatch.setattr(sv, "_load_next_page", lambda: None)
    sv._model.set_items([{"Id": "s1"}, {"Id": "s2"}])
    sv._refresh_scope = {"sort_by": "SortName"}
    # The page re-includes s2 (overlap) plus a genuinely new s3.
    sv._on_page_loaded({"Items": [{"Id": "s2"}, {"Id": "s3"}]}, sv._load_gen)
    assert [it.get("Id") for it in sv._model.items()] == ["s1", "s2", "s3"]


def test_all_duplicate_page_stops_cascade(qapp, monkeypatch):
    sv = SongsView()
    monkeypatch.setattr(sv, "_save_cache_async", lambda *a, **k: None)
    sv._model.set_items([{"Id": "s1"}, {"Id": "s2"}])
    sv._refresh_scope = {"sort_by": "SortName"}
    # An entirely-overlapping page must append nothing AND end pagination,
    # rather than spin re-fetching the same rows forever.
    sv._on_page_loaded({"Items": [{"Id": "s1"}, {"Id": "s2"}]}, sv._load_gen)
    assert [it.get("Id") for it in sv._model.items()] == ["s1", "s2"]
    assert sv._tail_reached is True


def test_tail_measured_on_raw_count_not_after_dedup(qapp, monkeypatch):
    sv = SongsView()
    sv.PAGE_SIZE = 3  # shrink so a "full" page is cheap to build
    monkeypatch.setattr(sv, "_save_cache_async", lambda *a, **k: None)
    monkeypatch.setattr(sv, "_load_next_page", lambda: None)
    sv._model.set_items([{"Id": "s1"}])
    sv._refresh_scope = {"sort_by": "SortName"}
    # A FULL raw page (3 == PAGE_SIZE) with one incidental overlap. Post-dedup
    # it's only 2 new rows, but the tail must be judged on the raw count so
    # background pagination keeps going (Jellyfin parity).
    sv._on_page_loaded(
        {"Items": [{"Id": "s1"}, {"Id": "s2"}, {"Id": "s3"}]}, sv._load_gen
    )
    assert [it.get("Id") for it in sv._model.items()] == ["s1", "s2", "s3"]
    assert sv._tail_reached is False


# ── Cold render + refresh paths also honour the generation (#6 audit) ───────
# The #790 fix gen-guarded only the background pages (_on_page_loaded); the
# cold page-1 render (_on_cold_fetch → _on_items_loaded) and the page-1
# refresh (_on_refresh_loaded) were not. So a cold fetch resolving AFTER a
# sort change could overwrite the current-sort list with stale rows, and the
# cascade it kicked would paginate the current sort onto that wrong head.


def test_on_items_loaded_drops_superseded_render(qapp):
    sv = SongsView()
    sv._model.set_items([{"Id": "current1"}])
    stale = sv._load_gen
    sv._load_gen += 1  # a newer load_songs() superseded the in-flight render
    sv._on_items_loaded({"Items": [{"Id": "x1"}, {"Id": "x2"}], "_load_gen": stale})
    # The stale render must NOT overwrite the current list.
    assert [it.get("Id") for it in sv._model.items()] == ["current1"]


def test_on_items_loaded_renders_current_gen(qapp):
    sv = SongsView()
    sv._on_items_loaded({"Items": [{"Id": "a"}, {"Id": "b"}], "_load_gen": sv._load_gen})
    assert {it.get("Id") for it in sv._model.items()} == {"a", "b"}


def test_on_items_loaded_offline_path_renders_without_gen(qapp):
    # The synchronous offline render stamps no gen → must still render.
    sv = SongsView()
    sv._on_items_loaded({"Items": [{"Id": "off1"}]})
    assert [it.get("Id") for it in sv._model.items()] == ["off1"]


def test_on_cold_fetch_drops_superseded(qapp, monkeypatch):
    sv = SongsView()
    monkeypatch.setattr(sv, "_save_cache_async", lambda *a, **k: None)
    sv._model.set_items([{"Id": "current1"}])
    stale = sv._load_gen
    sv._load_gen += 1
    sv._on_cold_fetch({"Items": [{"Id": "stale1"}]}, gen=stale)
    assert [it.get("Id") for it in sv._model.items()] == ["current1"]


def test_on_refresh_loaded_drops_superseded(qapp):
    sv = SongsView()
    calls = []
    sv.load_songs = lambda *a, **k: calls.append(True)
    sv._model.set_items([{"Id": "x"}])
    stale = sv._load_gen
    sv._load_gen += 1
    # A stale refresh whose page-1 differs would normally _clear()+reload;
    # the gen guard must drop it so it can't trample the current scope.
    sv._on_refresh_loaded({"Items": [{"Id": "different"}], "_load_gen": stale})
    assert calls == []


# ── Cold load must not self-supersede (live find, 2026-06-02) ──────────────
# load_songs captured the load generation, then its OWN _clear() (cold path)
# bumped _load_gen — so the cold page-1 fetch was dispatched already-stale and
# _on_cold_fetch dropped its own result on the gen guard. The view stayed
# blank ("No songs yet") with a fully-populated library, _page_fetch_in_flight
# stuck True, and the disk cache never written (so it never self-healed).
# Reproduced on Navidrome with the live test bridge; fixed by re-syncing the
# generation AFTER _clear() in the cold path.


def test_cold_load_renders_page_one(qapp, monkeypatch):
    from modules import disk_cache
    from modules import offline as _offline

    sv = SongsView()
    monkeypatch.setattr(_offline, "is_offline_mode", lambda: False)
    monkeypatch.setattr(disk_cache, "load", lambda *a, **k: None)  # force cold path
    monkeypatch.setattr(sv, "_save_cache_async", lambda *a, **k: None)
    monkeypatch.setattr(sv, "_load_next_page", lambda: None)  # no cascade

    page = [{"Id": "s1"}, {"Id": "s2"}, {"Id": "s3"}]

    # Deliver run_async results synchronously, exactly as the GUI thread
    # would (minus the worker hop): fn(*args) → on_result.
    def fake_run_async(fn, *args, on_result=None, on_error=None, **kw):
        try:
            res = fn(*args)
        except Exception as e:  # pragma: no cover - defensive
            if on_error:
                on_error(e)
            return
        if on_result:
            on_result(res)

    monkeypatch.setattr("modules.songs_view.run_async", fake_run_async)

    class _FakeApi:
        def get_items(self, *a, **k):
            return {"Items": page, "TotalRecordCount": len(page)}

    sv.api = _FakeApi()
    sv.load_songs("")  # single, uninterrupted cold load

    assert [it.get("Id") for it in sv._model.items()] == ["s1", "s2", "s3"]
    assert sv._page_fetch_in_flight is False
    assert sv._initial_load_complete is True
