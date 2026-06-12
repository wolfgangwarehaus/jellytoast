"""Device-busy dialog — the pinned direct (ALSA) output refused to open.

Shown via ``PlayerBus.audio_device_busy`` when the user's pinned
``alsa/…`` device fails to open while still being enumerable: another
app (usually PipeWire, on behalf of a browser or system sounds) holds
the hardware. Playback has deliberately STOPPED at this point — falling
back to the mixer silently would betray the exclusivity the user chose
(2026-06-11 live find: YouTube first + jellytoast second produced sound
through the shared mixer with the Bit-Perfect badge implied).

The dialog explains the situation and offers the one-click escape:
play through PipeWire for now (explicitly shared — not the bit-perfect
path)."""

from __future__ import annotations

from typing import Callable, Optional

from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QWidget

from jellytoast.design_tokens import TYPE_BODY, type_qss
from jellytoast.frosted_dialog import FrostedDialog


class DeviceBusyDialog(FrostedDialog):
    def __init__(
        self,
        parent: Optional[QWidget] = None,
        *,
        device_label: str,
        on_play_via_pipewire: Optional[Callable[[], None]] = None,
    ) -> None:
        super().__init__(parent, title="Audio device in use", min_width=420)
        from jellytoast import ui_helpers as _u

        self._message = QLabel(
            f"{device_label} is in use by another app.\n\n"
            "Bit-perfect playback claims the device exclusively — stop "
            "other audio first, then press play again. Or play through "
            "PipeWire for now (shared, not bit-perfect)."
        )
        self._message.setWordWrap(True)
        self._message.setStyleSheet(
            f"color: {_u.TEXT}; background: transparent; {type_qss(TYPE_BODY)}"
        )
        self.content_layout.addWidget(self._message)

        row = QHBoxLayout()
        row.addStretch(1)
        if on_play_via_pipewire is not None:
            self._pipewire_btn = QPushButton("Play via PipeWire")
            self._pipewire_btn.setAutoDefault(False)

            def _go():
                self.accept()
                on_play_via_pipewire()

            self._pipewire_btn.clicked.connect(_go)
            row.addWidget(self._pipewire_btn)
        self._ok_btn = QPushButton("OK")
        self._ok_btn.setDefault(True)
        self._ok_btn.clicked.connect(self.reject)
        row.addWidget(self._ok_btn)
        self.content_layout.addLayout(row)
