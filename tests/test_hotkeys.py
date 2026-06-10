"""Tests for jellytoast.hotkeys — the rebindable shortcut registry.

These cover the data shape (registry entries) and the QSettings round-
trip helpers. ``install_shortcuts`` itself wires real QShortcuts to a
QWidget parent and needs a Qt event loop to fire, so it's not tested
here — the registry data is the part we care about correctness-wise.
"""


import pytest
from PySide6.QtCore import QSettings
from PySide6.QtGui import QKeySequence

from jellytoast import hotkeys

REQUIRED_KEYS = {"action_id", "default_seq", "label", "callable", "context"}


class _FakeMainWindow:
    """Stand-in for the real main window. Each method is a no-op the
    registry lambdas can close over; the lambdas aren't invoked during
    these tests, just constructed."""

    def __init__(self):
        self.calls: list[str] = []

    def _show_search_view(self):
        self.calls.append("search")

    def _show_native_music_grid(self):
        self.calls.append("library")

    def close(self):
        self.calls.append("close")

    def _open_currently_playing_album(self):
        self.calls.append("album")


@pytest.fixture(autouse=True)
def _reset_hotkey_settings(monkeypatch):
    """Each test gets a clean QSettings slate. QStandardPaths is already
    redirected by conftest.setTestModeEnabled, but pre-existing keys
    from a sibling test can leak across — wipe ``hotkeys/*`` per test."""
    # Force a fresh QSettings handle in case conftest's test-mode
    # redirect kicked in after a prior cached handle.
    hotkeys._reset_settings_cache()
    qs = QSettings("jellytoast", "jellytoast")
    qs.beginGroup("hotkeys")
    qs.remove("")
    qs.endGroup()
    qs.sync()
    yield
    qs.beginGroup("hotkeys")
    qs.remove("")
    qs.endGroup()
    qs.sync()
    hotkeys._reset_settings_cache()


def test_build_registry_returns_expected_entries():
    mw = _FakeMainWindow()
    reg = hotkeys.build_registry(mw)
    # Four entries by default; the JT_NATIVE_ALBUM opt-in adds a fifth
    # and is checked separately below.
    assert len(reg) == 4
    ids = [e["action_id"] for e in reg]
    assert ids == ["all_music", "search_find", "search_slash", "quit"]


def test_build_registry_with_jt_native_album_env(monkeypatch):
    monkeypatch.setenv("JT_NATIVE_ALBUM", "1")
    mw = _FakeMainWindow()
    reg = hotkeys.build_registry(mw)
    assert len(reg) == 5
    assert reg[-1]["action_id"] == "native_album"
    assert reg[-1]["default_seq"] == "Ctrl+Shift+A"


def test_every_entry_has_all_required_keys():
    mw = _FakeMainWindow()
    for entry in hotkeys.build_registry(mw):
        missing = REQUIRED_KEYS - set(entry.keys())
        assert not missing, f"entry {entry.get('action_id')!r} missing keys: {missing}"
        assert callable(entry["callable"])
        assert isinstance(entry["default_seq"], str)
        assert isinstance(entry["label"], str)
        assert entry["context"] in {"global", "search"}


def test_default_sequences_match_originals():
    """The whole point of this refactor is zero behavior change. Lock
    in the exact defaults so a future edit to the registry can't
    silently retire a shortcut."""
    mw = _FakeMainWindow()
    reg = {e["action_id"]: e["default_seq"] for e in hotkeys.build_registry(mw)}
    assert reg["all_music"] == "Ctrl+Shift+L"
    assert reg["search_find"] == "Ctrl+F"
    assert reg["search_slash"] == "/"
    assert reg["quit"] == "Ctrl+Q"


def test_get_sequence_returns_default_when_no_override():
    seq = hotkeys.get_sequence("search_find", "Ctrl+F")
    assert seq == QKeySequence("Ctrl+F")


def test_get_sequence_falls_back_to_registry_default_with_no_arg():
    # No override, no explicit default — the function should look up
    # the entry in the registry and return its default.
    seq = hotkeys.get_sequence("quit")
    assert seq == QKeySequence("Ctrl+Q")


