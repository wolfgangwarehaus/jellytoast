"""Tests for offline connectivity state (``modules/offline/connectivity.py``).

Covers the offline-mode flag (user-toggled, persistent) and the
reachability default. Phase 5 transition coverage (note_success /
note_network_failure threshold flips) is exercised elsewhere.
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
        _conn.set_offline_mode("on")
        assert _conn.is_offline_mode() is True
        _conn.set_offline_mode("")
        assert _conn.is_offline_mode() is False
        _conn.set_offline_mode(1)
        assert _conn.is_offline_mode() is True
        _conn.set_offline_mode(0)
        assert _conn.is_offline_mode() is False


class TestReachability:
    def test_default_is_optimistic(self):
        assert _conn.is_server_reachable() is True
