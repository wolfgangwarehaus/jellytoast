"""Tests for offline connectivity state (``modules/offline/connectivity.py``).

Covers:

- Phase 1: the offline-mode flag in isolation, including bool coercion
  for non-bool truthy/falsy inputs.
- Phase 5: the consecutive-failure threshold state machine, the
  auto-offline transition that piggybacks on it, and the reconnect path
  that emits ``connectivity_changed`` + lifts auto-source offline mode
  without disturbing a user-source choice.

The PlayerBus signal emit helpers (``_emit_connectivity_changed`` /
``_emit_offline_mode_changed``) are monkey-patched into list recorders
so the tests stay pure-Python — no QApplication needed.
"""

from __future__ import annotations

import pytest

from modules.offline import connectivity as _conn

# ── Fakes ───────────────────────────────────────────────────────────────────


class _FakeSettings:
    """Stand-in for ``Settings`` covering the attributes connectivity
    actually reads + writes. ``offline_mode`` is a real attribute so we
    can also assert write-through from ``set_offline_mode``.
    ``server_url`` / ``server_hostnames`` default to empty so the
    multi-server failover walk no-ops in tests that don't exercise it."""

    def __init__(
        self,
        *,
        auto_offline_mode: bool = True,
        offline_mode: bool = False,
        server_url: str = "",
        server_hostnames: str = "",
    ) -> None:
        self.auto_offline_mode = auto_offline_mode
        self.offline_mode = offline_mode
        self.server_url = server_url
        self.server_hostnames = server_hostnames


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_connectivity():
    """Wipe module-global connectivity state around every test so
    ordering can't leak a stuck flag (or a stuck failure counter)."""
    _conn._reset_for_tests()
    yield
    _conn._reset_for_tests()


@pytest.fixture
def conn_emits(monkeypatch):
    """Record signal emissions into two lists so tests can assert on
    them without booting a QApplication.

    Returns ``(reachable_calls, offline_calls)`` — each a list of the
    bool values passed to the corresponding emit helper.
    """
    reachable_calls: list[bool] = []
    offline_calls: list[bool] = []
    monkeypatch.setattr(
        _conn,
        "_emit_connectivity_changed",
        lambda v: reachable_calls.append(v),
    )
    monkeypatch.setattr(
        _conn,
        "_emit_offline_mode_changed",
        lambda v: offline_calls.append(v),
    )
    return reachable_calls, offline_calls


@pytest.fixture
def fake_settings(monkeypatch):
    """Install a ``_FakeSettings`` in place of ``modules.settings``'s
    ``get_settings``. Returns the instance so tests can flip
    ``auto_offline_mode`` per-case and inspect ``offline_mode``
    write-through."""
    from modules import settings as _settings_mod

    fake = _FakeSettings()
    monkeypatch.setattr(_settings_mod, "get_settings", lambda: fake)
    return fake


# ── Phase 1 — offline-mode flag (in isolation) ──────────────────────────────


class TestOfflineMode:
    def test_default_is_online(self):
        assert _conn.is_offline_mode() is False

    def test_set_offline_mode_round_trips(self, fake_settings, conn_emits):
        _conn.set_offline_mode(True)
        assert _conn.is_offline_mode() is True
        _conn.set_offline_mode(False)
        assert _conn.is_offline_mode() is False

    def test_set_offline_mode_writes_through_true(self, fake_settings):
        # Public toggle persists so the choice survives restart.
        fake_settings.offline_mode = False
        _conn.set_offline_mode(True)
        assert fake_settings.offline_mode is True

    def test_set_offline_mode_writes_through_false(self, fake_settings):
        fake_settings.offline_mode = True
        _conn.set_offline_mode(False)
        assert fake_settings.offline_mode is False

    def test_set_offline_mode_coerces_truthy(self, fake_settings):
        _conn.set_offline_mode("on")
        assert _conn.is_offline_mode() is True
        _conn.set_offline_mode("")
        assert _conn.is_offline_mode() is False
        _conn.set_offline_mode(1)
        assert _conn.is_offline_mode() is True
        _conn.set_offline_mode(0)
        assert _conn.is_offline_mode() is False


