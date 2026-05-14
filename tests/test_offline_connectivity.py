"""Tests for offline connectivity state (``modules/offline/connectivity.py``).

Only the offline-mode flag is functional today (Phase 1); reachability
probing and ``note_outcome`` transitions are Phase 5. These tests cover
what's real and pin ``note_outcome`` as an explicit not-yet so Phase 5
can't land it unnoticed.
"""

from __future__ import annotations

import pytest

from modules.offline import connectivity as _conn


@pytest.fixture(autouse=True)
def _reset_connectivity():
    """connectivity holds module-global state — restore it around each
    test so order can't leak a stuck offline-mode flag."""
    before = _conn._offline_mode
    yield
    _conn._offline_mode = before


class TestOfflineMode:
    def test_default_is_online(self):
        assert _conn.is_offline_mode() is False

    def test_set_offline_mode_round_trips(self):
        _conn.set_offline_mode(True)
        assert _conn.is_offline_mode() is True
        _conn.set_offline_mode(False)
        assert _conn.is_offline_mode() is False

    def test_set_offline_mode_coerces_truthy(self):
        _conn.set_offline_mode("yes")          # truthy non-bool
        assert _conn.is_offline_mode() is True


class TestReachability:
    def test_default_is_optimistic(self):
        # Nothing feeds transitions in yet — Phase 1 default is True.
        assert _conn.is_server_reachable() is True

    def test_note_outcome_is_still_a_phase5_stub(self):
        # Guard: when Phase 5 implements this, this test fails loudly —
        # a prompt to replace it with real transition coverage.
        with pytest.raises(NotImplementedError):
            _conn.note_outcome(True)
