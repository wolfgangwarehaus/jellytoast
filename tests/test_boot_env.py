"""Boot setup shared by both front ends (jellytoast/boot_env.py)."""

from __future__ import annotations

import os
import signal
import sys
import time

import pytest

from jellytoast import boot_env


def test_app_re_exports_the_moved_helpers():
    from jellytoast import app

    assert app._enable_faulthandler is boot_env._enable_faulthandler
    assert app._setup_hidpi is boot_env._setup_hidpi


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="glibc only")
def test_malloc_tuning_applies_on_glibc():
    import ctypes

    try:
        has_mallopt = hasattr(ctypes.CDLL("libc.so.6"), "mallopt")
    except OSError:
        has_mallopt = False
    if not has_mallopt:
        pytest.skip("not glibc")
    assert boot_env.tune_malloc() is True


def test_signals_reach_python_without_a_polling_timer(qapp, monkeypatch):
    """SIGTERM → app.quit() through the wake-up fd: no timer wakes the
    interpreter five times a second any more."""
    from PySide6.QtCore import QEventLoop, QTimer

    quit_at = []
    loop = QEventLoop()
    monkeypatch.setattr(qapp, "quit", lambda: (quit_at.append(time.monotonic()), loop.quit()))
    boot_env.install_signal_handlers(qapp)
    try:
        assert isinstance(qapp._jt_signal_wake, tuple)  # the fd path, not the timer
        t0 = time.monotonic()
        QTimer.singleShot(50, lambda: os.kill(os.getpid(), signal.SIGTERM))
        QTimer.singleShot(3000, loop.quit)
        loop.exec()
        assert quit_at and quit_at[0] - t0 < 1.0
    finally:
        for s in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            signal.signal(s, signal.SIG_DFL)
        signal.set_wakeup_fd(-1)
        signal.alarm(0)