def test_set_sequence_round_trips():
    hotkeys.set_sequence("search_find", "Ctrl+K")
    seq = hotkeys.get_sequence("search_find", "Ctrl+F")
    assert seq == QKeySequence("Ctrl+K")


def test_set_sequence_accepts_qkeysequence():
    hotkeys.set_sequence("search_find", QKeySequence("Ctrl+J"))
    assert hotkeys.get_sequence("search_find", "Ctrl+F") == QKeySequence("Ctrl+J")


def test_reset_removes_override():
    hotkeys.set_sequence("search_find", "Ctrl+K")
    assert hotkeys.get_sequence("search_find", "Ctrl+F") == QKeySequence("Ctrl+K")
    hotkeys.reset("search_find")
    # Back to default.
    assert hotkeys.get_sequence("search_find", "Ctrl+F") == QKeySequence("Ctrl+F")


def test_reset_all_clears_every_hotkey_entry():
    hotkeys.set_sequence("search_find", "Ctrl+K")
    hotkeys.set_sequence("quit", "Ctrl+Shift+Q")
    hotkeys.set_sequence("all_music", "Ctrl+L")
    hotkeys.reset_all()
    # All three return the registry default again.
    assert hotkeys.get_sequence("search_find", "Ctrl+F") == QKeySequence("Ctrl+F")
    assert hotkeys.get_sequence("quit", "Ctrl+Q") == QKeySequence("Ctrl+Q")
    assert hotkeys.get_sequence("all_music", "Ctrl+Shift+L") == QKeySequence("Ctrl+Shift+L")


def test_reset_all_leaves_other_settings_groups_intact():
    """``hotkeys/*`` wipe must not touch unrelated QSettings keys."""
    qs = QSettings("jellytoast", "jellytoast")
    # Remove these keys on teardown. Under setTestModeEnabled the whole
    # process shares ONE QSettings store, so a leaked ``server/url``
    # leaks into every later test — and SettingsDialog auto-probes the
    # saved URL on construction (``run_async(provider.probe, …)``), so a
    # dead ``http://example.test`` left here makes an unrelated later
    # test fire a real ``requests.get`` on a pool worker that can SIGSEGV
    # mid-GC. Surfaced under random order. Clean up so nothing leaks.
    try:
        qs.setValue("playback/volume", 73)
        qs.setValue("server/url", "http://example.test")
        qs.sync()

        hotkeys.set_sequence("search_find", "Ctrl+K")
        hotkeys.reset_all()

        qs2 = QSettings("jellytoast", "jellytoast")
        assert qs2.value("playback/volume", type=int) == 73
        assert qs2.value("server/url", type=str) == "http://example.test"
        # And the hotkey group really is gone.
        assert qs2.value("hotkeys/search_find", "", type=str) == ""
    finally:
        qs.remove("playback/volume")
        qs.remove("server/url")
        qs.sync()


def test_set_sequence_with_empty_string_clears_override():
    hotkeys.set_sequence("search_find", "Ctrl+K")
    hotkeys.set_sequence("search_find", "")
    # Falls back to registry default.
    assert hotkeys.get_sequence("search_find", "Ctrl+F") == QKeySequence("Ctrl+F")


# ── registry_actions / conflict detection (Settings → Hotkeys page) ──────────


def test_registry_actions_omits_callables():
    actions = hotkeys.registry_actions()
    assert [a["action_id"] for a in actions] == [
        "all_music",
        "search_find",
        "search_slash",
        "quit",
    ]
    for a in actions:
        assert set(a.keys()) == {"action_id", "default_seq", "label", "context"}


def test_current_bindings_reflects_defaults():
    binds = hotkeys.current_bindings()
    assert binds["search_find"] == "Ctrl+F"
    assert binds["all_music"] == "Ctrl+Shift+L"


