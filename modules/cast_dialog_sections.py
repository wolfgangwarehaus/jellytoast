"""Pure-function helpers for the Cast dialog's collapsible-section state.

The Cast dialog (``modules.cast_dialog.CastDialog``) groups discovered
devices into one section per cast type — Chromecast, AirPlay, DLNA, Sonos,
Snapcast — with a clickable header that toggles between expanded and
collapsed. Section state persists across dialog opens via QSettings.

Design intent (memory ``feedback_cast_menu_unified_collapsible``):

- One menu, sections by type.
- Empty sections collapse by default (so users don't see five empty
  rows when only Chromecast is online).
- Sections with discovered devices expand by default (the common path).
- An explicit user toggle always wins over the defaults.

This module is widget-free so the state logic can be unit-tested without
spinning up a QDialog.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from PySide6.QtCore import QSettings

# Canonical type identifiers, one per cast protocol the manager
# discovers. All five — Chromecast, AirPlay, and the DLNA / Sonos /
# Snapcast backends (CastManager ``_OtherProtocolsMixin``) — emit
# ``CastDevice`` entries whose ``device_type`` matches a value here.
SECTION_TYPES: tuple[str, ...] = (
    "chromecast",
    "airplay",
    "dlna",
    "sonos",
    "snapcast",
)


# Human-readable labels rendered in the section header.
SECTION_LABELS: dict[str, str] = {
    "chromecast": "Chromecast",
    "airplay": "AirPlay",
    "dlna": "DLNA",
    "sonos": "Sonos",
    "snapcast": "Snapcast",
}


@dataclass(frozen=True)
class SectionState:
    """Resolved collapsed/expanded state for a single section."""

    section_type: str
    collapsed: bool


def _key(section_type: str) -> str:
    """QSettings key for a given section type's persisted collapsed flag."""
    return f"cast_dialog/section_{section_type}_collapsed"


def read_collapsed(settings: QSettings, section_type: str) -> Optional[bool]:
    """Read the persisted collapsed flag for ``section_type``.

    Returns ``None`` when no value has been stored — the caller should
    then fall back to the "empty → collapsed, non-empty → expanded"
    default. Returns ``True`` / ``False`` for an explicit user choice.
    """
    if not settings.contains(_key(section_type)):
        return None
    raw = settings.value(_key(section_type))
    # QSettings stringifies booleans on disk in the INI backend.
    # Normalise both bool and str-flavours back to a Python bool so
    # callers don't have to think about backend quirks.
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        return raw.strip().lower() in ("true", "1", "yes")
    return bool(raw)


def write_collapsed(settings: QSettings, section_type: str, collapsed: bool) -> None:
    """Persist the collapsed flag for ``section_type``."""
    settings.setValue(_key(section_type), bool(collapsed))


def resolve_state(settings: QSettings, section_type: str, has_devices: bool) -> SectionState:
    """Determine whether ``section_type`` should render collapsed.

    Precedence:
    1. Explicit persisted value (user toggled it before) wins.
    2. Otherwise default: empty section → collapsed, non-empty → expanded.
    """
    stored = read_collapsed(settings, section_type)
    if stored is not None:
        return SectionState(section_type=section_type, collapsed=stored)
    return SectionState(
        section_type=section_type,
        collapsed=not has_devices,
    )


def group_devices_by_type(devices) -> dict[str, list]:
    """Partition a flat ``CastDevice`` iterable into one list per section
    type. Unknown ``device_type`` values land in the closest matching
    section; otherwise they're dropped so a malformed device can't
    create a phantom section."""
    buckets: dict[str, list] = {t: [] for t in SECTION_TYPES}
    for dev in devices:
        t = getattr(dev, "device_type", "") or ""
        t = t.lower()
        if t in buckets:
            buckets[t].append(dev)
    return buckets
