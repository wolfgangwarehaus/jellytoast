"""Tests for the active-cast banner's device-label selection
(modules.now_playing_bar._refresh_active_banner).

The banner used to hardcode a Chromecast/AirPlay ternary that mislabelled
DLNA / Sonos / Snapcast devices as "AirPlay". It now keys the dialog's
``SECTION_LABELS`` map on the active device's ``device_type``. The
label-selection is pure given a ``device_type``, so we test it directly
against the shared mapping rather than painting the widget.

The GUI-only aspect (the banner actually rendering this label on screen,
in the right colour) is left for the manual test plan.
"""

from modules.cast_dialog_sections import SECTION_LABELS, SECTION_TYPES


def _banner_label(device_type: str) -> str:
    """Mirror of the label-selection in ``_refresh_active_banner`` — same
    map + same default casing as the row-label helper."""
    return SECTION_LABELS.get(device_type, (device_type or "Cast").title())


def test_section_labels_covers_every_device_type():
    # Every protocol the manager discovers must have a banner label, or
    # the banner falls back to the title-cased raw type.
    for t in SECTION_TYPES:
        assert t in SECTION_LABELS


def test_each_device_type_maps_to_its_label():
    assert _banner_label("chromecast") == "Chromecast"
    assert _banner_label("airplay") == "AirPlay"
    assert _banner_label("dlna") == "DLNA"
    assert _banner_label("sonos") == "Sonos"
    assert _banner_label("snapcast") == "Snapcast"


def test_non_airplay_devices_are_not_mislabelled_airplay():
    # Regression: the old hardcoded ternary labelled everything that
    # wasn't a Chromecast as "AirPlay".
    for t in ("dlna", "sonos", "snapcast"):
        assert _banner_label(t) != "AirPlay"


def test_unknown_device_type_falls_back_to_title_case():
    assert _banner_label("googletv") == "Googletv"
    assert _banner_label("") == "Cast"
