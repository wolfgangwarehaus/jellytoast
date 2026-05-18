"""Tests for the offline manager's Wi-Fi-only download gate.

v1 of the gate is the user toggle + a metered-flag stub the future
network-auto-detect layer will flip. ``_dispatch`` only blocks when
both are True, so a user who flips the toggle on while on Wi-Fi sees
no behaviour change until the connection actually changes underneath.
"""

from __future__ import annotations

import pytest

from modules.offline import manager as _mgr


@pytest.fixture(autouse=True)
def _reset_manager():
    """Manager holds module-level wifi-only + metered + queue state.
    Restore between tests so a stuck flag can't leak across cases."""
    _mgr._reset_for_tests()
    yield
    _mgr._reset_for_tests()


@pytest.fixture
def fake_settings(monkeypatch):
    """In-memory replacement for ``settings.downloads_wifi_only`` — the
    real QSettings would touch the test-mode QStandardPaths file and
    leak persistence across tests."""

    class _FakeSettings:
        def __init__(self):
            self.downloads_paused = False
            self.downloads_wifi_only = False

    fake = _FakeSettings()
    import modules.settings as settings_mod

    monkeypatch.setattr(settings_mod, "get_settings", lambda: fake)
    return fake


@pytest.fixture
def bus_spy(monkeypatch):
    """Spy on the PlayerBus signal emissions without needing a real
    QApplication. Records ``(signal_name, args)`` in order."""
    events = []

    class _Signal:
        def __init__(self, name):
            self._name = name

        def emit(self, *args):
            events.append((self._name, args))

        def connect(self, *a, **k):
            pass

    class _Bus:
        download_queue_paused = _Signal("paused")
        download_queue_resumed = _Signal("resumed")
        download_progress = _Signal("progress")
        downloads_wifi_only_changed = _Signal("wifi_only_changed")

    bus = _Bus()
    import modules.player_state as ps

    monkeypatch.setattr(ps.PlayerBus, "get", classmethod(lambda cls: bus))
    return events


class TestSetWifiOnly:
    def test_default_false(self, fake_settings, bus_spy):
        assert _mgr.is_wifi_only() is False

    def test_set_true_persists(self, fake_settings, bus_spy):
        _mgr.set_wifi_only(True)
        assert fake_settings.downloads_wifi_only is True

    def test_set_true_emits_signal(self, fake_settings, bus_spy):
        _mgr.set_wifi_only(True)
        assert ("wifi_only_changed", (True,)) in bus_spy

    def test_set_false_emits_signal(self, fake_settings, bus_spy):
        _mgr.set_wifi_only(True)
        _mgr.set_wifi_only(False)
        assert ("wifi_only_changed", (False,)) in bus_spy

    def test_set_idempotent(self, fake_settings, bus_spy):
        _mgr.set_wifi_only(True)
        _mgr.set_wifi_only(True)
        _mgr.set_wifi_only(True)
        emits = [e for e in bus_spy if e[0] == "wifi_only_changed"]
        assert len(emits) == 1

    def test_persists_across_reload(self, fake_settings, bus_spy):
        _mgr.set_wifi_only(True)
        # Simulate a restart: fresh hydration from settings.
        _mgr._reset_for_tests()
        # set_wifi_only persisted to the fake; first read should
        # hydrate from it.
        fake_settings.downloads_wifi_only = True
        assert _mgr.is_wifi_only() is True


class TestDispatchGating:
    def test_wifi_only_and_metered_blocks_dispatch(
        self, fake_settings, bus_spy, monkeypatch
    ):
        started = []
        monkeypatch.setattr(_mgr, "_start_download", lambda tid: started.append(tid))

        _mgr.set_wifi_only(True)
        _mgr.mark_metered(True)
        _mgr._jobs["t1"] = {"item": {"Id": "t1"}, "parents": set()}
        _mgr._queue.append("t1")
        _mgr._dispatch()

        assert started == []
        assert "t1" in _mgr._queue
        assert "t1" not in _mgr._active

    def test_wifi_only_without_metered_proceeds(
        self, fake_settings, bus_spy, monkeypatch
    ):
        started = []
        monkeypatch.setattr(_mgr, "_start_download", lambda tid: started.append(tid))

        _mgr.set_wifi_only(True)
        # _on_metered defaults False.
        _mgr._jobs["t1"] = {"item": {"Id": "t1"}, "parents": set()}
        _mgr._queue.append("t1")
        _mgr._dispatch()

        assert started == ["t1"]

    def test_metered_without_wifi_only_proceeds(
        self, fake_settings, bus_spy, monkeypatch
    ):
        started = []
        monkeypatch.setattr(_mgr, "_start_download", lambda tid: started.append(tid))

        _mgr.mark_metered(True)
        # Wi-Fi-only stays False — gate is a no-op until the user opts in.
        _mgr._jobs["t1"] = {"item": {"Id": "t1"}, "parents": set()}
        _mgr._queue.append("t1")
        _mgr._dispatch()

        assert started == ["t1"]


class TestKicksDispatchOnRelease:
    def test_set_wifi_only_false_while_blocked_kicks_dispatch(
        self, fake_settings, bus_spy, monkeypatch
    ):
        started = []
        monkeypatch.setattr(_mgr, "_start_download", lambda tid: started.append(tid))

        _mgr.set_wifi_only(True)
        _mgr.mark_metered(True)
        _mgr._jobs["t1"] = {"item": {"Id": "t1"}, "parents": set()}
        _mgr._queue.append("t1")
        _mgr._dispatch()
        assert started == []  # blocked

        _mgr.set_wifi_only(False)
        assert started == ["t1"]

    def test_metered_transition_true_to_false_kicks_dispatch(
        self, fake_settings, bus_spy, monkeypatch
    ):
        started = []
        monkeypatch.setattr(_mgr, "_start_download", lambda tid: started.append(tid))

        _mgr.set_wifi_only(True)
        _mgr.mark_metered(True)
        _mgr._jobs["t1"] = {"item": {"Id": "t1"}, "parents": set()}
        _mgr._queue.append("t1")
        _mgr._dispatch()
        assert started == []

        _mgr.mark_metered(False)
        assert started == ["t1"]


class TestMarkMetered:
    def test_default_false(self, fake_settings, bus_spy):
        assert _mgr.is_on_metered() is False

    def test_mark_true(self, fake_settings, bus_spy):
        _mgr.mark_metered(True)
        assert _mgr.is_on_metered() is True

    def test_mark_not_persisted(self, fake_settings, bus_spy):
        _mgr.mark_metered(True)
        # No settings key should have been touched — metered is transient.
        assert not hasattr(fake_settings, "downloads_metered")
