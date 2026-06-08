"""Internet-radio library surface.

A standalone page reached from the top bar's library dropdown ("Radio"
tab). Lists every station the active provider knows about — Subsonic's
``getInternetRadioStations`` for Navidrome / Airsonic / Gonic, the
QSettings-backed local store for Jellyfin (which has no native concept).
Clicking a row installs a single-item ``INTERNET_RADIO`` queue; the
bar / page swap their scrubber for the elapsed + LIVE pip treatment.

Add / Edit / Delete share one form dialog (`_StationFormDialog`). The
provider abstraction surface is uniform — both backends implement
``get/create/update/delete_internet_radio_station`` with the same dict
shape (``{id, name, streamUrl, homePageUrl}``) — so the page never
branches on provider kind. A provider raising ``NotImplementedError``
(e.g. a future read-only backend) degrades the buttons cleanly via a
small inline message instead of crashing.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from modules.async_io import run_async
from modules.design_tokens import (
    RADIUS_LG,
    SPACE_LG,
    SPACE_MD,
    SPACE_SM,
    SPACE_XL,
    TYPE_BODY,
    TYPE_CAPTION,
    type_qss,
)
from modules.frosted_dialog import (
    FrostedDialog,
    frosted_confirm,
    frosted_info,
    frosted_warning,
)
from modules.player_state import PlayerBus, QueueContext, QueueKind
from modules.providers import get_provider
from modules.radio_presets import POPULAR_STATIONS, category_order, logo_url_for_stream
from modules.ui_helpers import (
    BG_CARD,
    TEXT,
    TEXT_DIM,
    TEXT_FAINT,
    install_autofade_scrollbars,
)

# ── Add / Edit dialog ───────────────────────────────────────────────────────


class _StationFormDialog(FrostedDialog):
    """Modal form for creating or editing a station. Name + Stream URL
    are required; Home page URL is optional. The dialog only validates
    presence + basic URL shape — the round-trip to the provider is
    where real validation (admin permission, server reachability)
    happens, and any failure surfaces back to the caller as a raised
    exception we convert into a frosted alert. App-styled (frosted body +
    GLOBAL_STYLE) so it matches the active theme."""

    def __init__(
        self,
        parent: Optional[QWidget] = None,
        station: Optional[Dict] = None,
    ):
        is_edit = bool(station)
        super().__init__(
            parent,
            title="Edit station" if is_edit else "Add station",
            min_width=420,
        )
        from modules import ui_helpers as _u

        self._station = station or {}

        v = self.content_layout
        v.setContentsMargins(SPACE_LG, SPACE_SM, SPACE_LG, SPACE_LG)
        v.setSpacing(SPACE_SM)

        def _label(text: str) -> QLabel:
            lab = QLabel(text)
            lab.setStyleSheet(
                f"{type_qss(TYPE_CAPTION)} color: {_u.TEXT_DIM}; "
                "background: transparent;"
            )
            return lab

        v.addWidget(_label("Name"))
        self._name = QLineEdit(str(self._station.get("name") or ""))
        self._name.setPlaceholderText("e.g. SomaFM Groove Salad")
        v.addWidget(self._name)

        v.addWidget(_label("Stream URL"))
        self._stream = QLineEdit(str(self._station.get("streamUrl") or ""))
        self._stream.setPlaceholderText("https://… or http://…")
        v.addWidget(self._stream)

        v.addWidget(_label("Home page URL (optional)"))
        self._home = QLineEdit(str(self._station.get("homePageUrl") or ""))
        self._home.setPlaceholderText("https://example.com")
        v.addWidget(self._home)

        v.addSpacing(SPACE_SM)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        v.addWidget(buttons)
        self._buttons = buttons

    def _on_accept(self) -> None:
        name = self._name.text().strip()
        stream = self._stream.text().strip()
        if not name:
            frosted_warning(self, "Missing name", "Give the station a name.")
            self._name.setFocus()
            return
        if not stream or not (
            stream.startswith("http://") or stream.startswith("https://")
        ):
            frosted_warning(
                self,
                "Stream URL required",
                "Paste a direct stream URL — http:// or https://.",
            )
            self._stream.setFocus()
            return
        self.accept()

    def values(self) -> Dict[str, str]:
        """Form values as a dict — mirrors the provider station shape so
        the caller can pass them straight into create/update without
        translation."""
        return {
            "name": self._name.text().strip(),
            "streamUrl": self._stream.text().strip(),
            "homePageUrl": self._home.text().strip(),
        }


# ── Popular-stations picker ────────────────────────────────────────────────


class _PopularStationRow(QFrame):
    """One preset row inside the picker dialog — name + description +
    Add button. The Add button flips to a disabled "Added" state when
    the station's stream URL is already in the user's list, so users
    don't accidentally add duplicates if they re-open the picker."""

    add_requested = Signal(dict)  # the preset entry

    def __init__(self, preset: Dict, already_added: bool, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._preset = dict(preset)
        self.setObjectName("jtPresetRow")
        self.setStyleSheet(
            f"#jtPresetRow {{ background: {BG_CARD}; border-radius: {RADIUS_LG}px; }}"
        )

        row = QHBoxLayout(self)
        row.setContentsMargins(SPACE_MD, SPACE_SM, SPACE_MD, SPACE_SM)
        row.setSpacing(SPACE_MD)

        text_col = QVBoxLayout()
        text_col.setSpacing(2)
        name = QLabel(preset.get("name") or "")
        name.setStyleSheet(f"{type_qss(TYPE_BODY)} color: {TEXT};")
        desc = QLabel(preset.get("description") or "")
        desc.setStyleSheet(f"{type_qss(TYPE_CAPTION)} color: {TEXT_DIM};")
        text_col.addWidget(name)
        text_col.addWidget(desc)
        row.addLayout(text_col, 1)

        _ghost_btn_qss = (
            f"QPushButton {{ {type_qss(TYPE_CAPTION)} color: {TEXT_DIM}; "
            f"background: transparent; border: 1px solid {TEXT_FAINT}; "
            f"border-radius: {RADIUS_LG}px; padding: 4px 14px; }} "
            f"QPushButton:hover {{ color: {TEXT}; border-color: {TEXT_DIM}; }} "
            f"QPushButton:disabled {{ color: {TEXT_FAINT}; "
            f"border-color: {TEXT_FAINT}; }}"
        )
        self._add_btn = QPushButton("Added" if already_added else "Add")
        self._add_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._add_btn.setStyleSheet(_ghost_btn_qss)
        self._add_btn.setEnabled(not already_added)
        self._add_btn.clicked.connect(lambda: self.add_requested.emit(self._preset))
        row.addWidget(self._add_btn, 0, Qt.AlignmentFlag.AlignVCenter)

    def mark_added(self) -> None:
        """Flip the button to its added/disabled state — called after
        the provider confirms the create succeeded."""
        self._add_btn.setText("Added")
        self._add_btn.setEnabled(False)


class _PopularPickerDialog(FrostedDialog):
    """Modal picker over the curated ``POPULAR_STATIONS`` list. Stays
    open after each add so the user can grab several stations in one
    pass; closes on Done. Emits ``add_requested(preset)`` per Add
    click so the caller owns the provider round-trip. App-styled
    (frosted body + GLOBAL_STYLE) so it matches the active theme."""

    add_requested = Signal(dict)

    def __init__(
        self,
        already_added_urls: set,
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent, title="Popular stations", min_width=520)
        from modules import ui_helpers as _u

        self.setMinimumHeight(480)
        # Track row widgets by stream URL so the caller can flip an
        # "Add" → "Added" after the create succeeds without us
        # rebuilding the whole dialog.
        self._rows_by_url: Dict[str, _PopularStationRow] = {}

        outer = self.content_layout
        outer.setContentsMargins(SPACE_LG, SPACE_SM, SPACE_LG, SPACE_LG)
        outer.setSpacing(SPACE_SM)

        intro = QLabel(
            "Hand-picked publicly-streaming stations. Click Add to copy "
            "one into your library — you can edit or remove it later."
        )
        intro.setWordWrap(True)
        intro.setStyleSheet(
            f"{type_qss(TYPE_CAPTION)} color: {_u.TEXT_DIM}; background: transparent;"
        )
        outer.addWidget(intro)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setStyleSheet(
            "QScrollArea { background: transparent; border: none; } "
            "QScrollArea > QWidget > QWidget { background: transparent; }"
        )
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        install_autofade_scrollbars(scroll)

        host = QWidget()
        host.setStyleSheet("background: transparent;")
        v = QVBoxLayout(host)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(SPACE_SM)

        # Group rows by category in the documented order; categories
        # not in ``category_order()`` get appended after the known
        # ones, in first-seen order, so adding a new category to
        # POPULAR_STATIONS doesn't require touching the picker code.
        by_cat: Dict[str, List[Dict]] = {}
        for preset in POPULAR_STATIONS:
            cat = preset.get("category") or "Other"
            by_cat.setdefault(cat, []).append(preset)
        seen_order = []
        for cat in category_order():
            if cat in by_cat:
                seen_order.append(cat)
        for cat in by_cat.keys():
            if cat not in seen_order:
                seen_order.append(cat)

        for cat in seen_order:
            header = QLabel(cat.upper())
            header.setStyleSheet(
                f"{type_qss(TYPE_CAPTION)} color: {_u.TEXT_FAINT}; "
                "letter-spacing: 1px; padding-top: 6px; background: transparent;"
            )
            v.addWidget(header)
            for preset in by_cat[cat]:
                url = str(preset.get("streamUrl") or "")
                row = _PopularStationRow(preset, url in already_added_urls)
                row.add_requested.connect(self.add_requested.emit)
                v.addWidget(row)
                if url:
                    self._rows_by_url[url] = row

        v.addStretch(1)
        scroll.setWidget(host)
        outer.addWidget(scroll, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        outer.addWidget(buttons)

    def mark_added(self, stream_url: str) -> None:
        row = self._rows_by_url.get(stream_url)
        if row is not None:
            row.mark_added()


# ── Row ─────────────────────────────────────────────────────────────────────


class _StationRow(QFrame):
    """One station in the list — name + stream URL sub-line + Edit and
    Remove buttons. The whole row is the play affordance: clicking
    anywhere outside the action buttons emits ``play_requested`` so the
    caller can install the INTERNET_RADIO queue.

    Buttons share the ghost style used by the downloads list so the two
    surfaces read as siblings."""

    play_requested = Signal(dict)  # station
    edit_requested = Signal(dict)
    remove_requested = Signal(dict)

    def __init__(self, station: Dict, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._station = dict(station)
        self.setObjectName("jtStationRow")
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        row = QHBoxLayout(self)
        row.setContentsMargins(SPACE_MD, SPACE_SM, SPACE_MD, SPACE_SM)
        row.setSpacing(SPACE_MD)

        text_col = QVBoxLayout()
        text_col.setSpacing(2)
        self._name = QLabel(self._station.get("name") or "Unnamed station")
        self._sub = QLabel(self._station.get("streamUrl") or "")
        self._sub.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        text_col.addWidget(self._name)
        text_col.addWidget(self._sub)
        row.addLayout(text_col, 1)

        self._edit_btn = QPushButton("Edit")
        self._edit_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._edit_btn.clicked.connect(self._on_edit)
        row.addWidget(self._edit_btn, 0, Qt.AlignmentFlag.AlignVCenter)

        self._remove_btn = QPushButton("Remove")
        self._remove_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._remove_btn.clicked.connect(self._on_remove)
        row.addWidget(self._remove_btn, 0, Qt.AlignmentFlag.AlignVCenter)

        self._apply_styling()

    def _apply_styling(self) -> None:
        """(Re-)stamp the row's stylesheet using the current theme
        constants. Called at construction and again from the host's
        `_reapply_accent` so a live theme/accent swap doesn't leave
        baked colors stale (the module-level `TEXT_DIM` / `TEXT_FAINT`
        / `BG_CARD` get rebound by `ui_helpers.refresh_theme`, so any
        f-string captured at __init__ time keeps the OLD value)."""
        from modules.ui_helpers import (
            BG_CARD as _BG_CARD,
        )
        from modules.ui_helpers import (
            TEXT as _TEXT,
        )
        from modules.ui_helpers import (
            TEXT_DIM as _TEXT_DIM,
        )
        from modules.ui_helpers import (
            TEXT_FAINT as _TEXT_FAINT,
        )
        from modules.ui_helpers import (
            ink_alpha as _ink_alpha,
        )

        self.setStyleSheet(
            f"#jtStationRow {{ background: {_BG_CARD}; border-radius: {RADIUS_LG}px; }}"
            f"#jtStationRow:hover {{ background: {_ink_alpha(0.08)}; }}"
        )
        self._name.setStyleSheet(f"{type_qss(TYPE_BODY)} color: {_TEXT};")
        self._sub.setStyleSheet(f"{type_qss(TYPE_CAPTION)} color: {_TEXT_DIM};")
        ghost_qss = (
            f"QPushButton {{ {type_qss(TYPE_CAPTION)} color: {_TEXT_DIM}; "
            f"background: transparent; border: 1px solid {_TEXT_FAINT}; "
            f"border-radius: {RADIUS_LG}px; padding: 4px 12px; }} "
            f"QPushButton:hover {{ color: {_TEXT}; border-color: {_TEXT_DIM}; }} "
        )
        self._edit_btn.setStyleSheet(ghost_qss)
        self._remove_btn.setStyleSheet(ghost_qss)

    def update_station(self, station: Dict) -> None:
        """Mutate-in-place after an edit so the row repaints without a
        full reload of the page."""
        self._station = dict(station)
        self._name.setText(self._station.get("name") or "Unnamed station")
        self._sub.setText(self._station.get("streamUrl") or "")

    def mousePressEvent(self, e):
        # Action buttons consume their own clicks; the row click is the
        # play affordance, which fires only for the left button so a
        # right-click context menu (future) doesn't accidentally play.
        if e.button() == Qt.MouseButton.LeftButton:
            self.play_requested.emit(self._station)
        super().mousePressEvent(e)

    def _on_edit(self) -> None:
        self.edit_requested.emit(self._station)

    def _on_remove(self) -> None:
        self.remove_requested.emit(self._station)


# ── View ────────────────────────────────────────────────────────────────────


class RadioView(QWidget):
    """The "Radio" tab. Reload-on-show wired by ``reload()`` from the
    host so a station added on another device shows up after a navaway
    + back. CRUD calls hit the provider through ``run_async`` so a
    slow server doesn't stall the GUI."""

    def __init__(self, queue_mgr, parent: Optional[QWidget] = None):
        super().__init__(parent)
        # Top-level object name so tests / QSS can address the view itself
        # (the rows already carry jtStationRow / jtPresetRow).
        self.setObjectName("radioView")
        self._queue_mgr = queue_mgr
        self._rows: List[_StationRow] = []
        self.setStyleSheet("background: transparent;")

        page = QVBoxLayout(self)
        page.setContentsMargins(SPACE_XL, SPACE_LG, SPACE_XL, SPACE_LG)
        page.setSpacing(SPACE_MD)

        # Heading + Add button. Mirrors the downloads page layout so
        # the user reads them as two faces of the same "library list"
        # pattern.
        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        # No page title: the top-bar "Radio" nav label already names this
        # surface, so a second heading is redundant. Actions stay right-aligned.
        header.addStretch(1)
        self._browse_btn = QPushButton("Browse popular")
        self._browse_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._browse_btn.clicked.connect(self._on_browse_popular)
        header.addWidget(self._browse_btn)
        self._add_btn = QPushButton("Add station")
        self._add_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._add_btn.clicked.connect(self._on_add)
        header.addWidget(self._add_btn)
        page.addLayout(header)

        # Scroll surface — same shape the downloads library view uses.
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setStyleSheet(
            "QScrollArea { background: transparent; border: none; } "
            "QScrollArea > QWidget > QWidget { background: transparent; }"
        )
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        install_autofade_scrollbars(self._scroll)

        self._list_host = QWidget()
        self._list_host.setStyleSheet("background: transparent;")
        self._list = QVBoxLayout(self._list_host)
        self._list.setContentsMargins(0, 0, 0, 0)
        self._list.setSpacing(SPACE_SM)
        self._list.addStretch(1)
        self._scroll.setWidget(self._list_host)
        page.addWidget(self._scroll, 1)

        self._empty = QLabel(
            "No radio stations yet.\nClick “Add station” to paste in a "
            "stream URL — SomaFM, NTS, your local public radio's stream, …"
        )
        self._empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        page.addWidget(self._empty, 1)

        self._apply_styling()

        # Live-accent: re-stamp baked QSS strings on theme/accent swap.
        # See architecture_live_accent.md for the contract.
        PlayerBus.get().theme_changed.connect(self._reapply_accent)

        self.reload()

    def _apply_styling(self) -> None:
        """(Re-)stamp the page's own widget stylesheets using current
        theme constants. Sibling `_reapply_accent` also iterates child
        `_StationRow`s. See live-accent contract."""
        from modules.ui_helpers import (
            ACCENT as _ACCENT,
        )
        from modules.ui_helpers import (
            TEXT as _TEXT,
        )
        from modules.ui_helpers import (
            TEXT_DIM as _TEXT_DIM,
        )
        from modules.ui_helpers import (
            TEXT_FAINT as _TEXT_FAINT,
        )

        self._browse_btn.setStyleSheet(
            f"QPushButton {{ {type_qss(TYPE_CAPTION)} color: {_TEXT_DIM}; "
            f"background: transparent; border: 1px solid {_TEXT_FAINT}; "
            f"border-radius: {RADIUS_LG}px; padding: 6px 14px; }} "
            f"QPushButton:hover {{ color: {_TEXT}; border-color: {_TEXT_DIM}; }} "
        )
        self._add_btn.setStyleSheet(
            f"QPushButton {{ {type_qss(TYPE_CAPTION)} color: {_TEXT}; "
            f"background: {_ACCENT}; border: none; border-radius: "
            f"{RADIUS_LG}px; padding: 6px 14px; }} "
            f"QPushButton:hover {{ background: {_ACCENT}; }} "
        )
        self._empty.setStyleSheet(
            f"{type_qss(TYPE_BODY)} color: {_TEXT_FAINT}; padding: {SPACE_XL}px;"
        )

    def _reapply_accent(self) -> None:
        self._apply_styling()
        for row in self._rows:
            row._apply_styling()

    # ── Provider round-trip helpers ──────────────────────────────────────

    def _api(self):
        return get_provider()

    def reload(self) -> None:
        """Refetch from the provider and rebuild the list. Cheap on
        Jellyfin (QSettings read); a server call on Subsonic — wrapped
        in run_async so a slow response doesn't stall the view swap."""
        # Strip any current rows synchronously so the user sees the
        # surface clear immediately on tab switch.
        self._clear_rows()
        # In-flight guard: rapid re-nav can fire two reloads; without this a
        # stale _on_result lands its stations AFTER the newer reload cleared
        # the rows → doubled stations. Drop results from a superseded reload.
        self._load_gen = getattr(self, "_load_gen", 0) + 1
        gen = self._load_gen

        def _on_result(stations):
            if gen != self._load_gen:
                return
            self._populate(stations or [])

        def _on_error(exc):
            if gen != self._load_gen:
                return
            self._populate([])
            frosted_warning(
                self,
                "Couldn't load stations",
                f"The provider returned an error:\n{exc}",
            )

        run_async(self._api().get_internet_radio_stations, on_result=_on_result, on_error=_on_error)

    def _clear_rows(self) -> None:
        for row in self._rows:
            self._list.removeWidget(row)
            row.hide()
            row.setParent(None)
            row.deleteLater()
        self._rows.clear()

    def _populate(self, stations: List[Dict]) -> None:
        for station in stations:
            self._append_row(station)
        self._refresh_visibility()

    def _append_row(self, station: Dict) -> None:
        row = _StationRow(station, parent=self._list_host)
        row.play_requested.connect(self._on_play)
        row.edit_requested.connect(self._on_edit)
        row.remove_requested.connect(self._on_remove)
        self._list.insertWidget(self._list.count() - 1, row)
        self._rows.append(row)

    def _refresh_visibility(self) -> None:
        has_any = bool(self._rows)
        self._list_host.setVisible(has_any)
        self._empty.setVisible(not has_any)

    # ── CRUD handlers ────────────────────────────────────────────────────

    def _on_add(self) -> None:
        dlg = _StationFormDialog(self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        values = dlg.values()

        def _on_result(new_station):
            if isinstance(new_station, dict):
                self._append_row(new_station)
                self._refresh_visibility()

        def _on_error(exc):
            self._show_crud_error("create", exc)

        run_async(
            self._api().create_internet_radio_station,
            values["name"],
            values["streamUrl"],
            values["homePageUrl"] or None,
            on_result=_on_result,
            on_error=_on_error,
        )

    def _on_browse_popular(self) -> None:
        """Open the curated-station picker. The dialog stays open while
        the user adds stations one by one — every successful create
        flips the matching row's button to "Added" so they can see
        what's in the library without closing + reopening."""
        already_urls = {
            str(row._station.get("streamUrl") or "") for row in self._rows
        }
        dlg = _PopularPickerDialog(already_urls, parent=self)

        def _on_add_requested(preset: Dict):
            stream = str(preset.get("streamUrl") or "")
            name = str(preset.get("name") or "")
            home = str(preset.get("homePageUrl") or "")
            if not stream or not name:
                return

            def _on_result(new_station):
                if isinstance(new_station, dict):
                    self._append_row(new_station)
                    self._refresh_visibility()
                    # Flip the picker's row to "Added" so a re-click
                    # can't double-create. The dialog widget may have
                    # already closed by the time the async result lands
                    # — guard the call.
                    try:
                        dlg.mark_added(stream)
                    except RuntimeError:
                        pass

            def _on_error(exc):
                self._show_crud_error("create", exc)

            run_async(
                self._api().create_internet_radio_station,
                name,
                stream,
                home or None,
                on_result=_on_result,
                on_error=_on_error,
            )

        dlg.add_requested.connect(_on_add_requested)
        dlg.exec()

    def _on_edit(self, station: Dict) -> None:
        dlg = _StationFormDialog(self, station=station)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        values = dlg.values()
        sid = str(station.get("id") or "")

        def _on_result(updated):
            if not isinstance(updated, dict):
                return
            for row in self._rows:
                if str(row._station.get("id")) == sid:
                    row.update_station(updated)
                    break

        def _on_error(exc):
            self._show_crud_error("update", exc)

        run_async(
            self._api().update_internet_radio_station,
            sid,
            values["name"],
            values["streamUrl"],
            values["homePageUrl"] or None,
            on_result=_on_result,
            on_error=_on_error,
        )

    def _on_remove(self, station: Dict) -> None:
        name = station.get("name") or "this station"
        if not frosted_confirm(
            self,
            "Remove station",
            f"Remove “{name}” from your radio list?",
            confirm_text="Remove",
            destructive=True,
        ):
            return
        sid = str(station.get("id") or "")

        def _on_result(_unused):
            for row in list(self._rows):
                if str(row._station.get("id")) == sid:
                    self._list.removeWidget(row)
                    row.hide()
                    row.setParent(None)
                    row.deleteLater()
                    self._rows.remove(row)
                    break
            self._refresh_visibility()

        def _on_error(exc):
            self._show_crud_error("delete", exc)

        run_async(
            self._api().delete_internet_radio_station,
            sid,
            on_result=_on_result,
            on_error=_on_error,
        )

    def _show_crud_error(self, verb: str, exc: BaseException) -> None:
        if isinstance(exc, NotImplementedError):
            frosted_info(
                self,
                "Not supported",
                "This server doesn't support managing radio stations.\n"
                "Subsonic requires admin permission for create / edit / "
                "delete.",
            )
            return
        frosted_warning(
            self,
            f"Couldn't {verb} station",
            f"The provider returned an error:\n{exc}",
        )

    # ── Play ─────────────────────────────────────────────────────────────

    def _on_play(self, station: Dict) -> None:
        """Install a single-item INTERNET_RADIO queue. The item dict
        carries the live ``streamUrl`` inline so the queue manager's
        ``_build_now_playing`` skips its usual provider round-trip for
        an audio URL. The ICY title pipeline (mpv observer →
        ``radio_title_changed``) takes over the displayed track title
        once the stream is connected."""
        sid = str(station.get("id") or "")
        name = station.get("name") or "Radio"
        stream = station.get("streamUrl") or ""
        if not stream:
            return
        item = {
            "Id": sid,
            "Name": name,
            "Type": "Audio",
            # Inline URL — the queue manager treats this as the
            # authoritative stream and skips get_audio_stream_url.
            "streamUrl": stream,
            # Zero duration so the bar's "tot_time" reads 0:00 (then
            # hidden entirely by the LIVE treatment).
            "RunTimeTicks": 0,
        }
        # Logo: prefer a curated preset URL (we trust those); fall back
        # to whatever the station dict carries (Subsonic's OpenSubsonic
        # may surface one in a future revision). Empty string means no
        # cover until per-track art lookup succeeds.
        logo = logo_url_for_stream(stream) or str(station.get("logoUrl") or "")
        ctx = QueueContext(
            kind=QueueKind.INTERNET_RADIO,
            source_id=sid,
            source_label=name,
            source_icon=logo,
        )
        PlayerBus.get().queue_play_now.emit([item], 0, ctx)
