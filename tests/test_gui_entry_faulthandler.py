"""GUI-subsystem entry hardening — the silent Windows no-launch bug.

The pipx gui-script (``jellytoast.exe`` on Windows, pythonw generally)
runs with ``sys.stderr = None``; ``faulthandler.enable()`` raises
``RuntimeError`` there, which killed the app inside ``main()`` before
``app.exec()`` with no window and no error output (2026-06-10 Windows
round; the bare ``enable()`` shipped 2026-06-07, two days after the
last verified Windows install). ``_enable_faulthandler`` must tolerate
every stderr shape the entry point can meet.
"""

from __future__ import annotations

import sys

from jellytoast.app import _enable_faulthandler


def test_none_stderr_does_not_raise(monkeypatch):
    # The GUI-subsystem shape: no stderr at all. The old bare
    # faulthandler.enable() raised RuntimeError here.
    monkeypatch.setattr(sys, "stderr", None)
    _enable_faulthandler()  # must simply return


def test_fileno_less_stderr_does_not_raise(monkeypatch):
    # A replaced stream without a real file descriptor (test capture,
    # embedded hosts) — enable() raises from fileno(); must be swallowed.
    class _NoFileno:
        def write(self, *_a):
            return 0

    monkeypatch.setattr(sys, "stderr", _NoFileno())
    _enable_faulthandler()  # must not propagate


def test_real_stderr_enables(monkeypatch, capfd):
    # Sanity: with a real fd-backed stderr the crash hook does engage.
    import faulthandler

    was_enabled = faulthandler.is_enabled()
    monkeypatch.setattr(sys, "stderr", sys.__stderr__)
    try:
        _enable_faulthandler()
        assert faulthandler.is_enabled()
    finally:
        # Restore whatever pytest's own faulthandler plugin had set up.
        if not was_enabled:
            faulthandler.disable()
