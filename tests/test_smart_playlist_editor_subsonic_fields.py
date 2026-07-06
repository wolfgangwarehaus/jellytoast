"""Coverage for the rule-field dropdown when the active provider has
unsupported smart-rule fields (Subsonic: ``rating`` / ``last_played``).

_RuleChip annotates those fields with an in-label suffix plus an
explanatory tooltip via the QComboBox API (``setItemData`` with
ToolTipRole). The 0.1.7 theming arc swapped the dropdown from QComboBox
to ``Selector``, which didn't have ``setItemData`` — so building ANY
rule chip raised AttributeError on Subsonic/Navidrome (add-rule and
edit-playlist both dead), while Jellyfin (empty unsupported set) never
executed the line and stayed green. These tests run with a stubbed
provider so the Subsonic path executes without a server.
"""

from __future__ import annotations

from PySide6.QtCore import Qt

from jellytoast import providers
from jellytoast.smart_playlist_editor import _RuleChip


class _StubSubsonicProvider:
    kind = "subsonic"
    unsupported_smart_fields = frozenset({"rating", "last_played"})


def test_rule_chip_builds_with_unsupported_fields(qapp, monkeypatch):
    monkeypatch.setattr(providers, "get_provider", lambda: _StubSubsonicProvider())
    chip = _RuleChip(None)  # raised AttributeError before the Selector fix
    labels = [chip._field.itemText(i) for i in range(chip._field.count())]
    flagged = [lbl for lbl in labels if "not on this server" in lbl]
    assert len(flagged) == 2, f"rating + last_played should be flagged, got {flagged}"


def test_unsupported_field_carries_tooltip(qapp, monkeypatch):
    monkeypatch.setattr(providers, "get_provider", lambda: _StubSubsonicProvider())
    chip = _RuleChip(None)
    field = chip._field
    tips = {
        field.itemData(i): field.itemData(i, Qt.ItemDataRole.ToolTipRole)
        for i in range(field.count())
    }
    assert tips["rating"] and "never matches" in tips["rating"]
    assert tips["last_played"] and "never matches" in tips["last_played"]
    # Supported fields carry no tooltip.
    assert tips.get("artist") is None


def test_selector_set_item_data_user_role_replaces_data(qapp):
    from jellytoast.selector import Selector

    s = Selector()
    s.addItem("A", "a")
    s.setItemData(0, "b")
    assert s.itemData(0) == "b"
    s.setItemData(0, "tip text", Qt.ItemDataRole.ToolTipRole)
    assert s.itemData(0) == "b"  # tooltip write must not clobber data
    assert s.itemData(0, Qt.ItemDataRole.ToolTipRole) == "tip text"
    # Out-of-range writes are a no-op, not a crash.
    s.setItemData(5, "x")
