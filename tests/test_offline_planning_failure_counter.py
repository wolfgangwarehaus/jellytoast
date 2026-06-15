"""A planning (cascade-root) download failure must NOT bump the session
failure counter.

Bug-hunt regression: ``_plan_err`` called ``_record_failure`` which bumped
``_session_failed``, but a planning failure dispatches no track and never
reaches a drain edge to clear it — so the stale +1 leaked into a *later*
clean batch's drain notice ("Downloads failed" after a successful run).
Track-download failures (in ``_finish``) must still count.
"""

import pytest

from jellytoast.offline import manager as _mgr


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    _mgr._reset_for_tests()
    # Stub the index DB calls _record_failure makes so we exercise only
    # the session-counter logic, not the SQLite layer.
    import jellytoast.offline.index as index

    monkeypatch.setattr(index, "get_node", lambda _i: None)
    monkeypatch.setattr(index, "record_failure", lambda *a, **k: None)
    monkeypatch.setattr(index, "set_state", lambda *a, **k: None)
    yield
    _mgr._reset_for_tests()


def test_track_failure_bumps_session_counter():
    assert _mgr._session_failed == 0
    _mgr._record_failure("track-1")  # default bump_session=True
    assert _mgr._session_failed == 1


def test_planning_failure_does_not_bump_session_counter():
    assert _mgr._session_failed == 0
    _mgr._record_failure("album-root", bump_session=False)
    assert _mgr._session_failed == 0


def test_planning_failure_then_track_failure_counts_only_the_track():
    _mgr._record_failure("album-root", bump_session=False)
    _mgr._record_failure("track-1")
    assert _mgr._session_failed == 1
