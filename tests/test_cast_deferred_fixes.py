"""Bug-hunt deferred-batch cast fixes (logic-level; live cast behaviour is
hardware-verified):

- _CastTransportMixin._deregister_cast_listener detaches the push-status
  listener + clears the attachment (now also called on cast STOP, not just
  on a device swap — the leak fix).
- CastManager.sync_cast_paused tracks an externally-observed pause so a
  device-side pause/resume doesn't invert the next in-app toggle.

Both methods are self-contained, so they're exercised with a minimal
stand-in self rather than a full controller/manager.
"""

import types

from jellytoast.cast_manager._manager import CastManager
from jellytoast.player_cast_transport import _CastTransportMixin


def _fake_cc(listener):
    mc = types.SimpleNamespace(status_listeners=[listener])
    return types.SimpleNamespace(media_controller=mc)


class TestListenerDeregister:
    def test_removes_listener_and_clears_attachment(self):
        listener = object()
        cc = _fake_cc(listener)
        obj = types.SimpleNamespace(
            _cast_listener_attached_to=cc,
            _cast_status_listener=listener,
        )
        _CastTransportMixin._deregister_cast_listener(obj)
        assert listener not in cc.media_controller.status_listeners
        assert obj._cast_listener_attached_to is None

    def test_idempotent_when_unattached(self):
        obj = types.SimpleNamespace(
            _cast_listener_attached_to=None,
            _cast_status_listener=object(),
        )
        _CastTransportMixin._deregister_cast_listener(obj)  # must not raise
        assert obj._cast_listener_attached_to is None

    def test_best_effort_when_listener_already_gone(self):
        listener = object()
        cc = _fake_cc(object())  # a DIFFERENT listener in the list
        obj = types.SimpleNamespace(
            _cast_listener_attached_to=cc,
            _cast_status_listener=listener,
        )
        _CastTransportMixin._deregister_cast_listener(obj)  # no ValueError
        assert obj._cast_listener_attached_to is None


class TestSyncCastPaused:
    def test_sets_flag_both_ways(self):
        obj = types.SimpleNamespace()
        CastManager.sync_cast_paused(obj, True)
        assert obj._cast_paused is True
        CastManager.sync_cast_paused(obj, False)
        assert obj._cast_paused is False
