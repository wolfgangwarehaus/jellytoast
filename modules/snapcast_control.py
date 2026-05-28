"""Snapcast control surface.

Snapcast is a multiroom *routing matrix*, not a play-this-track target:
the snapserver sources its own streams and fans them to grouped clients.
So "casting to Snapcast" means **controlling** the server — routing each
group to a stream and setting per-room volume — not pushing a URL. That's
why a Snapcast pick in the cast dialog opens THIS dialog instead of going
through the stop-local-playback + ``active_cast`` flow the URL-push
backends use.

The transport lives in ``modules.cast.snapcast.SnapcastController`` (JSON-RPC
over its own asyncio loop); this module is the Qt surface over it. State
arrives via ``PlayerBus.snapcast_state_changed`` and the dialog rebuilds
from the snapshot.

``snapshot_to_rows`` is split out as a pure function so the
snapshot → render-model mapping is unit-testable without a QApplication.

VISUAL + HARDWARE NOTE: there is no Snapcast hardware available to verify
this against, so the wiring is correct-by-construction + unit-tested but
the layout/UX is unverified — expect august to refine it once a snapserver
is on hand (see docs/research/casting_snapcast.md §10).
"""

from __future__ import annotations

from typing import Any, Dict, List

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from modules.player_state import PlayerBus
from modules.selector import Selector


