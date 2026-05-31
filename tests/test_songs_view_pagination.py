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
