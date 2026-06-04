"""Windows blur backend — DWM Mica / Acrylic.

STUB (P0): reports UNSUPPORTED so the frosted theme paints its near-opaque
fallback body — a legible dark frosted panel, never a see-through window —
on Windows. The real Mica implementation (DwmSetWindowAttribute with
DWMWA_SYSTEMBACKDROP_TYPE on Windows 11 22H2+, legacy DWMWA_MICA_EFFECT on
21H2, opaque on Windows 10) lands in P1 and returns a status straight from
the DwmSetWindowAttribute HRESULT — Windows, unlike KWin, gives real success
feedback. See docs/research/portable_blur.md §5 for the full ctypes recipe.
"""

from __future__ import annotations


def is_supported() -> bool:
    return False


def apply(widget, enabled: bool, corner_radius: int = 0) -> bool:
    return False


def probe():
    from modules.blur import BlurStatus

    return BlurStatus.UNSUPPORTED
