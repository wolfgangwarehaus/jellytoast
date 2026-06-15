"""Behavioural tests for the cast-status translation logic in
``jellytoast.player_cast_transport._CastTransportMixin``.

These paths (chromecast push/poll status → bus, the DLNA transport-state
poll, and listener (de)registration on device swap) had **zero** direct
coverage before the extraction — the cast adversarial review flagged that
a future edit could silently break them. We exercise the moved methods as
**unbound functions bound to a tiny stub ``self``** (the repo's mixin-test
idiom — no QApplication, no real mpv/cast device), recording the ``_on_*``
emit helpers the translation funnels into.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from jellytoast.cast_manager import CastType
from jellytoast.player_cast_transport import _CastTransportMixin

# Bind the moved methods as plain functions so we can call them on a stub.
_apply_cast_status = _CastTransportMixin._apply_cast_status
_apply_dlna_status = _CastTransportMixin._apply_dlna_status
_ensure_cast_listener = _CastTransportMixin._ensure_cast_listener
_on_cast_started = _CastTransportMixin._on_cast_started
_poll_cast_status = _CastTransportMixin._poll_cast_status


def _emit_stub(**extra):
    """A stub MpvController carrying only the cast-status state + the
    ``_on_*`` recording sinks the translation methods touch."""
    s = SimpleNamespace(
        _cast_last_player_state=None,
        _cast_last_duration_ms=-1,
        _cast_last_position_ms=-1,
        _cast_anchor_pos_ms=0,
        _cast_anchor_wall=0.0,
        _monotonic=lambda: 1000.0,
        _on_position=MagicMock(),
        _on_duration=MagicMock(),
        _on_paused=MagicMock(),
        _on_ended=MagicMock(),
        # _apply_dlna_status syncs the device's pause state back to the
        # CastManager; None makes that guard a no-op for these tests.
        _cast_manager=None,
    )
    for k, v in extra.items():
        setattr(s, k, v)
    return s


def _cc_status(player_state="PLAYING", current_time=None, duration=None, idle_reason=None):
    return SimpleNamespace(
        player_state=player_state,
        current_time=current_time,
        duration=duration,
        idle_reason=idle_reason,
    )


# ── _apply_cast_status: chromecast MEDIA_STATUS → bus ────────────────────────


class TestApplyCastStatus:
    def test_playing_from_idle_resumes(self):
        s = _emit_stub()
        _apply_cast_status(s, _cc_status(player_state="PLAYING"))
        s._on_paused.assert_called_once_with(False)
        assert s._cast_last_player_state == "PLAYING"

    def test_paused_transition_pauses(self):
        s = _emit_stub(_cast_last_player_state="PLAYING")
        _apply_cast_status(s, _cc_status(player_state="PAUSED"))
        s._on_paused.assert_called_once_with(True)

    def test_idle_finished_after_playing_ends_track(self):
        s = _emit_stub(_cast_last_player_state="PLAYING")
        _apply_cast_status(s, _cc_status(player_state="IDLE", idle_reason="FINISHED"))
        s._on_ended.assert_called_once_with()

    def test_idle_on_first_connect_does_not_end(self):
        # No prior PLAYING/PAUSED/BUFFERING state → IDLE is just "connected".
        s = _emit_stub(_cast_last_player_state=None)
        _apply_cast_status(s, _cc_status(player_state="IDLE", idle_reason="FINISHED"))
        s._on_ended.assert_not_called()

    def test_idle_without_finished_reason_does_not_end(self):
        s = _emit_stub(_cast_last_player_state="PLAYING")
        _apply_cast_status(s, _cc_status(player_state="IDLE", idle_reason="CANCELLED"))
        s._on_ended.assert_not_called()

    def test_position_emits_and_anchors_then_dedups(self):
        s = _emit_stub(_cast_last_player_state="PLAYING")
        _apply_cast_status(s, _cc_status(player_state="PLAYING", current_time=12.0))
        s._on_position.assert_called_once_with(12000)
        assert s._cast_last_position_ms == 12000
        assert s._cast_anchor_pos_ms == 12000
        # Same reported time again → no re-emit (steady-state poll between pushes).
        s._on_position.reset_mock()
        _apply_cast_status(s, _cc_status(player_state="PLAYING", current_time=12.0))
        s._on_position.assert_not_called()

    def test_duration_emits_once_per_change(self):
        s = _emit_stub(_cast_last_player_state="PLAYING")
        _apply_cast_status(s, _cc_status(player_state="PLAYING", duration=200.0))
        s._on_duration.assert_called_once_with(200000)
        s._on_duration.reset_mock()
        _apply_cast_status(s, _cc_status(player_state="PLAYING", duration=200.0))
        s._on_duration.assert_not_called()


# ── _apply_dlna_status: DLNA transport snapshot → bus ────────────────────────


class TestApplyDlnaStatus:
    @staticmethod
    def _patch_controller(monkeypatch, state):
        import jellytoast.cast.dlna as _dlna

        monkeypatch.setattr(
            _dlna, "get_dlna_controller", lambda: SimpleNamespace(last_state=lambda: state)
        )

    def test_playing_resumes(self, monkeypatch):
        self._patch_controller(monkeypatch, {"transport_state": "PLAYING"})
        s = _emit_stub()
        _apply_dlna_status(s)
        s._on_paused.assert_called_once_with(False)
        assert s._cast_last_player_state == "PLAYING"

    def test_paused_pauses(self, monkeypatch):
        self._patch_controller(monkeypatch, {"transport_state": "PAUSED_PLAYBACK"})
        s = _emit_stub(_cast_last_player_state="PLAYING")
        _apply_dlna_status(s)
        s._on_paused.assert_called_once_with(True)

    def test_stopped_after_playing_ends_track(self, monkeypatch):
        self._patch_controller(monkeypatch, {"transport_state": "STOPPED"})
        s = _emit_stub(_cast_last_player_state="PLAYING")
        _apply_dlna_status(s)
        s._on_ended.assert_called_once_with()

    def test_position_and_duration_emit(self, monkeypatch):
        self._patch_controller(
            monkeypatch,
            {"transport_state": "PLAYING", "duration_sec": 180, "position_sec": 30},
        )
        s = _emit_stub()
        _apply_dlna_status(s)
        s._on_duration.assert_called_once_with(180000)
        s._on_position.assert_called_once_with(30000)

    def test_empty_state_is_noop(self, monkeypatch):
        self._patch_controller(monkeypatch, None)
        s = _emit_stub()
        _apply_dlna_status(s)
        s._on_paused.assert_not_called()
        s._on_ended.assert_not_called()


# ── _ensure_cast_listener: register-once + swap-deregister ───────────────────


class TestEnsureCastListener:
    @staticmethod
    def _device():
        mc = SimpleNamespace(register_status_listener=MagicMock(), status_listeners=[])
        return SimpleNamespace(media_controller=mc)

    def test_registers_on_first_device(self):
        listener = object()
        s = SimpleNamespace(_cast_listener_attached_to=None, _cast_status_listener=listener)
        s._deregister_cast_listener = lambda: _CastTransportMixin._deregister_cast_listener(s)
        dev = self._device()
        _ensure_cast_listener(s, dev)
        dev.media_controller.register_status_listener.assert_called_once_with(listener)
        assert s._cast_listener_attached_to is dev

    def test_same_device_is_noop(self):
        listener = object()
        dev = self._device()
        s = SimpleNamespace(_cast_listener_attached_to=dev, _cast_status_listener=listener)
        _ensure_cast_listener(s, dev)
        dev.media_controller.register_status_listener.assert_not_called()

    def test_device_swap_deregisters_old_then_registers_new(self):
        listener = object()
        old = self._device()
        old.media_controller.status_listeners = [listener]
        new = self._device()
        s = SimpleNamespace(_cast_listener_attached_to=old, _cast_status_listener=listener)
        s._deregister_cast_listener = lambda: _CastTransportMixin._deregister_cast_listener(s)
        _ensure_cast_listener(s, new)
        # Removed from the old device's listener list…
        assert listener not in old.media_controller.status_listeners
        # …and registered on the new one.
        new.media_controller.register_status_listener.assert_called_once_with(listener)
        assert s._cast_listener_attached_to is new

    def test_none_device_is_noop(self):
        s = SimpleNamespace(_cast_listener_attached_to=None, _cast_status_listener=object())
        _ensure_cast_listener(s, None)
        assert s._cast_listener_attached_to is None


# ── _on_cast_started: poll-timer start + uniform initial volume ──────────────


class TestOnCastStarted:
    def _start_stub(self, *, active, applied, muted_volume=None):
        timer = SimpleNamespace(isActive=lambda: active, start=MagicMock())
        cm = SimpleNamespace(cast_set_initial_volume=MagicMock(return_value=applied))
        bus = SimpleNamespace(
            volume_state=SimpleNamespace(emit=MagicMock()),
            mute_state=SimpleNamespace(emit=MagicMock()),
        )
        return SimpleNamespace(
            _cast_poll_timer=timer,
            _cast_manager=cm,
            bus=bus,
            _cast_volume=0,
            _cast_volume_push_wall=0.0,
            _monotonic=lambda: 1000.0,
            _muted_volume=muted_volume,
            _CAST_INITIAL_VOLUME=_CastTransportMixin._CAST_INITIAL_VOLUME,
        )

    def test_starts_poll_and_pushes_initial_volume(self):
        s = self._start_stub(active=False, applied=30)
        _on_cast_started(s, "Living Room")
        s._cast_poll_timer.start.assert_called_once()
        s._cast_manager.cast_set_initial_volume.assert_called_once_with(30)
        # Backend-applied volume surfaces back to the slider AND seeds the
        # live cast-volume tracker that mute/unmute saves & restores.
        s.bus.volume_state.emit.assert_called_once_with(30)
        assert s._cast_volume == 30

    def test_sonos_clamped_volume_is_what_surfaces(self):
        # cast_set_initial_volume returns the *applied* value (Sonos floor),
        # and that — not the requested 30 — is what the slider tracks.
        s = self._start_stub(active=True, applied=45)
        _on_cast_started(s, "Patio")
        s._cast_poll_timer.start.assert_not_called()  # already active
        s.bus.volume_state.emit.assert_called_once_with(45)
        assert s._cast_volume == 45

    def test_stale_local_mute_is_cleared_on_connect(self):
        # mute at the desk (local mpv: _muted_volume=80, icon lit), THEN
        # cast: the device comes up audible at _CAST_INITIAL_VOLUME, so the
        # stale local mute must be reconciled away — else the next press of
        # the still-lit mute button takes toggle_mute's UNMUTE branch and
        # slams the speaker from 30 up to 80 (the deafening bug, reborn via
        # the local->cast boundary).
        s = self._start_stub(active=False, applied=30, muted_volume=80)
        _on_cast_started(s, "Kitchen")
        assert s._muted_volume is None
        s.bus.mute_state.emit.assert_called_once_with(False)
        assert s._cast_volume == 30

    def test_unmuted_connect_does_not_emit_mute(self):
        s = self._start_stub(active=False, applied=30, muted_volume=None)
        _on_cast_started(s, "Den")
        s.bus.mute_state.emit.assert_not_called()


# ── _poll_cast_status: routing by device type ────────────────────────────────


class TestPollCastStatusRouting:
    def test_inactive_cast_is_noop(self):
        s = SimpleNamespace(_cast_active=lambda: False)
        # Should return immediately without touching a (absent) cast_manager.
        _poll_cast_status(s)  # no AttributeError == pass

    def test_dlna_routes_to_dlna_status(self):
        called = []
        dev = SimpleNamespace(device_type=CastType.DLNA)
        s = SimpleNamespace(
            _cast_active=lambda: True,
            _cast_manager=SimpleNamespace(active_cast=dev),
            _apply_dlna_status=lambda: called.append("dlna"),
        )
        _poll_cast_status(s)
        assert called == ["dlna"]

    def test_airplay_is_inert(self):
        # Non-chromecast, non-DLNA → no status channel tapped here.
        dev = SimpleNamespace(device_type=CastType.AIRPLAY)
        s = SimpleNamespace(
            _cast_active=lambda: True,
            _cast_manager=SimpleNamespace(active_cast=dev),
            _apply_dlna_status=MagicMock(),
        )
        _poll_cast_status(s)
        s._apply_dlna_status.assert_not_called()


_on_cast_stopped = _CastTransportMixin._on_cast_stopped


def _stop_stub(muted_volume):
    s = SimpleNamespace(
        _cast_poll_timer=SimpleNamespace(stop=MagicMock()),
        _cast_last_player_state=None,
        _cast_last_duration_ms=-1,
        _cast_last_position_ms=-1,
        _cast_anchor_pos_ms=0,
        _cast_anchor_wall=0.0,
        settings=SimpleNamespace(volume=80),
        _mpv=None,  # short-circuits the reload tail
        _muted_volume=muted_volume,
        # _on_cast_stopped now detaches the push-status listener too.
        _cast_listener_attached_to=None,
        _cast_status_listener=object(),
        bus=SimpleNamespace(
            volume_state=SimpleNamespace(emit=MagicMock()),
            mute_state=SimpleNamespace(emit=MagicMock()),
            playback_stopped=SimpleNamespace(emit=MagicMock()),
        ),
    )
    s._deregister_cast_listener = lambda: _CastTransportMixin._deregister_cast_listener(s)
    return s


class TestCastStopClearsMute:
    def test_muted_cast_stop_clears_flag_and_emits(self):
        # mute → cast → stop-cast: restoring the audible local volume must
        # also clear _muted_volume and emit mute_state(False), else the
        # icon/slider stay "muted" while mpv plays at the restored level.
        s = _stop_stub(muted_volume=70)
        _on_cast_stopped(s)
        assert s._muted_volume is None
        s.bus.mute_state.emit.assert_called_once_with(False)
        s.bus.volume_state.emit.assert_called_once_with(80)

    def test_unmuted_cast_stop_does_not_emit_mute(self):
        s = _stop_stub(muted_volume=None)
        _on_cast_stopped(s)
        assert s._muted_volume is None
        s.bus.mute_state.emit.assert_not_called()
        s.bus.volume_state.emit.assert_called_once_with(80)


_sync_receiver_volume = _CastTransportMixin._sync_receiver_volume


def _vol_stub(*, cast_volume=30, muted_volume=None, push_wall=0.0, level=0.5):
    cc = SimpleNamespace(status=SimpleNamespace(volume_level=level))
    bus = SimpleNamespace(volume_state=SimpleNamespace(emit=MagicMock()))
    s = SimpleNamespace(
        _muted_volume=muted_volume,
        _cast_volume=cast_volume,
        _cast_volume_push_wall=push_wall,
        _monotonic=lambda: 1000.0,
        _CAST_VOLUME_SYNC_SUPPRESS_S=_CastTransportMixin._CAST_VOLUME_SYNC_SUPPRESS_S,
        bus=bus,
    )
    return s, cc, bus


class TestSyncReceiverVolume:
    """M2: device-side volume changes (remote / Home app / physical
    buttons) must flow back into _cast_volume + the slider, without our
    own writes feeding back."""

    def test_external_change_syncs_to_app(self):
        # Device volume moved to 50% out from under us (app tracker at 30).
        # The slider + the mute-restore baseline must follow reality.
        s, cc, bus = _vol_stub(cast_volume=30, level=0.5)
        _sync_receiver_volume(s, cc)
        assert s._cast_volume == 50
        bus.volume_state.emit.assert_called_once_with(50)

    def test_no_sync_while_muted(self):
        # Muted = WE drove the device to 0; its echo of any level must not
        # clobber the saved unmute baseline (else unmute goes wrong/loud).
        s, cc, bus = _vol_stub(cast_volume=40, muted_volume=40, level=0.0)
        _sync_receiver_volume(s, cc)
        assert s._cast_volume == 40
        bus.volume_state.emit.assert_not_called()

    def test_no_sync_inside_suppression_window(self):
        # We just pushed (push_wall ≈ now), so the device's lagging echo of
        # the OLD value is ignored — no slider bounce, no clobber.
        s, cc, bus = _vol_stub(cast_volume=40, push_wall=999.5, level=0.3)
        _sync_receiver_volume(s, cc)
        assert s._cast_volume == 40
        bus.volume_state.emit.assert_not_called()

    def test_echo_of_our_own_value_is_noop(self):
        # Steady state: device reports exactly what we set → no emit.
        s, cc, bus = _vol_stub(cast_volume=30, level=0.30)
        _sync_receiver_volume(s, cc)
        assert s._cast_volume == 30
        bus.volume_state.emit.assert_not_called()

    def test_missing_receiver_status_is_safe(self):
        s, cc, bus = _vol_stub(cast_volume=30, level=0.5)
        cc.status = None  # receiver status not yet received
        _sync_receiver_volume(s, cc)  # no AttributeError
        bus.volume_state.emit.assert_not_called()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
