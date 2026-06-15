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
