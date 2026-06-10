"""Per-open dialog lifetime — the SettingsDialog deletion rule swept to
its siblings (2026-06-10 audit).

SettingsDialog established the pattern (settings_dialog.py): dialogs that
are built fresh per open and connect bound-method slots to the PlayerBus
singleton must actually die on close, or every open leaks a parented
corpse whose slots keep firing. ``done()`` hides WITHOUT a close event,
so ``WA_DeleteOnClose`` alone misses the Esc/reject path —
``finished→deleteLater`` covers it.

Covers the three sites the original fix missed:
- ``SnapcastControlDialog``: Esc/reject must run the closeEvent teardown
  (bus disconnects + JSON-RPC session drop), not just hide.
- ``CastDialog``: one deleted dialog per open, not an accumulating
  theme_changed subscriber.
- ``PairingDialog.run``: the exec()'d dialog is reaped after use.
"""

from __future__ import annotations

import shiboken6
from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtWidgets import QDialog


def _flush_deferred():
    """Deliver pending deleteLater()s — what the real event loop would do
    on its next pass."""
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


# ── SnapcastControlDialog ─────────────────────────────────────────────


class _FakeSnapController:
    def __init__(self):
        self.disconnected = False

    def connect(self, host, port, on_done=None):
        pass

    def snapshot(self):
        return {}

    def disconnect(self):
        self.disconnected = True


def _snapcast_dialog(qapp):
    from modules.cast.snapcast import SnapcastServerInfo
    from modules.snapcast_control import SnapcastControlDialog

    ctl = _FakeSnapController()
    info = SnapcastServerInfo(host="10.0.0.2", port=1705, hostname="snap.local")
    return SnapcastControlDialog(ctl, info, parent=None), ctl


def test_snapcast_reject_runs_teardown(qapp):
    # THE original bug: Esc → reject() → done() hides without a close
    # event, so the JSON-RPC session stayed live and the bus slots kept
    # rebuilding a hidden corpse (one per Esc).
    d, ctl = _snapcast_dialog(qapp)
    d.reject()
    assert ctl.disconnected is True
    assert d._closed is True


def test_snapcast_close_still_runs_teardown(qapp):
    # Window-X path (closeEvent) must keep working alongside the
    # finished-signal route, and the teardown must stay idempotent.
    d, ctl = _snapcast_dialog(qapp)
    d.close()
    assert ctl.disconnected is True
    d.close()  # second close: no double-disconnect warning / crash


def test_snapcast_dialog_deleted_after_reject(qapp):
    d, _ctl = _snapcast_dialog(qapp)
    assert d.testAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
    d.reject()
    _flush_deferred()
    assert not shiboken6.isValid(d)


# ── CastDialog ────────────────────────────────────────────────────────


class _FakeCastManager:
    """Just enough surface for CastDialog.__init__/done(): callback
    registration, an empty device list, and a no-op scan."""

    def __init__(self):
        self._on_update = None
        self.active_cast = None

    def set_devices_callback(self, cb):
        self._on_update = cb

    def get_all_devices(self):
        return []

    def discover_all(self):
        pass


def test_cast_dialog_deleted_per_open(qapp):
    from modules.cast_dialog import CastDialog

    mgr = _FakeCastManager()
    d = CastDialog(mgr, parent=None)
    assert d.testAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
    d.reject()
    # done() clears the discovery callback, finished→deleteLater reaps.
    assert mgr._on_update is None
    _flush_deferred()
    assert not shiboken6.isValid(d)


# ── PairingDialog.run ─────────────────────────────────────────────────


def _pairing_device():
    from modules.airplay2 import AirPlay2Device

    dev = AirPlay2Device.__new__(AirPlay2Device)
    dev.name = "Probe"
    dev.identifier = "probe-id"
    return dev


def test_pairing_run_reaps_dialog(qapp, monkeypatch):
    from modules import airplay_pairing as ap

    # _start_begin kicks off a real pairing round-trip; stub it out.
    monkeypatch.setattr(ap.PairingDialog, "_start_begin", lambda self: None)
    created = []

    def fake_exec(self):
        created.append(self)
        return QDialog.DialogCode.Rejected

    monkeypatch.setattr(ap.PairingDialog, "exec", fake_exec)
    creds = ap.PairingDialog.run(None, _pairing_device())
    assert creds == ""
    _flush_deferred()
    assert created and not shiboken6.isValid(created[0])


def test_pairing_run_returns_credentials_then_reaps(qapp, monkeypatch):
    from modules import airplay_pairing as ap

    monkeypatch.setattr(ap.PairingDialog, "_start_begin", lambda self: None)
    created = []

    def fake_exec(self):
        self.result_credentials = "blob"
        created.append(self)
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(ap.PairingDialog, "exec", fake_exec)
    creds = ap.PairingDialog.run(None, _pairing_device())
    assert creds == "blob"
    _flush_deferred()
    assert created and not shiboken6.isValid(created[0])
