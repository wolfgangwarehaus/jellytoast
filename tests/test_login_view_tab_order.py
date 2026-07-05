"""Coverage for the LoginView keyboard tab order.

The default Qt focus chain follows construction order, which detoured
through the flat "+ Add alternate URL" link between the Server URL and
Username fields — with no visible focus state on that link, Tab
appeared to swallow a keystroke mid-form. LoginView now sets an
explicit chain: server type → URL → username → password → Sign in,
with the secondary text links (demo, alternate URLs) after the primary
path. This pins that order so a widget added to the card later can't
silently re-break fill-out-the-form-with-Tab.

Needs the constructed widget (the chain is wired in ``__init__``) —
``qapp`` + ``isolated_settings`` keep it off the user's real config.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QWidget

from jellytoast.login_view import LoginView


def _next_tab_stop(w: QWidget) -> QWidget:
    """The next widget Tab would land on — walk the raw focus chain
    past the label/spacer widgets that don't accept tab focus, the
    same skip Qt's focusNextPrevChild does at runtime."""
    cur = w.nextInFocusChain()
    while not cur.focusPolicy() & Qt.FocusPolicy.TabFocus:
        cur = cur.nextInFocusChain()
    return cur


def test_tab_chain_walks_fields_then_submit(qapp, isolated_settings):
    view = LoginView()
    expected = [
        view._kind_combo,
        view._server_field,
        view._username_field,
        view._password_field,
        view._submit_btn,
        view._demo_btn,
        view._alt_urls_btn,
    ]
    for cur, nxt in zip(expected[:-1], expected[1:], strict=True):
        assert _next_tab_stop(cur) is nxt, (
            f"Tab from {cur.objectName() or type(cur).__name__} should land on "
            f"{nxt.objectName() or type(nxt).__name__}"
        )
