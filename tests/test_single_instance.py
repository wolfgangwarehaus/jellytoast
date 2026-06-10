"""Coverage for ``jellytoast.single_instance.SingleInstance``.

The class owns three responsibilities:

1. **Lock acquisition.** First call to ``acquire()`` for a given key
   takes a ``QSharedMemory`` segment and stands up a ``QLocalServer``
   on a per-user socket name.
2. **Duplicate detection.** A second instance attempting the same key
   sees the existing segment, pings the listener, and returns False.
   The original instance's ``raise_requested`` signal fires in response.
3. **Stale-segment recovery.** If the previous holder died abnormally
   the segment may persist with no listener behind it; ``acquire()``
   detects this via the socket-connect probe and force-recreates.

Tests use unique per-test keys so QSharedMemory state from one case
can't poison another. QLocalServer needs a running QApplication event
loop, supplied via the ``qapp`` session fixture in conftest.py.
"""

from __future__ import annotations

import uuid

from PySide6.QtTest import QSignalSpy

from jellytoast.single_instance import SingleInstance


def _unique_key() -> str:
    """Per-test key — uuid avoids cross-test segment / socket reuse."""
    return f"jt-test-{uuid.uuid4().hex[:12]}"


def _pump(app, ms: int = 200) -> None:
    """Drive the QApplication event loop for ``ms`` ms so queued
    signals (newConnection → _on_new_connection → raise_requested)
    have a chance to fire before the assertion."""
    from PySide6.QtCore import QEventLoop, QTimer

    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


# ── Lock acquisition ────────────────────────────────────────────────────


def test_fresh_acquire_returns_true(qapp):
    """A first-instance acquire succeeds: lock taken, listener up."""
    inst = SingleInstance(_unique_key())
    try:
        assert inst.acquire() is True
        assert inst._mem is not None
        assert inst._server is not None
        assert inst._server.isListening()
    finally:
        if inst._mem is not None:
            inst._mem.detach()


def test_second_acquire_returns_false_and_signals_first(qapp):
    """Two SingleInstance objects on the same key: the second sees the
    lock and pings the first, which emits ``raise_requested``."""
    key = _unique_key()
    first = SingleInstance(key)
    second = SingleInstance(key)
    try:
        assert first.acquire() is True
        spy = QSignalSpy(first.raise_requested)
        assert second.acquire() is False
        # _on_new_connection runs on the GUI thread via the
        # newConnection signal — pump the loop so the signal fires.
        # Event-driven wait: spy.wait() pumps the loop and returns the
        # instant raise_requested fires (the QLocalSocket round-trip +
        # queued GUI delivery), up to a generous ceiling. A fixed-duration
        # pump flaked under CPU load (-n auto / random order) when the
        # round-trip didn't finish inside the window.
        if spy.count() == 0:
            spy.wait(3000)
        assert spy.count() == 1
    finally:
        if first._mem is not None:
            first._mem.detach()


def test_stale_segment_recovers(qapp, monkeypatch):
    """If the segment exists but no listener answers (the previous
    holder was SIGKILL'd), acquire() detaches the stale segment and
    creates a fresh one — returning True for the new owner.

    A faithful in-process simulation isn't possible: the only way the
    real flow's ``detach()`` releases the OS-level segment is when no
    other handle holds it (i.e. the previous holder is dead). Here we
    patch the three primitives the recovery branch hinges on
    (attach succeeds, _signal_existing reports no listener, then
    create succeeds) and verify the branch reaches the listener-setup
    path."""
    inst = SingleInstance(_unique_key())
    monkeypatch.setattr(
        "jellytoast.single_instance.QSharedMemory.attach",
        lambda self: True,
    )
    monkeypatch.setattr(
        "jellytoast.single_instance.QSharedMemory.detach",
        lambda self: True,
    )
    monkeypatch.setattr(
        "jellytoast.single_instance.QSharedMemory.create",
        lambda self, size: True,
    )
    monkeypatch.setattr(inst, "_signal_existing", lambda: False)

    assert inst.acquire() is True
    # The recovered instance stood up its own listener.
    assert inst._mem is not None
    assert inst._server is not None
    assert inst._server.isListening()
    # Clean up: removeServer (don't touch the patched mem.detach).
    from PySide6.QtNetwork import QLocalServer

    QLocalServer.removeServer(inst._socket_name)


def test_acquire_race_returns_false(qapp, monkeypatch):
    """If both attach() AND create() fail (a parallel launch won the
    race between our checks), acquire() signals the winner and
    returns False — caller exits cleanly."""
    inst = SingleInstance(_unique_key())
    # Force both probes to fail. The first signal_existing returns
    # False (so we fall past the attach branch), then create() fails,
    # then the final signal_existing is the one whose return value
    # doesn't actually gate the result — acquire returns False.
    monkeypatch.setattr(
        "jellytoast.single_instance.QSharedMemory.attach",
        lambda self: False,
    )
    monkeypatch.setattr(
        "jellytoast.single_instance.QSharedMemory.create",
        lambda self, size: False,
    )
    signal_calls: list = []
    monkeypatch.setattr(
        inst,
        "_signal_existing",
        lambda: (signal_calls.append(1), True)[1],
    )
    assert inst.acquire() is False
    assert len(signal_calls) == 1  # only the race-path signal


# ── _signal_existing primitive ──────────────────────────────────────────


def test_signal_existing_returns_false_when_unreachable(qapp):
    """No server listening at the socket → ``_signal_existing`` should
    return False quickly (used by the stale-recovery branch)."""
    inst = SingleInstance(_unique_key())
    # Don't acquire — there's nothing to connect to.
    assert inst._signal_existing() is False


def test_signal_existing_pings_real_listener(qapp):
    """When a listener IS up, ``_signal_existing`` connects + writes
    'raise' and returns True. The listener's ``raise_requested`` fires
    on the GUI thread as a side effect."""
    key = _unique_key()
    holder = SingleInstance(key)
    try:
        assert holder.acquire() is True
        spy = QSignalSpy(holder.raise_requested)
        prober = SingleInstance(key)
        assert prober._signal_existing() is True
        # Event-driven wait: spy.wait() pumps the loop and returns the
        # instant raise_requested fires (the QLocalSocket round-trip +
        # queued GUI delivery), up to a generous ceiling. A fixed-duration
        # pump flaked under CPU load (-n auto / random order) when the
        # round-trip didn't finish inside the window.
        if spy.count() == 0:
            spy.wait(3000)
        assert spy.count() == 1
    finally:
        if holder._mem is not None:
            holder._mem.detach()


def test_shared_memory_key_is_per_user(qapp):
    """The QSharedMemory segment key must be per-user — on Linux a bare
    key maps to a system-global ftok id and would collide across user
    accounts (user B attaches to user A's segment, can't reach A's
    per-user socket, then fails to create → exits with no window).
    (2026-05-28 audit #10.)"""
    import getpass

    inst = SingleInstance("jellytoast")
    assert getpass.getuser() in inst._mem_key
    assert inst._mem_key != "jellytoast"  # not the bare, system-global key
    # Distinct from the socket name (different Qt namespace, own suffix).
    assert inst._mem_key != inst._socket_name
