"""Stub notifications backend for platforms where desktop notifications
aren't wired up yet. `is_supported()` returns False so settings UI can
hide notification preferences, and `notify()` is a silent no-op.

Future replacements:
- Windows: ToastNotificationManager via winrt.windows.ui.notifications,
  or the `windows_toasts` helper package.
- macOS: NSUserNotificationCenter via pyobjc, or shell out to
  `osascript -e 'display notification ...'`.
"""

from __future__ import annotations


def is_supported() -> bool:
    return False


def notify(title: str, body: str = "", icon: str | None = None, app_name: str = "jellytoast") -> None:
    return None
