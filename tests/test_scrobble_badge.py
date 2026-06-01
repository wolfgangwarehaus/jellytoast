"""Tests for the server-side scrobble badge in the now-playing bar.

The badge reads ``settings.server_scrobbles_lastfm`` and
``settings.server_scrobbles_listenbrainz`` (populated by
``modules.scrobble.refresh_server_scrobble_flags`` — LB submission_client
detection). These tests exercise the three
visibility / tooltip cases by mocking ``get_settings`` and instantiating
``_ScrobbleBadge`` directly — no Qt event loop needed.

Qt's ``isHidden()`` returns True for a widget that's never been shown
*and* False after a programmatic ``setVisible(True)``, so it works as a
proxy for the badge's "wants to be visible" intent without forcing the
test to spin up a parent and call ``show()``.
"""

import pytest


@pytest.fixture
def _badge_factory(qapp, monkeypatch):
    """Yields a callable ``make(lastfm, listenbrainz)`` that patches
    ``modules.settings.get_settings`` and returns a fresh badge.

    ``qapp`` provides the QApplication needed for QLabel construction
    (see conftest)."""
    from modules.now_playing_bar import _ScrobbleBadge

    def _make(lastfm: bool, listenbrainz: bool):
        class _Stub:
            server_scrobbles_lastfm = lastfm
            server_scrobbles_listenbrainz = listenbrainz

        import modules.settings as settings_mod

        monkeypatch.setattr(settings_mod, "get_settings", lambda: _Stub())
        return _ScrobbleBadge()

    return _make


def test_badge_hidden_when_both_flags_false(_badge_factory):
    badge = _badge_factory(False, False)
    assert badge.isHidden()
    assert badge.toolTip() == ""


def test_badge_visible_with_lastfm_only(_badge_factory):
    badge = _badge_factory(True, False)
    assert not badge.isHidden()
    assert badge.toolTip() == "Your server scrobbles to Last.fm"


def test_badge_visible_with_listenbrainz_only(_badge_factory):
    badge = _badge_factory(False, True)
    assert not badge.isHidden()
    assert badge.toolTip() == "Your server scrobbles to ListenBrainz"


def test_badge_visible_with_both_flags(_badge_factory):
    badge = _badge_factory(True, True)
    assert not badge.isHidden()
    assert badge.toolTip() == "Your server scrobbles to Last.fm and ListenBrainz"


def test_badge_survives_settings_failure(qapp, monkeypatch):
    """If ``get_settings`` blows up at construction (e.g. a malformed
    QSettings on disk), the badge should hide rather than propagate."""
    import modules.settings as settings_mod
    from modules.now_playing_bar import _ScrobbleBadge

    def _boom():
        raise RuntimeError("settings unavailable")

    monkeypatch.setattr(settings_mod, "get_settings", _boom)
    badge = _ScrobbleBadge()
    assert badge.isHidden()
    assert badge.toolTip() == ""
