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
