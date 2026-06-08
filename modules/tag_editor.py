"""Single-track tag editor — the "Edit tags…" dialog.

A modal form over the seven server-editable fields. Jellyfin is the
only backend with a metadata-write endpoint, so callers gate the
affordance on ``provider.can_edit_metadata`` *and*
``provider.can_edit_metadata_on_account()`` before opening this.

The Save path sends only the *changed* fields to
``provider.update_track_metadata`` — that keeps the LockedFields set
(the jellyfin#10724 refresh-revert workaround) scoped to what the user
actually touched.

Two finishers landed alongside the original form (see
docs/research/tag_editing.md §4):

- **Cover-picker.** Small preview pane + "Replace cover…" button that
  opens a QFileDialog for png/jpg/jpeg/webp. Targets the track's
  AlbumId so the swap is visible in every surface that reads the
  album cover (songs view, artist page, now-playing). Upload runs
  after the metadata write succeeds, via the same async worker.
- **Bulk "Apply to whole album".** Checkbox at the bottom (gated on
  AlbumId presence) that routes Save through
  ``provider.update_album_track_metadata``. Adds a confirm step
  summarizing the track count + edit fields so a checked box never
  silently rewrites N tracks.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from modules.async_io import run_async
from modules.design_tokens import SPACE_LG, SPACE_SM, TYPE_CAPTION, type_qss
from modules.providers import get_provider

COVER_PREVIEW_PX = 120
# Jellyfin's image endpoint accepts these via the same POST body shape;
# the suffix → mime map below is what the file picker filter mirrors.
_SUPPORTED_COVER_EXTS = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}


def _csv(values: Any) -> str:
    """Render a list field (Artists / Genres) as comma-separated text."""
    return ", ".join(str(v) for v in (values or []))


def _parse_csv(text: str) -> list[str]:
    """Parse comma-separated text back into a trimmed, blank-free list."""
    return [part.strip() for part in (text or "").split(",") if part.strip()]


def _mime_from_path(path: str) -> str:
    """Map a chosen file's suffix to the mime type the Jellyfin upload
    endpoint expects in its ``Content-Type`` header. Unknown extensions
    fall back to image/jpeg — Jellyfin re-encodes on its side and the
    QFileDialog filter is already restricting to the supported set."""
    return _SUPPORTED_COVER_EXTS.get(Path(path).suffix.lower(), "image/jpeg")


class TagEditorDialog(QDialog):
    """Edit one track's tags. Construct with the raw provider item dict
    (the same shape the context menus carry); ``exec()`` returns
    ``Accepted`` once a save succeeds."""

    def __init__(self, track: Dict[str, Any], parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._track = dict(track or {})
        self._item_id = str(self._track.get("Id") or "")
        # Both casings — Jellyfin items carry AlbumId; the offline /
        # Subsonic shapes use albumId. The cover-picker + bulk-edit
        # surfaces gate on this being truthy.
        self._album_id = str(
            self._track.get("AlbumId") or self._track.get("albumId") or ""
        )
        self._new_cover_bytes: Optional[bytes] = None
        self._new_cover_mime: str = ""

        self.setWindowTitle("Edit tags")
        self.setModal(True)
        self.setMinimumWidth(520)
        # Live-accent: re-stamp baked QSS on theme_changed. With
        # theme_mode=="auto" (follow-OS) an OS dark/light flip fires
        # theme_changed globally even while this modal is up, so without a
        # re-stamp the dialog freezes in the old palette. `_tinted` collects
        # the colour-only child widgets so _reapply_accent can recolour them
        # (a blanket findChildren would wipe the cover frame's bg/border).
        # See architecture_live_accent.md.
        self._tinted: list = []
        self._apply_dialog_qss()

        outer = QVBoxLayout(self)
        outer.setContentsMargins(SPACE_LG, SPACE_LG, SPACE_LG, SPACE_LG)
        outer.setSpacing(SPACE_SM)

        # Cover preview on the left, form on the right — gives the
        # dialog visual anchor and matches the song-row mental model
        # ("here's the song; here are its tags").
        top = QHBoxLayout()
        top.setSpacing(SPACE_LG)
        top.addLayout(self._build_cover_column(), 0)
        top.addLayout(self._build_form_column(), 1)
        outer.addLayout(top)

        from modules.ui_helpers import TEXT_DIM as _TEXT_DIM
        from modules.ui_helpers import TEXT_FAINT as _TEXT_FAINT

        hint = QLabel("Artists and Genres are comma-separated. 0 leaves a number unset.")
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color: {_TEXT_FAINT}; {type_qss(TYPE_CAPTION)}")
        self._tinted.append((hint, "TEXT_FAINT"))
        outer.addWidget(hint)

        # Bulk affordance — visible only when we know an album id. A
        # song with no AlbumId (rare; mostly local-test fixtures) gets
        # the single-track flow only, never a checkbox we can't honor.
        self._bulk_check: Optional[QCheckBox] = None
        if self._album_id:
            self._bulk_check = QCheckBox("Apply changes to all tracks on this album")
            self._bulk_check.setStyleSheet(
                f"QCheckBox {{ color: {_TEXT_DIM}; {type_qss(TYPE_CAPTION)} }}"
            )
            self._tinted.append((self._bulk_check, "TEXT_DIM"))
            outer.addWidget(self._bulk_check)

        self._status = QLabel("")
        self._status.setWordWrap(True)
        self._status.setStyleSheet(f"color: {_TEXT_DIM}; {type_qss(TYPE_CAPTION)}")
        self._tinted.append((self._status, "TEXT_DIM"))
        outer.addWidget(self._status)

        self._buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        self._buttons.accepted.connect(self._on_save)
        self._buttons.rejected.connect(self.reject)
        outer.addWidget(self._buttons)

        self._load_existing_cover()

        from modules.player_state import PlayerBus

        PlayerBus.get().theme_changed.connect(self._reapply_accent)

    # ── Live-accent ──────────────────────────────────────────────────────

    def _apply_dialog_qss(self) -> None:
        """Stamp the dialog-level QSS (background + QLineEdit/QSpinBox) from
        the current ui_helpers tokens. Read fresh so a theme swap takes
        effect; see architecture_live_accent.md."""
        from modules import ui_helpers as _ui

        self.setStyleSheet(
            f"QDialog {{ background: {_ui.BG}; }} "
            f"QLineEdit, QSpinBox {{ background: {_ui.ink_alpha(0.06)}; "
            f"color: {_ui.TEXT}; border: 1px solid {_ui.BORDER}; border-radius: 6px; "
            f"padding: 6px 9px; }} "
            f"QLineEdit:focus, QSpinBox:focus {{ border-color: {_ui.ink_alpha(0.32)}; }}"
        )

    def _reapply_accent(self) -> None:
        from modules import ui_helpers as _ui

        self._apply_dialog_qss()
        for widget, token in self._tinted:
            try:
                widget.setStyleSheet(
                    f"color: {getattr(_ui, token)}; {type_qss(TYPE_CAPTION)}"
                )
            except RuntimeError:
                pass  # widget deleted
        # Cover frame bg/border + (if no art yet) the placeholder text colour.
        try:
            self._cover_label.setStyleSheet(
                f"background: {_ui.ink_alpha(0.06)}; "
                f"border: 1px solid {_ui.BORDER}; border-radius: 6px;"
            )
            pm = self._cover_label.pixmap()
            if pm is None or pm.isNull():
                self._render_cover_placeholder()
        except RuntimeError:
            pass

    # ── UI construction ─────────────────────────────────────────────────

    def _build_cover_column(self) -> QVBoxLayout:
        col = QVBoxLayout()
        col.setSpacing(SPACE_SM)

        from modules.ui_helpers import BORDER as _BORDER
        from modules.ui_helpers import ink_alpha as _ink_alpha

        self._cover_label = QLabel()
        self._cover_label.setFixedSize(COVER_PREVIEW_PX, COVER_PREVIEW_PX)
        self._cover_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._cover_label.setFrameShape(QFrame.Shape.NoFrame)
        self._cover_label.setStyleSheet(
            f"background: {_ink_alpha(0.06)}; "
            f"border: 1px solid {_BORDER}; border-radius: 6px;"
        )
        # The fallback chip until the network reply lands (or forever,
        # if the album has no cover and the user is uploading a fresh
        # one).
        self._render_cover_placeholder()
        col.addWidget(self._cover_label, 0, Qt.AlignmentFlag.AlignTop)

        self._cover_button = QPushButton("Replace cover…")
        self._cover_button.clicked.connect(self._on_pick_cover)
        # No album id → no cover endpoint to write to. Disable rather
        # than hide so the layout stays stable across track types.
        self._cover_button.setEnabled(bool(self._album_id))
        col.addWidget(self._cover_button)
        col.addStretch(1)
        return col

    def _build_form_column(self) -> QFormLayout:
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
        return form

    def _label(self, text: str) -> QLabel:
        from modules.ui_helpers import TEXT_DIM as _TEXT_DIM

        lbl = QLabel(text)
        lbl.setStyleSheet(f"color: {_TEXT_DIM}; {type_qss(TYPE_CAPTION)}")
        self._tinted.append((lbl, "TEXT_DIM"))
        return lbl

    # ── Cover preview ───────────────────────────────────────────────────

    def _render_cover_placeholder(self) -> None:
        from modules.ui_helpers import TEXT_FAINT as _TEXT_FAINT

        self._cover_label.setText("No cover")
        self._cover_label.setStyleSheet(
            self._cover_label.styleSheet()
            + f" QLabel {{ color: {_TEXT_FAINT}; {type_qss(TYPE_CAPTION)} }}"
        )

    def _set_cover_pixmap(self, pix: QPixmap) -> None:
        if pix.isNull():
            self._render_cover_placeholder()
            return
        scaled = pix.scaled(
            COVER_PREVIEW_PX,
            COVER_PREVIEW_PX,
            Qt.AspectRatioMode.KeepAspectRatioByExpanding,
            Qt.TransformationMode.SmoothTransformation,
        )
        self._cover_label.setText("")
        self._cover_label.setPixmap(scaled)

    def _load_existing_cover(self) -> None:
        """Async-fetch the current album cover into the preview pane.
        Best-effort — if the request fails the placeholder stays."""
        if not self._album_id:
            return
        try:
            prov = get_provider()
            url = prov.get_image_url(self._album_id, "Primary", COVER_PREVIEW_PX * 2)
        except Exception:
            return
        if not url:
            return
        from modules.ui_helpers import load_image_async

        def _on_existing(pix):
            # If the user picked a replacement while this fetch was in flight,
            # don't clobber their pick with the old album cover.
            if self._new_cover_bytes is not None:
                return
            self._set_cover_pixmap(pix)

        load_image_async(
            key=f"tag-editor-cover:{self._album_id}",
            url=url,
            target_w=COVER_PREVIEW_PX,
            target_h=COVER_PREVIEW_PX,
            callback=_on_existing,
            on_error=lambda: None,
            priority="high",
        )

    def _on_pick_cover(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Choose cover image",
            "",
            "Images (*.png *.jpg *.jpeg *.webp)",
        )
        if not path:
            return
        try:
            data = Path(path).read_bytes()
        except OSError as exc:
            self._status.setText(f"Couldn't read that file: {exc}")
            return
        # 25 MB is a generous ceiling. Jellyfin will base64-encode the
        # body which inflates the wire payload ~33%; anything over this
        # is almost certainly a mistake (RAW from a camera, video file).
        if len(data) > 25 * 1024 * 1024:
            self._status.setText(
                "That image is over 25 MB — pick something smaller."
            )
            return
        self._new_cover_bytes = data
        self._new_cover_mime = _mime_from_path(path)
        pix = QPixmap()
        if not pix.loadFromData(data):
            self._status.setText("Couldn't decode that image — try a different file.")
            self._new_cover_bytes = None
            self._new_cover_mime = ""
            return
        self._set_cover_pixmap(pix)
        self._status.setText("Cover will be uploaded when you click Save.")

    # ── Edit collection + save ──────────────────────────────────────────

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

    def _bulk_requested(self) -> bool:
        return bool(self._bulk_check and self._bulk_check.isChecked())

    def _confirm_bulk(self, edits: Dict[str, Any]) -> bool:
        """Block on a Yes/No confirm before rewriting every track on
        the album. Skipped when bulk isn't requested."""
        fields = ", ".join(sorted(edits.keys())) if edits else "(none)"
        msg = (
            f"Apply these field changes to every track on the album "
            f'"{self._track.get("Album") or self._album_id}"?\n\n'
            f"Fields: {fields}\n\n"
            "Per-track cover art will not be changed by this bulk write."
        )
        from modules.frosted_dialog import frosted_confirm

        return frosted_confirm(
            self,
            "Apply to whole album",
            msg,
            confirm_text="Apply",
        )

    def _on_save(self) -> None:
        if not self._item_id:
            self._status.setText("This track has no id — can't save.")
            return
        edits = self.collect_edits()
        has_cover = self._new_cover_bytes is not None
        bulk = self._bulk_requested()

        if not edits and not has_cover:
            # Nothing changed — close as a cancel rather than firing a
            # no-op write.
            self.reject()
            return

        if bulk and edits and not self._confirm_bulk(edits):
            return

        self._buttons.setEnabled(False)
        self._status.setText("Saving…")

        prov = get_provider()
        item_id = self._item_id
        album_id = self._album_id
        cover_bytes = self._new_cover_bytes
        cover_mime = self._new_cover_mime

        def _go():
            # Metadata write first — if the server rejects it we want
            # the user to see the failure before any cover upload runs.
            if edits:
                if bulk and album_id:
                    result = prov.update_album_track_metadata(album_id, edits)
                else:
                    result = prov.update_track_metadata(item_id, edits)
            else:
                result = {}
            if cover_bytes is not None and album_id:
                # Cover targets the album — what the songs view, artist
                # page, and now-playing surface all read. A per-track
                # cover override would only ever show on the track's
                # own item card, which isn't a surface we use.
                #
                # Cover failures are surfaced *separately* from
                # metadata failures: a cover-only error must not read
                # as "metadata edit rejected" since the metadata
                # phase already succeeded. Stash the error on the
                # result dict and let _on_saved branch on it.
                try:
                    prov.upload_cover_art(album_id, cover_bytes, cover_mime)
                except Exception as e:
                    if not isinstance(result, dict):
                        result = {}
                    result["_cover_error"] = f"{type(e).__name__}: {e}"
            return result

        run_async(_go, on_result=self._on_saved, on_error=self._on_save_error)

    def _on_saved(self, result: Any) -> None:
        # Partial-failure surfacing: the bulk path returns a summary
        # dict with per-track failures; the cover-upload path stashes
        # its own error under _cover_error. Surface whichever fired —
        # both, if both did.
        cover_error: Optional[str] = None
        failed_list: list = []
        n_total = 0
        if isinstance(result, dict):
            ce = result.get("_cover_error")
            cover_error = ce if isinstance(ce, str) and ce else None
            fl = result.get("failed")
            failed_list = fl if isinstance(fl, list) else []
            n_total = result.get("total") or 0

        n_failed = len(failed_list)
        if failed_list and cover_error:
            self._buttons.setEnabled(True)
            self._status.setText(
                f"Saved {n_total - n_failed} of {n_total} tracks "
                f"({n_failed} failed); cover upload also failed."
            )
            return
        if failed_list:
            self._buttons.setEnabled(True)
            self._status.setText(
                f"Saved {n_total - n_failed} of {n_total} tracks — "
                f"{n_failed} failed. See server logs."
            )
            return
        if cover_error:
            self._buttons.setEnabled(True)
            self._status.setText(
                "Metadata saved, but the cover upload failed."
            )
            return
        self.accept()

    def _on_save_error(self, _exc) -> None:
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
