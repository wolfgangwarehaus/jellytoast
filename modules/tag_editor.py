"""Single-track tag editor — the "Edit tags…" dialog.

A modal form over the seven server-editable fields. Jellyfin is the
only backend with a metadata-write endpoint, so callers gate the
affordance on ``provider.can_edit_metadata`` *and*
``provider.can_edit_metadata_on_account()`` before opening this.

The Save path sends only the *changed* fields to
``provider.update_track_metadata`` — that keeps the LockedFields set
(the jellyfin#10724 refresh-revert workaround) scoped to what the user
actually touched. Cover-art upload is a separate follow-up and is not
part of this dialog.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from modules.async_io import run_async
from modules.design_tokens import SPACE_LG, SPACE_SM, TYPE_CAPTION, type_qss
from modules.providers import get_provider
from modules.ui_helpers import BG, BORDER, TEXT, TEXT_DIM, TEXT_FAINT, ink_alpha


def _csv(values: Any) -> str:
    """Render a list field (Artists / Genres) as comma-separated text."""
    return ", ".join(str(v) for v in (values or []))


def _parse_csv(text: str) -> list[str]:
    """Parse comma-separated text back into a trimmed, blank-free list."""
    return [part.strip() for part in (text or "").split(",") if part.strip()]


class TagEditorDialog(QDialog):
    """Edit one track's tags. Construct with the raw provider item dict
    (the same shape the context menus carry); ``exec()`` returns
    ``Accepted`` once a save succeeds."""

    def __init__(self, track: Dict[str, Any], parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._track = dict(track or {})
        self._item_id = str(self._track.get("Id") or "")
        self.setWindowTitle("Edit tags")
        self.setModal(True)
        self.setMinimumWidth(440)
        # Late-import theme constants so a settings-driven theme/accent
        # swap that landed since module import takes effect on the next
        # dialog opening — `from modules.ui_helpers import TEXT` at
        # module top freezes the binding to the load-time value, and
        # `ui_helpers.refresh_theme()` rebinds (doesn't mutate) the
        # source. Per architecture_live_accent.md / contract.
        from modules.ui_helpers import (
            BG as _BG,
            TEXT as _TEXT,
            BORDER as _BORDER,
            ink_alpha as _ink_alpha,
        )

        self.setStyleSheet(
            f"QDialog {{ background: {_BG}; }} "
            f"QLineEdit, QSpinBox {{ background: {_ink_alpha(0.06)}; "
            f"color: {_TEXT}; border: 1px solid {_BORDER}; border-radius: 6px; "
            f"padding: 6px 9px; }} "
            f"QLineEdit:focus, QSpinBox:focus {{ border-color: {_ink_alpha(0.32)}; }}"
        )

        outer = QVBoxLayout(self)
        outer.setContentsMargins(SPACE_LG, SPACE_LG, SPACE_LG, SPACE_LG)
        outer.setSpacing(SPACE_SM)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        form.setHorizontalSpacing(16)
        form.setVerticalSpacing(8)

        self._name = QLineEdit(str(self._track.get("Name") or ""))
        self._artists = QLineEdit(_csv(self._track.get("Artists")))
        self._album = QLineEdit(str(self._track.get("Album") or ""))
        self._album_artist = QLineEdit(str(self._track.get("AlbumArtist") or ""))
        self._genres = QLineEdit(_csv(self._track.get("Genres")))

        # 0 == "unset" for both numeric fields.
        self._track_no = QSpinBox()
        self._track_no.setRange(0, 9999)
        self._track_no.setValue(int(self._track.get("IndexNumber") or 0))
        self._year = QSpinBox()
        self._year.setRange(0, 9999)
        self._year.setValue(int(self._track.get("ProductionYear") or 0))

        form.addRow(self._label("Title"), self._name)
        form.addRow(self._label("Artists"), self._artists)
        form.addRow(self._label("Album"), self._album)
        form.addRow(self._label("Album artist"), self._album_artist)
        form.addRow(self._label("Genres"), self._genres)
        form.addRow(self._label("Track no."), self._track_no)
        form.addRow(self._label("Year"), self._year)
        outer.addLayout(form)

        hint = QLabel("Artists and Genres are comma-separated. 0 leaves a number unset.")
        hint.setWordWrap(True)
        from modules.ui_helpers import TEXT_FAINT as _TEXT_FAINT, TEXT_DIM as _TEXT_DIM

        hint.setStyleSheet(f"color: {_TEXT_FAINT}; {type_qss(TYPE_CAPTION)}")
        outer.addWidget(hint)

        self._status = QLabel("")
        self._status.setWordWrap(True)
        self._status.setStyleSheet(f"color: {_TEXT_DIM}; {type_qss(TYPE_CAPTION)}")
        outer.addWidget(self._status)

        self._buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        self._buttons.accepted.connect(self._on_save)
        self._buttons.rejected.connect(self.reject)
        outer.addWidget(self._buttons)

    def _label(self, text: str) -> QLabel:
        from modules.ui_helpers import TEXT_DIM as _TEXT_DIM

        lbl = QLabel(text)
        lbl.setStyleSheet(f"color: {_TEXT_DIM}; {type_qss(TYPE_CAPTION)}")
        return lbl

    def collect_edits(self) -> Dict[str, Any]:
        """The fields whose form value differs from the track's current
        value — only these are sent, so unrelated fields aren't locked."""
        edits: Dict[str, Any] = {}

        name = self._name.text().strip()
        if name != str(self._track.get("Name") or ""):
            edits["Name"] = name

        artists = _parse_csv(self._artists.text())
        if artists != [str(a) for a in (self._track.get("Artists") or [])]:
            edits["Artists"] = artists

        album = self._album.text().strip()
        if album != str(self._track.get("Album") or ""):
            edits["Album"] = album

        album_artist = self._album_artist.text().strip()
        if album_artist != str(self._track.get("AlbumArtist") or ""):
            edits["AlbumArtist"] = album_artist

        genres = _parse_csv(self._genres.text())
        if genres != [str(g) for g in (self._track.get("Genres") or [])]:
            edits["Genres"] = genres

        track_no = self._track_no.value()
        if track_no != int(self._track.get("IndexNumber") or 0):
            edits["IndexNumber"] = track_no

        year = self._year.value()
        if year != int(self._track.get("ProductionYear") or 0):
            edits["ProductionYear"] = year

        return edits

    def _on_save(self):
        if not self._item_id:
            self._status.setText("This track has no id — can't save.")
            return
        edits = self.collect_edits()
        if not edits:
            # Nothing changed — close as a cancel rather than firing a
            # no-op write.
            self.reject()
            return
        self._buttons.setEnabled(False)
        self._status.setText("Saving…")

        def _go():
            return get_provider().update_track_metadata(self._item_id, edits)

        run_async(_go, on_result=self._on_saved, on_error=self._on_save_error)

    def _on_saved(self, _merged):
        self.accept()

    def _on_save_error(self, _exc):
        self._buttons.setEnabled(True)
        self._status.setText(
            "Couldn't save — the server rejected the edit (admin rights "
            "are required to edit metadata on Jellyfin)."
        )


def open_tag_editor(track: Dict[str, Any], parent: Optional[QWidget] = None) -> bool:
    """Open the tag editor for ``track``. Returns True when a save was
    committed, False on cancel / no-change."""
    dlg = TagEditorDialog(track, parent)
    accepted = dlg.exec() == QDialog.DialogCode.Accepted
    dlg.deleteLater()
    return accepted
