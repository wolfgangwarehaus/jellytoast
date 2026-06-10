"""Tests for jellytoast.scrobble.queue.remove — identity vs count removal.

The identity path (``records=``) is the #437 data-loss fix: count-based
removal dropped the oldest N, so two overlapping flushes (each scanning +
sending the same prefix, then each calling remove(N)) ate N fresh entries
queued in between. Identity removal only ever drops what was actually sent.
"""

from jellytoast.scrobble import queue as q


def _store(monkeypatch, items):
    saved = []
    monkeypatch.setattr(q, "_load_raw", lambda: list(items))
    monkeypatch.setattr(q, "_save_raw", lambda kept: saved.append(kept))
    return saved


def test_identity_removes_exact_records(monkeypatch):
    items = [
        {"service": "lb", "x": 1},
        {"service": "lb", "x": 2},
        {"service": "lb", "x": 3},
    ]
    saved = _store(monkeypatch, items)
    q.remove("lb", records=[{"service": "lb", "x": 1}, {"service": "lb", "x": 3}])
    assert saved == [[{"service": "lb", "x": 2}]]


def test_identity_second_call_is_noop_preserving_fresh_entries(monkeypatch):
    # Data-loss scenario: a duplicate flush re-calls remove with the
    # already-sent records while only a FRESH, never-sent entry (x=9)
    # remains. Identity removal matches nothing → no write → x=9 survives.
    # (Count-based remove(2) would have eaten it.)
    saved = _store(monkeypatch, [{"service": "lb", "x": 9}])
    q.remove("lb", records=[{"service": "lb", "x": 1}, {"service": "lb", "x": 2}])
    assert saved == []


def test_identity_only_touches_matching_service(monkeypatch):
    items = [{"service": "lb", "x": 1}, {"service": "lastfm", "x": 1}]
    saved = _store(monkeypatch, items)
    q.remove("lb", records=[{"service": "lb", "x": 1}])
    assert saved == [[{"service": "lastfm", "x": 1}]]


def test_count_mode_still_works(monkeypatch):
    items = [{"service": "lb", "x": 1}, {"service": "lb", "x": 2}, {"service": "lb", "x": 3}]
    saved = _store(monkeypatch, items)
    q.remove("lb", 2)  # legacy count mode: drop oldest 2
    assert saved == [[{"service": "lb", "x": 3}]]


def test_empty_records_is_noop(monkeypatch):
    saved = _store(monkeypatch, [{"service": "lb", "x": 1}])
    q.remove("lb", records=[])
    assert saved == []
