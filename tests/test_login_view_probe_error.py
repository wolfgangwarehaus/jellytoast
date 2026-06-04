"""Coverage for ``modules.login_view.LoginView._on_probe_err``.

When the URL probe fails, the error shown to the user must name the
*selected* provider — not a hardcoded "Jellyfin". A Navidrome user who
fat-fingers the URL and gets a 404 should be told it "doesn't look like
a Subsonic server", matching the provider dropdown they picked. The
sibling success-path message (``_on_probe_ok``) already does this; this
pins the failure path so it can't silently regress to "Jellyfin" again.

The method only touches ``self.provider.kind``, ``self._set_submitting``
and ``self._show_error`` — none of which need the constructed widget —
so the tests drive it on a ``__new__`` instance with those three stubbed,
no QApplication required.
"""

from __future__ import annotations

from modules.login_view import LoginView


class _FakeProvider:
    def __init__(self, kind: str):
        self.kind = kind


def _probe_err(kind: str, err: Exception) -> str:
    """Run ``_on_probe_err`` on a bare LoginView with a stubbed provider
    and capture the message handed to ``_show_error``."""
    view = LoginView.__new__(LoginView)
    view.provider = _FakeProvider(kind)
    view._set_submitting = lambda _flag: None
    captured: list[str] = []
    view._show_error = captured.append
    LoginView._on_probe_err(view, err)
    assert len(captured) == 1
    return captured[0]


def test_404_names_subsonic_when_subsonic_selected():
    msg = _probe_err("subsonic", Exception("404 Not Found"))
    assert "Subsonic server" in msg
    assert "Jellyfin" not in msg


def test_404_names_jellyfin_when_jellyfin_selected():
    msg = _probe_err("jellyfin", Exception("404 Not Found"))
    assert "Jellyfin server" in msg


def test_connection_error_is_provider_neutral():
    # The unreachable-server message doesn't name a provider — make sure
    # the 404 branch didn't bleed into it.
    msg = _probe_err("subsonic", Exception("Max retries exceeded"))
    assert "Couldn't reach the server" in msg
    assert "Subsonic" not in msg and "Jellyfin" not in msg
