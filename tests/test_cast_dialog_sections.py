"""Tests for the cast-dialog section-state helpers.

`cast_dialog_sections` keeps the persist + default-resolution logic
out of the dialog widget so we can exercise it without spinning up Qt's
widget subsystem. QSettings is exercised through the in-memory
``QSettings.IniFormat`` backed by a tmp file, which the
``QStandardPaths.setTestModeEnabled(True)`` call in ``conftest.py``
already redirects away from the user's real ~/.config.
"""

from dataclasses import dataclass

from PySide6.QtCore import QSettings

from modules.cast_dialog_sections import (
    SECTION_LABELS,
    SECTION_TYPES,
    SectionState,
    group_devices_by_type,
    read_collapsed,
    resolve_state,
    write_collapsed,
)


@dataclass
class _FakeDevice:
    """Lightweight stand-in for ``modules.cast_manager.CastDevice`` — the
    section logic only needs ``device_type``, so importing the real
    dataclass would drag in pychromecast just to construct test data."""

    device_type: str
    name: str = "dev"


def _fresh_settings(tmp_path) -> QSettings:
    """A QSettings rooted in tmp_path with a clean INI file, so each
    test owns its own persisted state and order-of-execution can't
    cross-contaminate."""
    path = tmp_path / "settings.ini"
    return QSettings(str(path), QSettings.Format.IniFormat)


# ── Persistence helpers ───────────────────────────────────────────────


def test_read_collapsed_unset_returns_none(tmp_path):
    s = _fresh_settings(tmp_path)
    assert read_collapsed(s, "chromecast") is None


def test_write_then_read_round_trip(tmp_path):
    s = _fresh_settings(tmp_path)
    write_collapsed(s, "airplay", True)
    assert read_collapsed(s, "airplay") is True
    write_collapsed(s, "airplay", False)
    assert read_collapsed(s, "airplay") is False


def test_write_uses_correct_key(tmp_path):
    s = _fresh_settings(tmp_path)
    write_collapsed(s, "sonos", True)
    # Bypass the helper and inspect the raw key so a future rename
    # would fail this test loudly instead of silently breaking on-disk
    # compatibility for existing users.
    assert s.contains("cast_dialog/section_sonos_collapsed")


def test_each_section_type_persists_independently(tmp_path):
    s = _fresh_settings(tmp_path)
    write_collapsed(s, "chromecast", False)
    write_collapsed(s, "airplay", True)
    write_collapsed(s, "dlna", True)
    assert read_collapsed(s, "chromecast") is False
    assert read_collapsed(s, "airplay") is True
    assert read_collapsed(s, "dlna") is True
    assert read_collapsed(s, "sonos") is None


# ── Default-state resolver ────────────────────────────────────────────


def test_empty_section_no_setting_defaults_collapsed(tmp_path):
    s = _fresh_settings(tmp_path)
    state = resolve_state(s, "chromecast", has_devices=False)
    assert state == SectionState("chromecast", collapsed=True)


def test_nonempty_section_no_setting_defaults_expanded(tmp_path):
    s = _fresh_settings(tmp_path)
    state = resolve_state(s, "chromecast", has_devices=True)
    assert state == SectionState("chromecast", collapsed=False)


def test_explicit_setting_wins_over_empty_default(tmp_path):
    s = _fresh_settings(tmp_path)
    # User collapsed a non-empty section — even when devices show up,
    # the section stays where they left it.
    write_collapsed(s, "chromecast", True)
    state = resolve_state(s, "chromecast", has_devices=True)
    assert state.collapsed is True


def test_explicit_setting_wins_over_nonempty_default(tmp_path):
    s = _fresh_settings(tmp_path)
    # User expanded an empty section (curious about what's there) —
    # we keep it open even though the default would collapse it.
    write_collapsed(s, "snapcast", False)
    state = resolve_state(s, "snapcast", has_devices=False)
    assert state.collapsed is False


# ── Toggle path persists ──────────────────────────────────────────────


def test_toggle_writes_new_state(tmp_path):
    s = _fresh_settings(tmp_path)
    # Simulate a toggle: read current resolved state, flip it, persist.
    before = resolve_state(s, "airplay", has_devices=True)
    write_collapsed(s, "airplay", not before.collapsed)
    after = resolve_state(s, "airplay", has_devices=True)
    assert after.collapsed != before.collapsed


def test_state_survives_settings_reload(tmp_path):
    s1 = _fresh_settings(tmp_path)
    write_collapsed(s1, "dlna", True)
    s1.sync()
    # New QSettings instance against the same file — what we wrote
    # must persist across "dialog closes + reopens" cycles.
    s2 = QSettings(s1.fileName(), QSettings.Format.IniFormat)
    assert read_collapsed(s2, "dlna") is True


# ── Section metadata ──────────────────────────────────────────────────


def test_all_required_sections_declared():
    # Spec calls for these five sections by name. If a future refactor
    # drops one, this test catches it before the dialog goes missing a
    # header at runtime.
    assert set(SECTION_TYPES) == {"chromecast", "airplay", "dlna", "sonos", "snapcast"}
    for t in SECTION_TYPES:
        assert t in SECTION_LABELS
        assert SECTION_LABELS[t]  # non-empty label


# ── Grouping ──────────────────────────────────────────────────────────


def test_group_devices_by_type_partitions_correctly():
    devices = [
        _FakeDevice("chromecast", "Living Room"),
        _FakeDevice("airplay", "Kitchen HomePod"),
        _FakeDevice("chromecast", "Bedroom"),
        _FakeDevice("airplay", "Office"),
    ]
    buckets = group_devices_by_type(devices)
    assert len(buckets["chromecast"]) == 2
    assert len(buckets["airplay"]) == 2
    assert buckets["dlna"] == []
    assert buckets["sonos"] == []
    assert buckets["snapcast"] == []


def test_group_devices_by_type_ignores_unknown_types():
    devices = [
        _FakeDevice("chromecast"),
        _FakeDevice("bogus_kind"),
    ]
    buckets = group_devices_by_type(devices)
    assert len(buckets["chromecast"]) == 1
    # Unknown types are dropped — they can't materialise a phantom section.
    assert sum(len(v) for v in buckets.values()) == 1


def test_group_devices_by_type_handles_empty_input():
    buckets = group_devices_by_type([])
    for t in SECTION_TYPES:
        assert buckets[t] == []
