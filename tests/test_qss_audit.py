"""Static audit for Qt QSS parse warnings on QPushButton stylesheets.

The 2026-05-17 live testing surfaced a runtime warning ``Could not
parse stylesheet of object QPushButton(0x...)`` during an offline
disconnect. Qt silently falls back to defaults; the warning is
otherwise harmless but visible in stderr.

The 2026-05-18 audit replayed every ``QPushButton`` stylesheet in the
``modules/`` tree against headless Qt (offscreen platform plugin) and
drove ``PlayerBus.offline_mode_changed`` / ``connectivity_changed``
through every widget that re-renders on those signals. Zero "Could
not parse" warnings reproduced.

This test pins the ``OfflineChip`` stylesheet — the only widget whose
QSS is rebuilt mid-flight when offline state flips — as a regression
test against the suspected offender. If a real offender surfaces
later, parameterise this with the offending (file, qss) pair rather
than scanning every QPushButton on every run.
"""

from __future__ import annotations

import os
from typing import List

# Headless Qt — works in CI without a display. Set before any Qt
# import; the QApplication fixture in conftest picks it up.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QtMsgType, qInstallMessageHandler  # noqa: E402
from PySide6.QtGui import QPixmap  # noqa: E402


def _capture_parse_warnings(callable_) -> List[str]:
    """Run ``callable_`` with a Qt message handler that captures any
    "Could not parse stylesheet" warning Qt emits. Restores the
    previous handler so we don't suppress diagnostics in other tests.

    Qt's stylesheet parser runs lazily on first polish/paint, so the
    callable must force the widget to render — accessing styleSheet()
    alone won't trip the parser."""
    captured: List[str] = []

    def handler(_msg_type: QtMsgType, _ctx, msg: str) -> None:
        if "Could not parse" in msg:
            captured.append(msg)

    prev = qInstallMessageHandler(handler)
    try:
        callable_()
    finally:
        qInstallMessageHandler(prev)
    return captured


def test_offline_chip_stylesheet_parses(qapp):
    """``OfflineChip`` QSS must parse without Qt warnings, including
    after the bus emits ``offline_mode_changed`` and
    ``connectivity_changed`` (the chip rebuilds its stylesheet on
    each transition)."""
    from modules.offline_banner import OfflineChip
    from modules.player_state import PlayerBus

    def exercise() -> None:
        chip = OfflineChip()
        chip.show()
        chip.ensurePolished()
        chip.resize(120, 24)
        # Force a real polish/paint so Qt's lazy parser runs.
        chip.render(QPixmap(chip.size()))
        qapp.processEvents()

        bus = PlayerBus.get()
        for emit in (
            lambda: bus.offline_mode_changed.emit(True),
            lambda: bus.connectivity_changed.emit(False),
            lambda: bus.connectivity_changed.emit(True),
            lambda: bus.offline_mode_changed.emit(False),
            lambda: bus.theme_changed.emit(),
        ):
            emit()
            qapp.processEvents()
            chip.render(QPixmap(chip.size()))

        chip.hide()
        chip.deleteLater()

    warnings = _capture_parse_warnings(exercise)
    assert warnings == [], (
        "OfflineChip stylesheet produced Qt parse warnings:\n"
        + "\n".join(warnings)
    )
