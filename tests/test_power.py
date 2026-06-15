"""Sleep-inhibitor (jellytoast.power): keep the machine awake while audio
plays, release on pause/stop/end.

The OS backend is mocked, so the controller's transition logic and the
Windows ctypes flag mapping are what's asserted here — both run on Linux
CI. The real Windows/Linux backends are verified on-device.
"""

import pytest

from jellytoast import power
from jellytoast.player_state import PlayerBus


class _FakeBackend:
    def __init__(self):
        self.inhibit_calls = 0
        self.release_calls = 0
        self.supported = True

    def is_supported(self):
        return self.supported

    def inhibit(self):
        self.inhibit_calls += 1
        return True

    def release(self):
        self.release_calls += 1


@pytest.fixture
def fake_backend(monkeypatch):
    fb = _FakeBackend()
    monkeypatch.setattr(power, "_backend", fb)
    return fb


class TestController:
    def test_inhibit_then_release_transitions(self, qapp, fake_backend):
        inh = power.SleepInhibitor()
        inh._inhibit()
        assert inh._inhibited is True
        assert fake_backend.inhibit_calls == 1
        inh._release()
        assert inh._inhibited is False
        assert fake_backend.release_calls == 1

    def test_inhibit_is_idempotent(self, qapp, fake_backend):
        """Position ticks / repeated starts must not spam the OS — only a
        not-playing → playing transition hits the backend."""
        inh = power.SleepInhibitor()
        inh._inhibit()
        inh._inhibit()
        inh._inhibit()
        assert fake_backend.inhibit_calls == 1

    def test_release_without_inhibit_is_noop(self, qapp, fake_backend):
        inh = power.SleepInhibitor()
        inh._release()
        assert fake_backend.release_calls == 0

    def test_stop_releases_active_hold(self, qapp, fake_backend):
        inh = power.SleepInhibitor()
        inh._inhibit()
        inh.stop()
        assert fake_backend.release_calls == 1
        assert inh._inhibited is False

    def test_backend_failure_leaves_state_uninhibited(self, qapp, monkeypatch):
        class _Failing(_FakeBackend):
            def inhibit(self):
                self.inhibit_calls += 1
                return False  # OS refused

        fb = _Failing()
        monkeypatch.setattr(power, "_backend", fb)
        inh = power.SleepInhibitor()
        inh._inhibit()
        # Did not flip to inhibited, so a later real success can retry.
        assert inh._inhibited is False
        inh._inhibit()
        assert fb.inhibit_calls == 2


class TestBusWiring:
    def test_play_inhibits_pause_releases(self, qapp, fake_backend):
        bus = PlayerBus.get()
        inh = power.SleepInhibitor()
        inh.start()
        try:
            bus.playback_started.emit(None)
            assert fake_backend.inhibit_calls == 1
            assert inh._inhibited is True

            bus.playback_paused.emit()
            assert fake_backend.release_calls == 1
            assert inh._inhibited is False

            bus.playback_resumed.emit()
            assert fake_backend.inhibit_calls == 2

            bus.playback_stopped.emit()
            assert fake_backend.release_calls == 2
        finally:
            # Singleton bus: drop this inhibitor's connections so it can't
            # react to other tests' emissions.
            for sig in (bus.playback_started, bus.playback_resumed):
                try:
                    sig.disconnect(inh._inhibit)
                except (RuntimeError, TypeError):
                    pass
            for sig in (bus.playback_paused, bus.playback_stopped, bus.playback_ended):
                try:
                    sig.disconnect(inh._release)
                except (RuntimeError, TypeError):
                    pass

    def test_start_is_idempotent(self, qapp, fake_backend):
        inh = power.SleepInhibitor()
        inh.start()
        inh.start()  # second start must not double-connect
        bus = PlayerBus.get()
        try:
            bus.playback_started.emit(None)
            assert fake_backend.inhibit_calls == 1
        finally:
            try:
                bus.playback_started.disconnect(inh._inhibit)
            except (RuntimeError, TypeError):
                pass
            for sig in (bus.playback_resumed,):
                try:
                    sig.disconnect(inh._inhibit)
                except (RuntimeError, TypeError):
                    pass
            for sig in (bus.playback_paused, bus.playback_stopped, bus.playback_ended):
                try:
                    sig.disconnect(inh._release)
                except (RuntimeError, TypeError):
                    pass


class TestWindowsBackend:
    def test_sets_system_required_on_inhibit(self, monkeypatch):
        from jellytoast.power import _windows

        calls = []

        class _FakeK32:
            def SetThreadExecutionState(self, flags):
                calls.append(flags)
                return 1  # previous state, non-zero = success

        monkeypatch.setattr(_windows, "_kernel32", _FakeK32())
        assert _windows.is_supported() is True
        assert _windows.inhibit() is True
        assert calls[-1] == _windows.ES_CONTINUOUS | _windows.ES_SYSTEM_REQUIRED
        _windows.release()
        # Release clears the system-required flag, keeping ES_CONTINUOUS.
        assert calls[-1] == _windows.ES_CONTINUOUS

    def test_unsupported_without_kernel32(self, monkeypatch):
        from jellytoast.power import _windows

        monkeypatch.setattr(_windows, "_kernel32", None)
        assert _windows.is_supported() is False
        assert _windows.inhibit() is False
        _windows.release()  # must not raise


class TestUnsupportedBackend:
    def test_is_noop(self):
        from jellytoast.power import _unsupported

        assert _unsupported.is_supported() is False
        assert _unsupported.inhibit() is False
        _unsupported.release()  # must not raise
