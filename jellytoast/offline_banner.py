"""Offline-mode status chip — small rounded button in the top bar's
right column, to the left of the search button.

Three steady states (hidden when none apply):

- Offline mode on, server reachable — "Offline" chip; click to go
  online. Click enters a brief "Connecting…" animation, then lifts
  offline mode (which the bus signal turns into a refresh that hides
  the chip).
- Offline mode on, server unreachable — "Offline" chip, clickable to
  fire an immediate recovery probe (in addition to the periodic
  background probe). On probe success, offline lifts; on failure the
  chip drops back to the unreachable state.
- Offline mode off, server unreachable — "No connection" chip,
  clickable to fire the same recovery probe.

Subscribes to ``offline_mode_changed`` + ``connectivity_changed`` on
``PlayerBus`` and re-renders on either. The chip manages its own
visibility; the top bar just adds it to its right column.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QPushButton

from jellytoast import offline
from jellytoast.design_tokens import TYPE_CAPTION, rad, type_qss
from jellytoast.player_state import PlayerBus
from jellytoast.ui_helpers import ACCENT, TEXT_DIM

# How long the "Connecting…" animation is shown before we actually
# flip offline mode off. Short enough to feel like an immediate
# response, long enough to register as confirmation rather than a
# silent toggle.
_CONNECTING_HOLD_MS = 700
_DOT_TICK_MS = 220


class OfflineChip(QPushButton):
    """Compact rounded-square status indicator. Subtler than the rest
    of the top bar's affordances — present but not shouting."""

    def __init__(self, parent=None):
        super().__init__("", parent)
        self.setObjectName("jtOfflineChip")
        self.setFixedHeight(24)
        self.setFlat(True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._connecting = False
        self._dot_phase = 0
        self._dot_timer = QTimer(self)
        self._dot_timer.setInterval(_DOT_TICK_MS)
        self._dot_timer.timeout.connect(self._tick_dots)
        self._apply_style()
        self.clicked.connect(self._on_clicked)

        bus = PlayerBus.get()
        bus.offline_mode_changed.connect(self._on_offline_mode_changed)
        bus.connectivity_changed.connect(self._refresh)
        # Live-accent: rebuild the stylesheet when the user changes
        # their accent so the chip stays in palette.
        bus.theme_changed.connect(self._apply_style)
        self._refresh()

    # ── Style ───────────────────────────────────────────────────────────

    def _apply_style(self, *_: object) -> None:
        r, g, b = self._accent_rgb()
        # Subtle by design: thin accent border, low-opacity fill. Sits
        # adjacent to the search icon without competing with it. Radius
        # matches the rounded-square idiom used elsewhere (top bar
        # view dropdown, settings dialog buttons).
        self.setStyleSheet(
            f"QPushButton#jtOfflineChip {{ "
            f"background: rgba({r},{g},{b},0.12); color: {TEXT_DIM}; "
            f"border: 1px solid rgba({r},{g},{b},0.32); "
            f"border-radius: {rad(6)}px; padding: 0 10px; "
            f"{type_qss(TYPE_CAPTION)} }}"
            f"QPushButton#jtOfflineChip:hover {{ "
            f"background: rgba({r},{g},{b},0.20); "
            f"border-color: rgba({r},{g},{b},0.50); }}"
        )

    @staticmethod
    def _accent_rgb() -> tuple[int, int, int]:
        try:
            s = ACCENT.lstrip("#")
            return int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)
        except Exception:
            return 138, 91, 228

    # ── Interaction ─────────────────────────────────────────────────────

    def _on_clicked(self) -> None:
        if self._connecting:
            return
        is_offline = offline.is_offline_mode()
        reachable = offline.is_server_reachable()
        if is_offline and reachable:
            # Happy path — server is up, user just wants to leave
            # offline mode. Short hold for visual confirmation, then
            # flip the toggle (bus signal hides the chip).
            self._enter_connecting()
            QTimer.singleShot(_CONNECTING_HOLD_MS, self._finish_reconnect)
        elif not reachable:
            # Unreachable in either offline-on or offline-off state —
            # fire an immediate recovery probe. Probe success lifts
            # auto-offline + flips reachable, both of which fire bus
            # signals that re-render the chip. On failure we drop
            # back to the same state we were in.
            self._enter_connecting()
            offline.probe_now(on_done=self._on_probe_done)

    def _enter_connecting(self) -> None:
        self._connecting = True
        self._dot_phase = 0
        self._dot_timer.start()
        self.setEnabled(False)
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self._render_connecting()

    def _finish_reconnect(self) -> None:
        # Flip offline mode off — the bus signal handler hides the
        # chip in _on_offline_mode_changed.
        offline.set_offline_mode(False)

    def _on_probe_done(self, recovered: bool) -> None:
        # Called from connectivity.probe_now on the GUI thread once
        # the recovery probe completes. If recovered, the bus signals
        # already triggered _refresh via _on_offline_mode_changed /
        # connectivity_changed → no extra work here. If not, exit the
        # connecting animation manually so the chip drops back to its
        # steady "Offline" / "No connection" rendering.
        if not recovered and self._connecting:
            self._connecting = False
            self._dot_timer.stop()
            self.setEnabled(True)
            self._refresh()

    def _tick_dots(self) -> None:
        self._dot_phase = (self._dot_phase + 1) % 4
        self._render_connecting()

    def _render_connecting(self) -> None:
        dots = "." * self._dot_phase
        # Pad so the chip width doesn't bounce as dots cycle.
        self.setText(f"Connecting{dots:<3}")

    # ── Bus handlers ────────────────────────────────────────────────────

    def _on_offline_mode_changed(self, _on: bool) -> None:
        # Either we just succeeded in going online (mid-animation) or
        # offline mode flipped elsewhere — clear connecting state and
        # let _refresh pick the right steady-state rendering.
        if self._connecting:
            self._connecting = False
            self._dot_timer.stop()
            self.setEnabled(True)
        self._refresh()

    def _refresh(self, *_: object) -> None:
        if self._connecting:
            return
        is_offline = offline.is_offline_mode()
        reachable = offline.is_server_reachable()
        if is_offline and reachable:
            self.setText("Offline")
            self.setToolTip("Click to go online")
            self.setCursor(Qt.CursorShape.PointingHandCursor)
            self.setEnabled(True)
            self.setVisible(True)
        elif is_offline and not reachable:
            self.setText("Offline")
            self.setToolTip("Server unreachable — click to try reconnecting")
            self.setCursor(Qt.CursorShape.PointingHandCursor)
            self.setEnabled(True)
            self.setVisible(True)
        elif not is_offline and not reachable:
            self.setText("No connection")
            self.setToolTip("Server unreachable — click to try reconnecting")
            self.setCursor(Qt.CursorShape.PointingHandCursor)
            self.setEnabled(True)
            self.setVisible(True)
        else:
            self.setVisible(False)
