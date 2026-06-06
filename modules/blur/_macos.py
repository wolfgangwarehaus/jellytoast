"""macOS blur backend — NSVisualEffectView vibrancy.

STUB (deferred): reports UNSUPPORTED so the frosted theme paints its
near-opaque fallback body on macOS. The real vibrancy path (bridge the
widget's NSView via pyobjc, set the NSWindow clear, insert an
NSVisualEffectView below the Qt content) is design-only until there is Mac
hardware to test on — project policy is no untestable Apple code. See
docs/research/portable_blur.md §6.
"""

from __future__ import annotations


def is_supported() -> bool:
    return False


def apply(
    widget, enabled: bool, corner_radius: int = 0, dark: bool = True
) -> bool:
    return False


def probe():
    from modules.blur import BlurStatus

    return BlurStatus.UNSUPPORTED


def reason(status):
    return "macOS vibrancy isn't implemented yet — using a near-opaque body"