def test_current_bindings_reflects_override():
    hotkeys.set_sequence("search_find", "Ctrl+K")
    assert hotkeys.current_bindings()["search_find"] == "Ctrl+K"


def test_find_conflict_detects_a_clash():
    # Binding all_music to Ctrl+F clashes with search_find's default.
    assert hotkeys.find_conflict("all_music", "Ctrl+F") == "search_find"


def test_find_conflict_none_when_unique():
    assert hotkeys.find_conflict("all_music", "Ctrl+Alt+Z") is None


def test_find_conflict_ignores_the_same_action():
    # An action never conflicts with its own current binding.
    assert hotkeys.find_conflict("search_find", "Ctrl+F") is None


def test_find_conflict_empty_sequence_never_conflicts():
    assert hotkeys.find_conflict("all_music", "") is None


def test_find_conflict_honours_a_passed_snapshot():
    # Mid-edit page state: search_find about to become Ctrl+J — binding
    # all_music to Ctrl+J should clash against that unsaved snapshot.
    snapshot = {"search_find": "Ctrl+J", "all_music": "Ctrl+Shift+L"}
    assert hotkeys.find_conflict("all_music", "Ctrl+J", bindings=snapshot) == "search_find"


# ── Settings → Hotkeys editable page (conflict revert / live emit) ───────────


class TestHotkeyEditorDialog:
    """The editable Settings page — _on_hotkey_edited's conflict-revert
    and the live hotkeys_changed emit."""

    def _dialog(self, qapp):
        from jellytoast.settings_dialog import SettingsDialog

        dlg = SettingsDialog()
        # Settings pages build lazily on first navigation — open the
        # Hotkeys page so _build_hotkeys() runs and _hotkey_edits
        # exists (in the real app the editor widgets these tests poke
        # are only reachable once the page is on screen anyway).
        for i in range(dlg.nav.count()):
            if dlg.nav.item(i).text() == "Hotkeys":
                dlg.nav.setCurrentRow(i)
                break
        return dlg

    def test_clean_rebind_persists_and_emits(self, qapp):
        from jellytoast.player_state import PlayerBus

        dlg = self._dialog(qapp)
        try:
            edit, warn, default = dlg._hotkey_edits["all_music"]
            fired: list[int] = []
            PlayerBus.get().hotkeys_changed.connect(lambda: fired.append(1))
            edit.setKeySequence(QKeySequence("Ctrl+Alt+Z"))
            dlg._on_hotkey_edited("all_music")
            # isHidden(), not isVisible(): the dialog is never show()n
            # in the test, so isVisible() is False for every child.
            assert warn.isHidden()
            assert hotkeys.get_sequence("all_music", default) == QKeySequence(
                "Ctrl+Alt+Z"
            )
            assert fired  # the main window's re-install trigger fired
        finally:
            dlg.deleteLater()

    def test_conflicting_rebind_reverts_and_warns(self, qapp):
        dlg = self._dialog(qapp)
        try:
            edit, warn, default = dlg._hotkey_edits["all_music"]
            # Ctrl+F already belongs to search_find.
            edit.setKeySequence(QKeySequence("Ctrl+F"))
            dlg._on_hotkey_edited("all_music")
            assert not warn.isHidden()  # conflict warning shown
            # Field snapped back to the default; nothing persisted.
            assert edit.keySequence() == QKeySequence("Ctrl+Shift+L")
            assert hotkeys.get_sequence("all_music", default) == QKeySequence(
                "Ctrl+Shift+L"
            )
        finally:
            dlg.deleteLater()

    def test_reset_all_restores_defaults(self, qapp):
        dlg = self._dialog(qapp)
        try:
            hotkeys.set_sequence("all_music", "Ctrl+Alt+Z")
            dlg._on_hotkeys_reset_all()
            edit, _warn, default = dlg._hotkey_edits["all_music"]
            assert edit.keySequence() == QKeySequence("Ctrl+Shift+L")
            assert hotkeys.get_sequence("all_music", default) == QKeySequence(
                "Ctrl+Shift+L"
            )
        finally:
            dlg.deleteLater()