def snapshot_to_rows(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    """Turn a ``SnapcastController.snapshot()`` dict into a flat render
    model the dialog maps straight to widgets.

    Returns ``{"streams": [...], "groups": [row, ...]}`` where each row is
    ``{group_id, group_name, stream_id, muted, clients}`` and ``clients``
    is the resolved member client dicts (in group order). Pure — no Qt,
    no controller — so it unit-tests on its own.
    """
    streams: List[Dict[str, Any]] = list(snapshot.get("streams") or [])
    clients_by_id = {c["id"]: c for c in (snapshot.get("clients") or [])}
    rows: List[Dict[str, Any]] = []
    for g in snapshot.get("groups") or []:
        members = [clients_by_id[cid] for cid in g.get("client_ids", []) if cid in clients_by_id]
        # Snapserver groups are frequently unnamed — fall back to the
        # first member's name, then a generic label.
        name = g.get("name") or (members[0]["name"] if members else "Group")
        rows.append(
            {
                "group_id": g["id"],
                "group_name": name,
                "stream_id": g.get("stream_id", ""),
                "muted": bool(g.get("muted", False)),
                "clients": members,
            }
        )
    return {"streams": streams, "groups": rows}


class SnapcastControlDialog(QDialog):
    """Connect to one snapserver and drive its groups / streams / volumes.

    Non-modal (``show()``), like the cast dialog. Connects on open,
    rebuilds on every ``snapcast_state_changed``, and disconnects on
    close so a closed dialog doesn't hold a live JSON-RPC session.
    """

    def __init__(self, controller, server_info, parent=None):
        super().__init__(parent)
        self._ctl = controller
        self._info = server_info
        self._bus = PlayerBus.get()
        self._building = False
        self._closed = False

        name = getattr(server_info, "hostname", "") or getattr(server_info, "host", "") or "Snapcast"
        self.setWindowTitle(f"Snapcast — {name}")
        self.setMinimumWidth(420)

        root = QVBoxLayout(self)
        self._status = QLabel("Connecting…")
        self._status.setWordWrap(True)
        root.addWidget(self._status)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self._body = QWidget()
        self._body_layout = QVBoxLayout(self._body)
        self._body_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self._scroll.setWidget(self._body)
        root.addWidget(self._scroll, 1)

        close_row = QHBoxLayout()
        close_row.addStretch(1)
        close_btn = QPushButton("Close")
        close_btn.setAutoDefault(False)
        close_btn.clicked.connect(self.close)
        close_row.addWidget(close_btn)
        root.addLayout(close_row)

        # State + lifecycle signals. snapcast_* are deliberately separate
        # from the cast_* bus (design doc §10).
        self._bus.snapcast_state_changed.connect(self._on_state_changed)
        self._bus.snapcast_connection_changed.connect(self._on_connection_changed)
        self._bus.snapcast_error.connect(self._on_error)

        # Open the session. connect() fires on_done on the GUI thread once
        # Server.GetStatus lands; the state signal then drives the first
        # render.
        host = getattr(server_info, "host", "")
        port = getattr(server_info, "port", 1705)
        self._ctl.connect(host, port, on_done=self._on_connected)

    # ── Controller callbacks / bus slots ─────────────────────────────

    def _on_connected(self, ok: bool):
        if ok:
            self._status.setText("Connected. Route each room to a stream below.")
            self._rebuild(snapshot_to_rows(self._ctl.snapshot()))
        else:
            self._status.setText(
                "Couldn't connect to this Snapcast server. It may be offline "
                "or on a different network."
            )

    def _on_connection_changed(self, connected: bool):
        if not connected:
            self._status.setText("Disconnected.")

    def _on_error(self, message: str):
        # Surface but don't tear down — a transient RPC error shouldn't
        # nuke the dialog.
        self._status.setText(message)

    def _on_state_changed(self, snapshot: dict):
        self._rebuild(snapshot_to_rows(snapshot))

    # ── Rendering ────────────────────────────────────────────────────

    def _clear_body(self):
        while self._body_layout.count():
            item = self._body_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

    def _rebuild(self, model: Dict[str, Any]):
        """Rebuild the group cards from a render model. ``_building``
        guards the per-widget signals so populating values doesn't echo
        a write back to the server."""
        self._building = True
        try:
            self._clear_body()
            streams = model.get("streams", [])
            groups = model.get("groups", [])
            if not groups:
                self._body_layout.addWidget(QLabel("No groups reported by this server yet."))
                return
            for row in groups:
                self._body_layout.addWidget(self._build_group_card(row, streams))
        finally:
            self._building = False

    def _build_group_card(self, row: Dict[str, Any], streams: List[Dict[str, Any]]) -> QWidget:
        gid = row["group_id"]
        card = QFrame()
        card.setObjectName("snapGroupCard")
        v = QVBoxLayout(card)

        header = QHBoxLayout()
        title = QLabel(row["group_name"])
        title.setStyleSheet("font-weight: 600;")
        header.addWidget(title)
        header.addStretch(1)

        header.addWidget(QLabel("Stream:"))
        stream_sel = Selector()
        for s in streams:
            stream_sel.addItem(s.get("name") or s.get("id", ""), s.get("id", ""))
        idx = stream_sel.findData(row.get("stream_id", ""))
        if idx >= 0:
            stream_sel.setCurrentIndex(idx)
        stream_sel.currentIndexChanged.connect(
            lambda _i, g=gid, sel=stream_sel: self._on_stream_picked(g, sel)
        )
        header.addWidget(stream_sel)

        mute_btn = QPushButton("Muted" if row["muted"] else "Mute")
        mute_btn.setCheckable(True)
        mute_btn.setChecked(row["muted"])
        mute_btn.setAutoDefault(False)
        mute_btn.toggled.connect(lambda on, g=gid, b=mute_btn: self._on_group_mute(g, on, b))
        header.addWidget(mute_btn)
        v.addLayout(header)

        for client in row["clients"]:
            v.addLayout(self._build_client_row(client))
        return card

    def _build_client_row(self, client: Dict[str, Any]) -> QHBoxLayout:
        cid = client["id"]
        h = QHBoxLayout()
        label = QLabel(client.get("name") or cid)
        label.setMinimumWidth(120)
        h.addWidget(label)

        slider = QSlider(Qt.Orientation.Horizontal)
        slider.setRange(0, 100)
        slider.setValue(int(client.get("volume", 0)))
        # Commit on release only — avoids flooding the JSON-RPC link with
        # a write per pixel of drag.
        slider.sliderReleased.connect(lambda c=cid, s=slider: self._on_client_volume(c, s.value()))
        h.addWidget(slider, 1)

        mute = QPushButton("Muted" if client.get("muted") else "Mute")
        mute.setCheckable(True)
        mute.setChecked(bool(client.get("muted")))
        mute.setAutoDefault(False)
        mute.toggled.connect(lambda on, c=cid, b=mute: self._on_client_mute(c, on, b))
        h.addWidget(mute)
        return h

    # ── Write handlers (guarded against the rebuild echo) ─────────────

    def _on_stream_picked(self, group_id: str, sel: Selector):
        if self._building:
            return
        stream_id = sel.currentData()
        if stream_id:
            self._ctl.set_group_stream(group_id, stream_id)

    def _on_group_mute(self, group_id: str, muted: bool, btn: QPushButton):
        btn.setText("Muted" if muted else "Mute")
        if self._building:
            return
        self._ctl.set_group_mute(group_id, muted)

    def _on_client_volume(self, client_id: str, pct: int):
        if self._building:
            return
        self._ctl.set_client_volume(client_id, int(pct))

    def _on_client_mute(self, client_id: str, muted: bool, btn: QPushButton):
        btn.setText("Muted" if muted else "Mute")
        if self._building:
            return
        self._ctl.set_client_mute(client_id, muted)

    # ── Lifecycle ────────────────────────────────────────────────────

    def closeEvent(self, e):
        # Drop the bus subscriptions + the live session so a closed dialog
        # doesn't keep a JSON-RPC connection (or echo state into a dead
        # widget). Idempotent — close can fire more than once (explicit
        # close + the finished-signal teardown), and disconnecting an
        # already-disconnected signal warns under PySide.
        if self._closed:
            super().closeEvent(e)
            return
        self._closed = True
        for sig, slot in (
            (self._bus.snapcast_state_changed, self._on_state_changed),
            (self._bus.snapcast_connection_changed, self._on_connection_changed),
            (self._bus.snapcast_error, self._on_error),
        ):
            try:
                sig.disconnect(slot)
            except (RuntimeError, TypeError):
                pass
        try:
            self._ctl.disconnect()
        except Exception:
            pass
        super().closeEvent(e)
