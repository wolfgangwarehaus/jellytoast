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


class TestLinuxBackend:
    """The Linux keep-awake must block SLEEP, not the screen: never
    org.freedesktop.ScreenSaver (on KDE that keeps the display on and doesn't
    stop suspend). PowerManagement.Inhibit first, the portal as fallback, and
    release routed to whichever holds."""

    @pytest.fixture
    def linux(self, monkeypatch):
        from jellytoast.power import _linux

        calls = []
        monkeypatch.setattr(_linux, "_hold", None)
        monkeypatch.setattr(_linux, "_release_pm", lambda: calls.append("release_pm"))
        monkeypatch.setattr(_linux, "_release_portal", lambda: calls.append("release_portal"))
        return _linux, calls

    def test_prefers_power_management(self, linux, monkeypatch):
        _linux, calls = linux
        monkeypatch.setattr(_linux, "_inhibit_pm", lambda: True)
        monkeypatch.setattr(_linux, "_inhibit_portal", lambda: calls.append("portal") or True)
        assert _linux.inhibit() is True
        assert _linux._hold == "pm" and "portal" not in calls
        _linux.release()
        assert calls == ["release_pm"] and _linux._hold is None

    def test_falls_back_to_portal(self, linux, monkeypatch):
        _linux, calls = linux
        monkeypatch.setattr(_linux, "_inhibit_pm", lambda: False)
        monkeypatch.setattr(_linux, "_inhibit_portal", lambda: True)
        assert _linux.inhibit() is True and _linux._hold == "portal"
        _linux.release()
        assert calls == ["release_portal"]

    def test_nothing_available(self, linux, monkeypatch):
        _linux, calls = linux
        monkeypatch.setattr(_linux, "_inhibit_pm", lambda: False)
        monkeypatch.setattr(_linux, "_inhibit_portal", lambda: False)
        assert _linux.inhibit() is False
        _linux.release()
        assert calls == []

    def test_a_raising_backend_falls_through(self, linux, monkeypatch):
        _linux, _calls = linux

        def boom():
            raise RuntimeError("bus gone")

        monkeypatch.setattr(_linux, "_inhibit_pm", boom)
        monkeypatch.setattr(_linux, "_inhibit_portal", lambda: True)
        assert _linux.inhibit() is True and _linux._hold == "portal"

    def test_never_uses_the_screensaver_inhibit(self):
        import inspect

        from jellytoast.power import _linux

        code = "\n".join(
            line for line in inspect.getsource(_linux).splitlines()
            if not line.lstrip().startswith("#")
        )
        body = code.split('"""', 2)[2]  # past the module docstring
        assert "org.freedesktop.ScreenSaver" not in body

    def test_portal_call_types_flags_as_uint32(self, monkeypatch):
        import asyncio

        import dbus_next.aio
        from dbus_next import MessageType

        from jellytoast.power import _linux

        sent = []

        class _Reply:
            message_type = MessageType.METHOD_RETURN
            body = ["/org/freedesktop/portal/desktop/request/1_1/t"]

        class _Bus:
            def __init__(self, **_kw):
                pass

            async def connect(self):
                return self

            async def call(self, msg):
                sent.append(msg)
                return _Reply()

            def disconnect(self):
                pass

        monkeypatch.setattr(dbus_next.aio, "MessageBus", _Bus)
        hold = _linux._PortalHold.__new__(_linux._PortalHold)
        hold._bus, hold._handle = None, ""
        assert asyncio.run(hold._inhibit()) is True
        msg = sent[0]
        assert (msg.member, msg.signature) == ("Inhibit", "sua{sv}")
        assert msg.body[1] == _linux._PORTAL_SUSPEND == 4
        asyncio.run(hold._close())
        assert sent[1].member == "Close" and sent[1].path == hold._handle
