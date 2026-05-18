"""Cross-platform media-controls backend smoke tests.

The media_controls package picks its backend at import time:
  • On Linux with dbus-next importable, it binds the MPRIS service.
  • Anywhere else (or if dbus-next blows up at import), it binds the
    no-op UnsupportedMediaControlsService.

The MPRIS backend runs dbus-next on a background asyncio thread; we
*never* call .start() in tests because that would touch the real
session bus. Construction alone is the testable surface.
"""

from __future__ import annotations

import importlib
import sys

import pytest


def _reload_media_controls():
    """Drop and re-import the package so its import-time gate
    re-evaluates against current `platform_compat` flags / mocks."""
    for mod_name in (
        "modules.media_controls",
        "modules.media_controls._mpris",
        "modules.media_controls._unsupported",
    ):
        sys.modules.pop(mod_name, None)
    return importlib.import_module("modules.media_controls")


def _force_linux(monkeypatch):
    import modules.platform_compat as pc

    monkeypatch.setattr(pc, "IS_LINUX", True)


def _force_non_linux(monkeypatch):
    import modules.platform_compat as pc

    monkeypatch.setattr(pc, "IS_LINUX", False)


def test_imports_cleanly_on_linux(monkeypatch, qapp):
    _force_linux(monkeypatch)
    mc = _reload_media_controls()
    assert hasattr(mc, "MediaControlsService")
    # Public name resolves to a callable class.
    assert callable(mc.MediaControlsService)


def test_imports_cleanly_on_non_linux(monkeypatch, qapp):
    _force_non_linux(monkeypatch)
    mc = _reload_media_controls()
    assert hasattr(mc, "MediaControlsService")
    assert callable(mc.MediaControlsService)


def test_mpris_backend_selected_on_linux(monkeypatch, qapp):
    _force_linux(monkeypatch)
    mc = _reload_media_controls()
    assert mc.MediaControlsService.__name__ == "MprisService"


def test_unsupported_backend_selected_off_linux(monkeypatch, qapp):
    _force_non_linux(monkeypatch)
    mc = _reload_media_controls()
    assert mc.MediaControlsService.__name__ == "UnsupportedMediaControlsService"


def test_unsupported_service_constructs_without_error(monkeypatch, qapp):
    _force_non_linux(monkeypatch)
    mc = _reload_media_controls()
    svc = mc.MediaControlsService()
    assert svc is not None


def test_unsupported_service_start_stop_are_silent_noops(monkeypatch, qapp):
    _force_non_linux(monkeypatch)
    mc = _reload_media_controls()
    svc = mc.MediaControlsService()
    # Must not raise, must not touch any external resource.
    svc.start()
    svc.stop()


def test_falls_back_to_unsupported_when_dbus_next_broken(monkeypatch, qapp):
    """If `import dbus_next.aio` fails, the package's try/except at
    import time must catch and bind the unsupported backend instead."""
    _force_linux(monkeypatch)

    # Sabotage dbus_next so the _mpris import raises.
    class _ExplodingModule:
        def __getattr__(self, name):
            raise ImportError(f"simulated missing: {name}")

    monkeypatch.setitem(sys.modules, "dbus_next", _ExplodingModule())
    monkeypatch.setitem(sys.modules, "dbus_next.aio", _ExplodingModule())
    monkeypatch.setitem(sys.modules, "dbus_next.service", _ExplodingModule())
    monkeypatch.setitem(sys.modules, "dbus_next.constants", _ExplodingModule())

    mc = _reload_media_controls()
    assert mc.MediaControlsService.__name__ == "UnsupportedMediaControlsService"


def test_mpris_service_constructs_without_starting_thread(monkeypatch, qapp):
    """MprisService.__init__ must not block on dbus — it only allocates
    state. The asyncio thread spins up in start(), which we never call
    here."""
    _force_linux(monkeypatch)
    mc = _reload_media_controls()
    svc = mc.MediaControlsService()
    # Thread/loop are lazy; not initialized until start().
    assert svc._thread is None
    assert svc._loop is None
    assert svc._dbus is None


def test_mpris_service_stop_is_safe_before_start(monkeypatch, qapp):
    """stop() on a never-started service must not raise. Real shutdown
    paths can fire after a setup failure, so this is a real scenario."""
    _force_linux(monkeypatch)
    mc = _reload_media_controls()
    svc = mc.MediaControlsService()
    svc.stop()  # should be a no-op since _loop is None


def test_mpris_player_update_volume_translates_percent_to_unit(monkeypatch, qapp):
    """update_volume(int_percent) stores the value as a 0-1 float and
    fires emit_properties_changed. We don't exercise the MPRIS Volume
    setter directly because it's a dbus_property descriptor; the state
    updater is the entry path the Qt side actually uses."""
    _force_linux(monkeypatch)
    _reload_media_controls()
    from modules.media_controls._mpris import MprisPlayer
    from modules.player_state import PlayerBus

    bus = PlayerBus.get()
    player = MprisPlayer(bus)

    emitted = []
    monkeypatch.setattr(player, "emit_properties_changed", lambda p: emitted.append(p))

    player.update_volume(50)
    assert player._volume == 0.5
    assert emitted == [{"Volume": 0.5}]


def test_mpris_player_update_status_emits_properties(monkeypatch, qapp):
    """update_status must update _status and call emit_properties_changed
    (mocked here — we don't want a real D-Bus signal)."""
    _force_linux(monkeypatch)
    _reload_media_controls()
    from modules.media_controls._mpris import MprisPlayer
    from modules.player_state import PlayerBus

    bus = PlayerBus.get()
    player = MprisPlayer(bus)

    emitted = []

    def fake_emit(payload):
        emitted.append(payload)

    monkeypatch.setattr(player, "emit_properties_changed", fake_emit)

    player.update_status("Playing")
    assert player._status == "Playing"
    assert emitted == [{"PlaybackStatus": "Playing"}]


def test_mpris_player_can_next_prev_tracks_queue_state(monkeypatch, qapp):
    """Setting has_next / has_prev must update the cached flags so
    MPRIS clients (Plasma widget) toggle the next/prev buttons."""
    _force_linux(monkeypatch)
    _reload_media_controls()
    from modules.media_controls._mpris import MprisPlayer
    from modules.player_state import PlayerBus

    bus = PlayerBus.get()
    player = MprisPlayer(bus)

    monkeypatch.setattr(player, "emit_properties_changed", lambda _: None)

    player.update_can_next_prev(True, False)
    assert player._can_go_next is True
    assert player._can_go_prev is False
    assert player.CanGoNext is True
    assert player.CanGoPrevious is False


@pytest.fixture(autouse=True)
def _restore_media_controls_module():
    """Leave a fresh import after each test so the next test gets a
    clean module + backend reference."""
    yield
    for mod_name in (
        "modules.media_controls",
        "modules.media_controls._mpris",
        "modules.media_controls._unsupported",
    ):
        sys.modules.pop(mod_name, None)
