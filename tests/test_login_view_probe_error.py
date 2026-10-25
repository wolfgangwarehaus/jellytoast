"""The desktop sign-in screen's error handling (``LoginView._on_sign_in_err``).

Messages come from the shared ``server_connect.message_for``; the view adds
two things of its own: a "the public demo is down" message when "Try a demo"
can't reach the demo, and clearing the password after a wrong one.
"""

from __future__ import annotations

from jellytoast.login_view import LoginView
from jellytoast.server_connect import ConnectError


class _Field:
    def __init__(self):
        self.cleared = False
        self.focused = False

    def clear(self):
        self.cleared = True

    def setFocus(self, *_a):  # noqa: N802
        self.focused = True


def _err(err, demo=False):
    view = LoginView.__new__(LoginView)
    view._set_submitting = lambda _flag: None
    view._pending_demo = demo
    view._password_field = _Field()
    captured: list[str] = []
    view._show_error = captured.append
    LoginView._on_sign_in_err(view, err)
    assert len(captured) == 1
    return captured[0], view._password_field


def test_unreachable_says_so_instead_of_wrong_server_type():
    msg, _ = _err(ConnectError(ConnectError.UNREACHABLE, "nas.local"))
    assert "Couldn't reach nas.local" in msg
    assert "doesn't look like" not in msg


def test_not_a_music_server_names_both_kinds():
    msg, _ = _err(ConnectError(ConnectError.NOT_SERVER, "example.com"))
    assert "Jellyfin" in msg and "Navidrome" in msg


def test_wrong_password_clears_the_field():
    msg, pw = _err(ConnectError(ConnectError.WRONG_CREDENTIALS, "nas.local"))
    assert msg == "Wrong username or password."
    assert pw.cleared and pw.focused


def test_demo_down_blames_the_demo_not_the_user():
    msg, pw = _err(ConnectError(ConnectError.UNREACHABLE, "demo.navidrome.org"), demo=True)
    assert "public demo" in msg
    assert not pw.cleared


def test_unexpected_exception_still_gets_a_sentence():
    msg, _ = _err(RuntimeError("kaboom"))
    assert "kaboom" in msg