# ── Reachability defaults ───────────────────────────────────────────────────


class TestReachability:
    def test_default_is_optimistic(self):
        # Optimistic boot: we assume reachable until proven otherwise.
        assert _conn.is_server_reachable() is True


# ── Phase 5 — threshold state machine ───────────────────────────────────────


class TestFailureThreshold:
    def test_one_failure_does_not_flip(self, fake_settings, conn_emits):
        reachable_calls, _ = conn_emits
        _conn.note_network_failure()
        assert _conn.is_server_reachable() is True
        assert reachable_calls == []

    def test_two_failures_flip_to_unreachable(self, fake_settings, conn_emits):
        # auto_offline_mode default in _FakeSettings is True, so we'd
        # also get an offline_mode_changed. That's covered separately;
        # here we only assert the connectivity-side transition.
        reachable_calls, _ = conn_emits
        for _ in range(2):
            _conn.note_network_failure()
        assert _conn.is_server_reachable() is False
        assert reachable_calls == [False]

    def test_two_failures_with_auto_offline_on_flips_offline(self, fake_settings, conn_emits):
        fake_settings.auto_offline_mode = True
        _, offline_calls = conn_emits
        for _ in range(2):
            _conn.note_network_failure()
        assert _conn.is_offline_mode() is True
        assert _conn._offline_source == "auto"
        assert offline_calls == [True]

    def test_two_failures_with_auto_offline_off_does_not_flip_offline(
        self, fake_settings, conn_emits
    ):
        fake_settings.auto_offline_mode = False
        _, offline_calls = conn_emits
        for _ in range(2):
            _conn.note_network_failure()
        assert _conn.is_offline_mode() is False
        assert _conn._offline_source is None
        assert offline_calls == []

    def test_extra_failures_after_unreachable_dont_re_emit(self, fake_settings, conn_emits):
        # Once unreachable, additional failures are no-ops on the bus.
        reachable_calls, _ = conn_emits
        for _ in range(5):
            _conn.note_network_failure()
        assert reachable_calls == [False]


class TestSuccessReset:
    def test_success_after_one_failure_resets_counter_silently(self, fake_settings, conn_emits):
        # Below threshold: counter resets, but reachable was still True
        # so no signal should fire.
        reachable_calls, offline_calls = conn_emits
        _conn.note_network_failure()
        _conn.note_success()
        assert _conn._consecutive_failures == 0
        assert reachable_calls == []
        assert offline_calls == []
        # And we should now need a fresh run of two to flip again.
        _conn.note_network_failure()
        assert _conn.is_server_reachable() is True

    def test_reconnect_emits_and_lifts_auto_offline(self, fake_settings, conn_emits):
        fake_settings.auto_offline_mode = True
        reachable_calls, offline_calls = conn_emits
        for _ in range(3):
            _conn.note_network_failure()
        # Sanity: we are in the auto-offline state.
        assert _conn.is_server_reachable() is False
        assert _conn.is_offline_mode() is True
        assert _conn._offline_source == "auto"

        _conn.note_success()

        assert _conn.is_server_reachable() is True
        assert _conn.is_offline_mode() is False
        assert _conn._offline_source is None
        assert reachable_calls == [False, True]
        # offline flipped on (auto) then off (lifted).
        assert offline_calls == [True, False]

    def test_user_set_offline_survives_a_blip(self, fake_settings, conn_emits):
        # User manually toggles offline mode. Then network blips:
        # 3 failures + a success. The reconnect path should restore
        # reachable but leave offline_mode alone because the source
        # was "user".
        fake_settings.auto_offline_mode = True
        _conn.set_offline_mode(True)
        assert _conn._offline_source == "user"

        # Clear the emit recorders so we're only asserting on what
        # happens during the blip — set_offline_mode just emitted a
        # True that's not what we're testing here.
        reachable_calls, offline_calls = conn_emits
        reachable_calls.clear()
        offline_calls.clear()

        for _ in range(3):
            _conn.note_network_failure()
        # auto-offline guard: _offline_mode was already True, so the
        # internal flip is a no-op and source stays "user".
        assert _conn._offline_source == "user"
        assert offline_calls == []  # offline didn't transition.

        _conn.note_success()
        assert _conn.is_server_reachable() is True
        assert _conn.is_offline_mode() is True  # still offline.
        assert _conn._offline_source == "user"  # still user-owned.
        assert reachable_calls == [False, True]
        assert offline_calls == []  # no offline churn.


