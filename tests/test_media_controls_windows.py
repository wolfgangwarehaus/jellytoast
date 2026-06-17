"""Windows SMTC backend (jellytoast.media_controls._windows).

The winrt projection isn't present on Linux CI, so we assert what we can
there: (1) the module imports and the service instantiates on any
platform; (2) ``start()`` degrades to a clean no-op without winrt; (3)
the media-key → PlayerBus command mapping is correct — the part that must
be right for hardware keys to do the right thing. The live SMTC surface
(flyout, lock screen, real key events) is verified on Windows.
"""

import pytest

from jellytoast.media_controls import _windows
from jellytoast.player_state import PlayerBus


def test_module_imports_and_instantiates(qapp):
    svc = _windows.WindowsMediaControlsService()
    assert svc is not None
    assert svc._smtc is None


def test_start_without_winrt_is_noop(qapp):
    # No winrt projection on Linux: start() must not raise and must leave
    # the service inert (no media-key integration, no dangling state).
    svc = _windows.WindowsMediaControlsService()
    svc.start(window=None)
    assert svc._smtc is None
    svc.stop()  # safe even though it never registered


def test_status_and_metadata_calls_are_safe_without_smtc(qapp):
    """The PlayerBus-driven slots must be inert when SMTC never came up
    (start failed) — they're connected only after a successful init, but
    belt-and-suspenders: calling them directly must not raise."""
    svc = _windows.WindowsMediaControlsService()
    svc._on_paused()
    svc._on_resumed()
    svc._on_stopped()
    svc._on_duration(180_000)
    svc._on_position(5_000)  # no smtc + no duration → early return


class TestButtonMapping:
    @pytest.mark.parametrize(
        "btn,signal_name",
        [
            (_windows._BTN_PLAY, "pause_toggled"),
            (_windows._BTN_PAUSE, "pause_toggled"),
            (_windows._BTN_STOP, "stop_requested"),
            (_windows._BTN_NEXT, "next_track"),
            (_windows._BTN_PREVIOUS, "prev_track"),
        ],
    )
    def test_button_emits_expected_command(self, qapp, btn, signal_name):
        bus = PlayerBus.get()
        svc = _windows.WindowsMediaControlsService()
        svc._bus = bus
        fired = []
        sig = getattr(bus, signal_name)

        def _slot(*_a):
            fired.append(signal_name)

        sig.connect(_slot)
        try:
            svc._on_button(btn)
            assert fired == [signal_name]
        finally:
            sig.disconnect(_slot)

    def test_unknown_button_is_ignored(self, qapp):
        bus = PlayerBus.get()
        svc = _windows.WindowsMediaControlsService()
        svc._bus = bus
        fired = []
        slots = {}
        for name in ("pause_toggled", "stop_requested", "next_track", "prev_track"):

            def _slot(*_a, n=name):
                fired.append(n)

            slots[name] = _slot
            getattr(bus, name).connect(_slot)
        try:
            svc._on_button(3)  # Record — not mapped
            assert fired == []
        finally:
            for name, slot in slots.items():
                getattr(bus, name).disconnect(slot)

    def test_button_without_bus_is_safe(self, qapp):
        svc = _windows.WindowsMediaControlsService()
        # _bus is None until a successful start(); a stray press is a no-op.
        svc._on_button(_windows._BTN_NEXT)


class _StubSmtc:
    """Stand-in for the WinRT SystemMediaTransportControls — only the two
    boolean properties the queue-position handler touches."""

    is_next_enabled = None
    is_previous_enabled = None


class TestQueuePosition:
    """Next/Prev must grey out at the queue boundaries (the SMTC flyout +
    lock screen read is_next_enabled / is_previous_enabled). Index-based,
    mirroring the MPRIS handler — the winrt surface is absent on Linux, so
    we drive the slot against a stub _smtc, same as the rest of this file."""

    def _svc(self):
        svc = _windows.WindowsMediaControlsService()
        svc._smtc = _StubSmtc()
        return svc

    def test_middle_of_queue_enables_both(self, qapp):
        svc = self._svc()
        svc._on_queue_changed(["a", "b", "c"], 1)
        assert svc._smtc.is_next_enabled is True
        assert svc._smtc.is_previous_enabled is True

    def test_last_track_disables_next(self, qapp):
        svc = self._svc()
        svc._on_queue_changed(["a", "b", "c"], 2)
        assert svc._smtc.is_next_enabled is False
        assert svc._smtc.is_previous_enabled is True

    def test_first_track_disables_prev(self, qapp):
        svc = self._svc()
        svc._on_queue_changed(["a", "b", "c"], 0)
        assert svc._smtc.is_next_enabled is True
        assert svc._smtc.is_previous_enabled is False

    def test_single_item_queue_disables_both(self, qapp):
        svc = self._svc()
        svc._on_queue_changed(["only"], 0)
        assert svc._smtc.is_next_enabled is False
        assert svc._smtc.is_previous_enabled is False

    def test_empty_queue_disables_both(self, qapp):
        svc = self._svc()
        svc._on_queue_changed([], -1)
        assert svc._smtc.is_next_enabled is False
        assert svc._smtc.is_previous_enabled is False

    def test_safe_without_smtc(self, qapp):
        # _smtc is None before a successful start(); must not raise.
        _windows.WindowsMediaControlsService()._on_queue_changed(["a", "b"], 0)

    def test_connect_bus_wires_queue_changed(self, qapp):
        # The gap was a missing subscription — prove queue_changed reaches the
        # slot through _connect_bus (not just that the slot's math is right).
        bus = PlayerBus.get()
        svc = self._svc()
        svc._connect_bus()
        try:
            bus.queue_changed.emit(["a", "b", "c"], 2)
            assert svc._smtc.is_next_enabled is False
            assert svc._smtc.is_previous_enabled is True
        finally:
            bus.queue_changed.disconnect(svc._on_queue_changed)
