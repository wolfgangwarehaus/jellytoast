"""No-op keep-awake backend for platforms without an inhibitor wired up
(macOS for now — a future backend would use IOPMAssertionCreateWithName
with kIOPMAssertionTypePreventUserIdleSystemSleep). Same shape as the
real backends so the controller never platform-branches."""

from __future__ import annotations


def is_supported() -> bool:
    return False


def inhibit() -> bool:
    return False


def release() -> None:
    pass
