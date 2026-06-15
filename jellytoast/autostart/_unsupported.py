"""Stub autostart backend for platforms where launch-on-login isn't
wired up yet. Every call returns False so the settings UI can hide the
toggle and call sites no-op cleanly.

Future replacements:
- macOS: drop a LaunchAgent .plist into ~/Library/LaunchAgents/ with
  RunAtLoad=true.
"""

from __future__ import annotations


def is_supported() -> bool:
    return False


def is_enabled() -> bool:
    return False


def enable() -> bool:
    return False


def disable() -> bool:
    return False
