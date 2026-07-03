"""Cross-platform notifications backend smoke tests. All external
commands are mocked so the suite runs identically on Linux CI workers
and on headless / non-Linux dev machines.
"""

from __future__ import annotations

import importlib
import subprocess
import sys

import pytest


def _reload_notifications():
    """Drop and re-import the package so `_select_backend()` re-evaluates
    against the current `sys.platform` / mocks."""
    for mod_name in (
        "jellytoast.notifications",
        "jellytoast.notifications._linux",
        "jellytoast.notifications._unsupported",
    ):
        sys.modules.pop(mod_name, None)
    return importlib.import_module("jellytoast.notifications")


def test_imports_cleanly_on_linux(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    # platform_compat's constants are baked at ITS import — patch them too,
    # or on a Windows/macOS host _select_backend still routes natively.
    import jellytoast.platform_compat as pc

    monkeypatch.setattr(pc, "IS_LINUX", True)
    monkeypatch.setattr(pc, "IS_WINDOWS", False)
    monkeypatch.setattr(pc, "IS_MACOS", False)
    notifications = _reload_notifications()
    assert hasattr(notifications, "notify")
    assert hasattr(notifications, "is_supported")


def test_imports_cleanly_on_non_linux(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    notifications = _reload_notifications()
    assert hasattr(notifications, "notify")
    assert hasattr(notifications, "is_supported")


def test_is_supported_returns_bool():
    notifications = _reload_notifications()
    result = notifications.is_supported()
    assert isinstance(result, bool)


def test_notify_does_not_raise_on_linux_with_notify_send(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    # platform_compat's constants are baked at ITS import — patch them too,
    # or on a Windows/macOS host _select_backend still routes natively.
    import jellytoast.platform_compat as pc

    monkeypatch.setattr(pc, "IS_LINUX", True)
    monkeypatch.setattr(pc, "IS_WINDOWS", False)
    monkeypatch.setattr(pc, "IS_MACOS", False)
    notifications = _reload_notifications()

    monkeypatch.setattr(
        "jellytoast.notifications._linux.shutil.which",
        lambda cmd: "/usr/bin/notify-send",
    )

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    monkeypatch.setattr(
        "jellytoast.notifications._linux.subprocess.run",
        fake_run,
    )

    notifications.notify("Hi", "World")
    assert len(calls) == 1
    assert "Hi" in calls[0]
    assert "World" in calls[0]


def test_notify_passes_icon_when_provided(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    # platform_compat's constants are baked at ITS import — patch them too,
    # or on a Windows/macOS host _select_backend still routes natively.
    import jellytoast.platform_compat as pc

    monkeypatch.setattr(pc, "IS_LINUX", True)
    monkeypatch.setattr(pc, "IS_WINDOWS", False)
    monkeypatch.setattr(pc, "IS_MACOS", False)
    notifications = _reload_notifications()

    monkeypatch.setattr(
        "jellytoast.notifications._linux.shutil.which",
        lambda cmd: "/usr/bin/notify-send",
    )

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    monkeypatch.setattr(
        "jellytoast.notifications._linux.subprocess.run",
        fake_run,
    )

    notifications.notify("Hi", "World", icon="audio-x-generic")
    assert "--icon" in calls[0]
    assert "audio-x-generic" in calls[0]


def test_unsupported_backend_silently_noops(monkeypatch):
    notifications = _reload_notifications()

    from jellytoast.notifications import _unsupported

    monkeypatch.setattr(
        notifications,
        "_select_backend",
        lambda: _unsupported,
    )

    notifications.notify("Hi", "World")
    assert notifications.is_supported() is False


def test_linux_backend_graceful_when_notify_send_missing(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    # platform_compat's constants are baked at ITS import — patch them too,
    # or on a Windows/macOS host _select_backend still routes natively.
    import jellytoast.platform_compat as pc

    monkeypatch.setattr(pc, "IS_LINUX", True)
    monkeypatch.setattr(pc, "IS_WINDOWS", False)
    monkeypatch.setattr(pc, "IS_MACOS", False)
    notifications = _reload_notifications()

    monkeypatch.setattr(
        "jellytoast.notifications._linux.shutil.which",
        lambda cmd: None,
    )

    assert notifications.is_supported() is False
    notifications.notify("Hi", "World")


def test_linux_backend_graceful_on_subprocess_exception(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    # platform_compat's constants are baked at ITS import — patch them too,
    # or on a Windows/macOS host _select_backend still routes natively.
    import jellytoast.platform_compat as pc

    monkeypatch.setattr(pc, "IS_LINUX", True)
    monkeypatch.setattr(pc, "IS_WINDOWS", False)
    monkeypatch.setattr(pc, "IS_MACOS", False)
    notifications = _reload_notifications()

    monkeypatch.setattr(
        "jellytoast.notifications._linux.shutil.which",
        lambda cmd: "/usr/bin/notify-send",
    )

    def raising_run(cmd, **kwargs):
        raise FileNotFoundError("notify-send vanished mid-call")

    monkeypatch.setattr(
        "jellytoast.notifications._linux.subprocess.run",
        raising_run,
    )

    notifications.notify("Hi", "World")


def test_select_backend_memoizes(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    # platform_compat's constants are baked at ITS import — patch them too,
    # or on a Windows/macOS host _select_backend still routes natively.
    import jellytoast.platform_compat as pc

    monkeypatch.setattr(pc, "IS_LINUX", True)
    monkeypatch.setattr(pc, "IS_WINDOWS", False)
    monkeypatch.setattr(pc, "IS_MACOS", False)
    notifications = _reload_notifications()

    first = notifications._select_backend()
    second = notifications._select_backend()
    assert first is second


@pytest.fixture(autouse=True)
def _restore_notifications_module():
    """Leave a fresh import after each test so the next test gets a
    clean `_select_backend` cache."""
    yield
    for mod_name in (
        "jellytoast.notifications",
        "jellytoast.notifications._linux",
        "jellytoast.notifications._unsupported",
    ):
        sys.modules.pop(mod_name, None)
