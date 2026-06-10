"""IconButton chrome conventions (finding #1, 2026-06-07).

Icon buttons are mouse-driven chrome. Two app-wide rules are now centralised
in ``IconButton.__init__`` so every transport/chrome button is uniform across
the top bar, now-playing bar, mini player, dialogs, and the VolumeButton /
CoverOverlayButton subclasses:

* **No keyboard focus** — ``focusPolicy() == NoFocus`` — so a focus snap
  (e.g. after a mode toggle) never paints Qt's focus ring on a transport
  button. (The bars previously inherited QPushButton's StrongFocus default;
  the mini player set NoFocus per-site. Now it's one place.)
* **Default arrow cursor** — the pointing-hand affordance is reserved for
  text CTAs and clickable cards; icon buttons keep the arrow. A handful of
  sites (heart, play overlay, back, about, volume) used to override to the
  hand — those overrides were removed.

`qapp` (conftest.py) provides the QApplication widget construction needs.
"""

from PySide6.QtCore import Qt

from jellytoast.icon_button import IconButton


def test_icon_button_takes_no_keyboard_focus(qapp):
    assert IconButton().focusPolicy() == Qt.FocusPolicy.NoFocus


def test_icon_button_keeps_default_arrow_cursor(qapp):
    # No explicit cursor set → Qt's default arrow, never the pointing hand.
    assert IconButton().cursor().shape() == Qt.CursorShape.ArrowCursor


def test_volume_button_subclass_inherits_chrome_conventions(qapp):
    from jellytoast.player_state import PlayerBus
    from jellytoast.volume_button import VolumeButton

    btn = VolumeButton(PlayerBus())
    assert btn.focusPolicy() == Qt.FocusPolicy.NoFocus
    assert btn.cursor().shape() == Qt.CursorShape.ArrowCursor


def test_cover_overlay_button_subclass_inherits_chrome_conventions(qapp):
    from PySide6.QtWidgets import QWidget

    from jellytoast.ui_helpers import CoverOverlayButton

    parent = QWidget()
    btn = CoverOverlayButton(parent)
    assert btn.focusPolicy() == Qt.FocusPolicy.NoFocus
    assert btn.cursor().shape() == Qt.CursorShape.ArrowCursor
