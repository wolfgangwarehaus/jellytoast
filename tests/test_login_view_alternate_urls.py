"""Coverage for ``modules.login_view._UrlRow`` + ``_AlternateUrlsDialog``.

The dialog exposes a list of alternate server URLs (Tailscale name,
LAN IP, public hostname) the connectivity engine fails over to. The
backend setter (``Settings.server_hostnames``) re-validates, so the
dialog just gathers — but its own behaviour matters:

* Pre-populated rows reflect ``settings.server_hostnames``.
* ``+ Add URL`` and the per-row × button create / drop rows.
* On Save: blank-URL rows are dropped, label is preserved, priority
  is assigned by row order (so a drag-reorder would in principle
  re-prioritise — currently there's no drag UI but the contract is
  ordinal).
* On Cancel: nothing is written.

Widget tests need a QApplication — supplied via the ``qapp`` session
fixture in conftest.py.
"""

from __future__ import annotations

import pytest

from modules.login_view import _AlternateUrlsDialog, _UrlRow


class _FakeSettings:
    """In-memory stand-in for ``Settings`` with just the
    ``server_hostnames`` getter/setter the dialog touches. Records
    writes so a test can assert on what landed."""

    def __init__(self, initial: list | None = None):
        self.server_hostnames = list(initial or [])


# ── _UrlRow ─────────────────────────────────────────────────────────────


def test_url_row_builds_with_initial_values(qapp):
    captures: list = []
    row = _UrlRow(captures.append, url="http://nas.lan", label="LAN")
    assert row.url_edit.text() == "http://nas.lan"
    assert row.label_edit.text() == "LAN"


def test_url_row_remove_button_invokes_callback(qapp):
    captures: list = []
    row = _UrlRow(captures.append, url="x", label="y")
    # The remove × button is the third child of the row's HBoxLayout.
    layout = row.layout()
    remove_btn = layout.itemAt(2).widget()
    remove_btn.click()
    assert captures == [row]


# ── _AlternateUrlsDialog ────────────────────────────────────────────────


def test_dialog_starts_with_no_rows_when_settings_empty(qapp):
    settings = _FakeSettings(initial=[])
    dlg = _AlternateUrlsDialog(settings)
    assert dlg._rows == []


def test_dialog_prepopulates_from_settings(qapp):
    settings = _FakeSettings(
        initial=[
            {"url": "http://nas.lan", "label": "LAN", "priority": 0},
            {"url": "http://nas.ts", "label": "Tailscale", "priority": 1},
        ]
    )
    dlg = _AlternateUrlsDialog(settings)
    assert len(dlg._rows) == 2
    assert dlg._rows[0].url_edit.text() == "http://nas.lan"
    assert dlg._rows[0].label_edit.text() == "LAN"
    assert dlg._rows[1].url_edit.text() == "http://nas.ts"
    assert dlg._rows[1].label_edit.text() == "Tailscale"


def test_add_row_appends(qapp):
    settings = _FakeSettings()
    dlg = _AlternateUrlsDialog(settings)
    dlg._add_row("http://new.example", "new")
    assert len(dlg._rows) == 1
    assert dlg._rows[0].url_edit.text() == "http://new.example"


def test_remove_row_drops_target(qapp):
    settings = _FakeSettings(
        initial=[
            {"url": "http://a", "label": "a", "priority": 0},
            {"url": "http://b", "label": "b", "priority": 1},
            {"url": "http://c", "label": "c", "priority": 2},
        ]
    )
    dlg = _AlternateUrlsDialog(settings)
    middle = dlg._rows[1]
    dlg._remove_row(middle)
    assert len(dlg._rows) == 2
    assert [r.url_edit.text() for r in dlg._rows] == ["http://a", "http://c"]


def test_remove_row_unknown_is_idempotent(qapp):
    """Removing a row that was never in the dialog must not raise —
    keeps the contract loose for any future double-clicks on the ×
    button after a fast remove."""
    settings = _FakeSettings()
    dlg = _AlternateUrlsDialog(settings)
    # Construct a row not owned by the dialog.
    orphan = _UrlRow(lambda r: None, url="x")
    dlg._remove_row(orphan)  # no raise
    assert dlg._rows == []


# ── _on_accept ──────────────────────────────────────────────────────────


def test_accept_drops_blank_url_rows(qapp):
    settings = _FakeSettings()
    dlg = _AlternateUrlsDialog(settings)
    dlg._add_row("", "")  # blank — should be dropped
    dlg._add_row("http://kept.example", "kept")
    dlg._add_row("   ", "whitespace only")  # whitespace strips to blank → dropped
    dlg._on_accept()
    saved = settings.server_hostnames
    assert len(saved) == 1
    assert saved[0]["url"] == "http://kept.example"
    assert saved[0]["label"] == "kept"


def test_accept_assigns_priority_by_row_order(qapp):
    """Priority is the row's position in the cleaned (blanks-dropped)
    list, not the raw row index — the connectivity engine walks the
    list in this order, so a drag-reorder UI could re-stamp these."""
    settings = _FakeSettings()
    dlg = _AlternateUrlsDialog(settings)
    dlg._add_row("http://first", "first")
    dlg._add_row("", "blank")  # dropped, mustn't take a priority slot
    dlg._add_row("http://second", "second")
    dlg._add_row("http://third", "third")
    dlg._on_accept()
    saved = settings.server_hostnames
    assert [e["priority"] for e in saved] == [0, 1, 2]
    assert [e["url"] for e in saved] == [
        "http://first",
        "http://second",
        "http://third",
    ]


def test_accept_preserves_label(qapp):
    settings = _FakeSettings()
    dlg = _AlternateUrlsDialog(settings)
    dlg._add_row("http://x.example", "Office VPN")
    dlg._on_accept()
    saved = settings.server_hostnames
    assert saved[0]["label"] == "Office VPN"


def test_accept_strips_whitespace_from_inputs(qapp):
    settings = _FakeSettings()
    dlg = _AlternateUrlsDialog(settings)
    dlg._add_row("  http://nas.lan  ", "  LAN  ")
    dlg._on_accept()
    saved = settings.server_hostnames
    assert saved[0]["url"] == "http://nas.lan"
    assert saved[0]["label"] == "LAN"


def test_reject_does_not_write_settings(qapp):
    """Cancel path: the dialog never calls _on_accept, so the settings
    object stays untouched even if the user added rows."""
    settings = _FakeSettings(
        initial=[{"url": "http://original", "label": "orig", "priority": 0}]
    )
    dlg = _AlternateUrlsDialog(settings)
    dlg._add_row("http://transient", "tmp")
    dlg.reject()
    # server_hostnames untouched — still the initial value.
    assert settings.server_hostnames == [
        {"url": "http://original", "label": "orig", "priority": 0}
    ]


def test_accept_with_only_blank_rows_clears_settings(qapp):
    """If every row is blank, the dialog saves an empty list — which
    is the canonical single-URL state."""
    settings = _FakeSettings(
        initial=[{"url": "http://old", "label": "old", "priority": 0}]
    )
    dlg = _AlternateUrlsDialog(settings)
    # Initial row from settings is non-empty; clear it.
    dlg._rows[0].url_edit.setText("")
    dlg._on_accept()
    assert settings.server_hostnames == []