# ── init() lifecycle ────────────────────────────────────────────────────────


class TestInit:
    def test_init_restores_persisted_offline_mode(self, fake_settings, conn_emits):
        fake_settings.offline_mode = True
        _, offline_calls = conn_emits

        _conn.init()

        assert _conn.is_offline_mode() is True
        assert _conn._offline_source == "user"
        assert offline_calls == [True]

    def test_init_with_no_persisted_offline_is_quiet(self, fake_settings, conn_emits):
        fake_settings.offline_mode = False
        _, offline_calls = conn_emits

        _conn.init()

        assert _conn.is_offline_mode() is False
        assert offline_calls == []

    def test_init_is_idempotent(self, fake_settings, conn_emits):
        # The docstring promises idempotency for re-init from a test
        # harness. Verify the second call is a no-op (no double-emit).
        fake_settings.offline_mode = True
        _, offline_calls = conn_emits

        _conn.init()
        _conn.init()

        assert _conn.is_offline_mode() is True
        assert offline_calls == [True]  # only the first call emits.


# ── Auth-failure tracking ───────────────────────────────────────────────────


class TestAuthFailureThreshold:
    @pytest.fixture
    def auth_emits(self, monkeypatch):
        """Capture auth_failed emissions so tests can assert without
        booting a QApplication."""
        calls: list[None] = []
        monkeypatch.setattr(
            _conn,
            "_emit_auth_failed",
            lambda: calls.append(None),
        )
        return calls

    def test_below_threshold_does_not_emit(self, fake_settings, auth_emits):
        _conn.note_auth_failure()
        _conn.note_auth_failure()
        assert auth_emits == []

    def test_threshold_emits_exactly_once(self, fake_settings, auth_emits):
        for _ in range(3):
            _conn.note_auth_failure()
        assert auth_emits == [None]

    def test_extra_failures_after_threshold_dont_re_emit(self, fake_settings, auth_emits):
        for _ in range(5):
            _conn.note_auth_failure()
        assert auth_emits == [None]

    def test_success_resets_counter(self, fake_settings, auth_emits):
        _conn.note_auth_failure()
        _conn.note_auth_failure()
        _conn.note_auth_success()
        # Fresh budget — single failure shouldn't trip.
        _conn.note_auth_failure()
        _conn.note_auth_failure()
        assert auth_emits == []

    def test_success_after_threshold_allows_re_emit_on_next_burst(self, fake_settings, auth_emits):
        # Threshold tripped, then a success resets, then a fresh burst
        # of failures should fire again (it's a per-burst signal, not
        # a permanent latch).
        for _ in range(3):
            _conn.note_auth_failure()
        _conn.note_auth_success()
        for _ in range(3):
            _conn.note_auth_failure()
        assert auth_emits == [None, None]


# ── Provider-layer concerns (not in this module) ────────────────────────────


class TestProviderResponsibilities:
    @pytest.mark.skip(
        reason="HTTPError 4xx vs network-class is enforced at the "
        "provider call site (see jellyfin_api / subsonic_api), "
        "not inside connectivity. note_network_failure() is "
        "unconditional once called — the filtering lives "
        "upstream. Nothing to test here."
    )
    def test_http_4xx_is_not_a_network_failure(self):
        pass
