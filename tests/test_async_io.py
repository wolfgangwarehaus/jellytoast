"""Tests for modules.async_io — the shutdown-race guard on _AsyncTask.

A pool worker (``_AsyncTask.run`` on the shared QThreadPool) can finish
*after* its ``_Signaler``'s C++ object is gone — at app/interpreter
shutdown the QApplication is torn down while the daemon worker is still
running its fn, so the result ``emit`` hits a deleted QObject. The worker
must swallow that ``RuntimeError`` rather than spewing it to stderr (it
surfaced a lot under random test order, and it's a real app-quit race).
"""

from modules.async_io import _AsyncTask


class _DeadSignal:
    """Stand-in for a Signal whose owning QObject was C++-deleted —
    ``emit`` raises like PySide does in that state."""

    def emit(self, *_args):
        raise RuntimeError("Signal source has been deleted")


class _DeadSignaler:
    completed = _DeadSignal()
    failed = _DeadSignal()


def test_emit_swallows_runtimeerror_from_deleted_signaler():
    # The static guard must not propagate a deleted-signaler emit.
    _AsyncTask._emit(_DeadSignal(), "payload")  # must not raise


def test_run_swallows_when_signaler_deleted_on_success():
    task = _AsyncTask(lambda: 42, (), {}, _DeadSignaler())
    task.run()  # completed.emit raises internally → swallowed, no propagation


def test_run_swallows_when_signaler_deleted_on_error():
    def boom():
        raise ValueError("fn failed")

    task = _AsyncTask(boom, (), {}, _DeadSignaler())
    task.run()  # fn raises → failed.emit raises → both swallowed


def test_run_emits_result_to_a_live_signaler():
    got = []

    class _LiveSignal:
        def emit(self, payload):
            got.append(payload)

    class _LiveSignaler:
        completed = _LiveSignal()
        failed = _LiveSignal()

    _AsyncTask(lambda: "ok", (), {}, _LiveSignaler()).run()
    assert got == ["ok"]
