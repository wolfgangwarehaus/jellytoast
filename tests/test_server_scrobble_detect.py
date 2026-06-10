"""ListenBrainz submission_client probe — the double-scrobble detector.

Navidrome doesn't expose scrobble linkage in its API, so we infer a second
scrobbler from the user's own LB listens: any ``submission_client`` other
than ``jellytoast`` (e.g. ``navidrome``) means the server is covering this
account. Pure logic — ``requests.get`` is stubbed, no network.
"""

from __future__ import annotations

import requests

from jellytoast.scrobble import server_scrobble_detect as ssd


class _Resp:
    def __init__(self, status, payload=None, raises=False):
        self.status_code = status
        self._payload = payload
        self._raises = raises

    def json(self):
        if self._raises:
            raise ValueError("bad json")
        return self._payload


def _listens(*clients):
    """Build a LB listens response, one listen per submission_client (None =
    a listen with no submission_client field)."""
    out = []
    for c in clients:
        ai = {} if c is None else {"submission_client": c}
        out.append({"track_metadata": {"artist_name": "A", "track_name": "T", "additional_info": ai}})
    return {"payload": {"listens": out}}


def _patch_get(monkeypatch, resp):
    def fake_get(url, params=None, headers=None, timeout=None):
        return resp
    monkeypatch.setattr(requests, "get", fake_get)


def test_detects_navidrome_alongside_jellytoast(monkeypatch):
    _patch_get(monkeypatch, _Resp(200, _listens("navidrome", "jellytoast", "navidrome")))
    res = ssd.detect("https://api.listenbrainz.org", "avtips", "tok")
    assert res.checked
    assert res.foreign_clients == ("navidrome",)  # de-duped, jellytoast excluded
    assert res.server_scrobbles is True


def test_only_jellytoast_means_no_second_scrobbler(monkeypatch):
    _patch_get(monkeypatch, _Resp(200, _listens("jellytoast", "jellytoast")))
    res = ssd.detect("https://api.listenbrainz.org", "avtips", "tok")
    assert res.checked
    assert res.foreign_clients == ()
    assert res.server_scrobbles is False


def test_case_insensitive_and_ignores_blank_client(monkeypatch):
    _patch_get(monkeypatch, _Resp(200, _listens("JellyToast", None, "Navidrome")))
    res = ssd.detect("https://api.listenbrainz.org", "avtips")
    assert res.foreign_clients == ("navidrome",)  # JellyToast == ours; None skipped


def test_empty_account_checked_but_no_foreign(monkeypatch):
    _patch_get(monkeypatch, _Resp(200, {"payload": {"listens": []}}))
    res = ssd.detect("https://api.listenbrainz.org", "avtips")
    assert res.checked is True
    assert res.server_scrobbles is False


def test_non_200_is_not_checked(monkeypatch):
    _patch_get(monkeypatch, _Resp(500, None))
    res = ssd.detect("https://api.listenbrainz.org", "avtips")
    assert res.checked is False
    assert res.server_scrobbles is False


def test_network_error_is_not_checked(monkeypatch):
    def boom(*a, **k):
        raise requests.RequestException("down")
    monkeypatch.setattr(requests, "get", boom)
    res = ssd.detect("https://api.listenbrainz.org", "avtips")
    assert res.checked is False


def test_bad_json_is_not_checked(monkeypatch):
    _patch_get(monkeypatch, _Resp(200, None, raises=True))
    res = ssd.detect("https://api.listenbrainz.org", "avtips")
    assert res.checked is False


def test_no_username_short_circuits(monkeypatch):
    called = {"n": 0}

    def fake_get(*a, **k):
        called["n"] += 1
        return _Resp(200, _listens("navidrome"))
    monkeypatch.setattr(requests, "get", fake_get)
    res = ssd.detect("https://api.listenbrainz.org", "")
    assert res.checked is False
    assert called["n"] == 0  # no network attempt without a username


class TestRefreshServerScrobbleFlags:
    """The package helper that runs detect() and persists the result —
    wiring between the probe and settings.server_scrobbles_listenbrainz."""

    def test_sets_flag_on_navidrome_when_navidrome_submits(self, qapp, isolated_settings, monkeypatch):
        import jellytoast.async_io as aio
        from jellytoast import scrobble as scr

        isolated_settings.listenbrainz_enabled = True
        isolated_settings.listenbrainz_username = "avtips"
        isolated_settings.listenbrainz_token = "tok"
        isolated_settings.server_is_navidrome = True  # current server is Navidrome
        isolated_settings.server_scrobbles_listenbrainz = False
        monkeypatch.setattr(
            ssd, "detect",
            lambda *a, **k: ssd.Result(checked=True, foreign_clients=("navidrome",)),
        )
        monkeypatch.setattr(
            aio, "run_async",
            lambda fn, *a, on_result=None, on_error=None, **k: on_result(fn(*a)),
        )
        done = []
        scr.refresh_server_scrobble_flags(on_done=done.append)
        assert isolated_settings.server_scrobbles_listenbrainz is True
        assert isolated_settings.server_scrobble_check_done is True
        assert done and done[0].server_scrobbles is True

    def test_jellyfin_swap_ignores_stale_navidrome_listens(self, qapp, isolated_settings, monkeypatch):
        # After a swap to Jellyfin, the LB account STILL carries recent
        # "navidrome" listens — but the current server is Jellyfin, which
        # doesn't forward, so in-app scrobbling must auto-resume (flag → False)
        # instead of staying suppressed by the stale name.
        import jellytoast.async_io as aio
        from jellytoast import scrobble as scr

        isolated_settings.listenbrainz_enabled = True
        isolated_settings.listenbrainz_username = "avtips"
        isolated_settings.server_is_navidrome = False  # now on Jellyfin
        isolated_settings.server_scrobbles_listenbrainz = True  # stale from Navidrome
        monkeypatch.setattr(
            ssd, "detect",
            lambda *a, **k: ssd.Result(checked=True, foreign_clients=("navidrome",)),
        )
        monkeypatch.setattr(
            aio, "run_async",
            lambda fn, *a, on_result=None, on_error=None, **k: on_result(fn(*a)),
        )
        scr.refresh_server_scrobble_flags()
        assert isolated_settings.server_scrobbles_listenbrainz is False  # resumes in-app

    def test_clears_flag_when_no_foreign_client(self, qapp, isolated_settings, monkeypatch):
        import jellytoast.async_io as aio
        from jellytoast import scrobble as scr

        isolated_settings.listenbrainz_enabled = True
        isolated_settings.listenbrainz_username = "u"
        isolated_settings.server_scrobbles_listenbrainz = True  # stale True
        monkeypatch.setattr(
            ssd, "detect", lambda *a, **k: ssd.Result(checked=True, foreign_clients=())
        )
        monkeypatch.setattr(
            aio, "run_async",
            lambda fn, *a, on_result=None, on_error=None, **k: on_result(fn(*a)),
        )
        scr.refresh_server_scrobble_flags()
        assert isolated_settings.server_scrobbles_listenbrainz is False  # self-corrects

    def test_noop_when_lb_not_configured(self, qapp, isolated_settings, monkeypatch):
        from jellytoast import scrobble as scr

        isolated_settings.listenbrainz_enabled = False
        called = []
        monkeypatch.setattr(ssd, "detect", lambda *a, **k: called.append(1))
        done = []
        scr.refresh_server_scrobble_flags(on_done=done.append)
        assert called == []  # no probe without LB configured
        assert done and done[0].checked is False


def test_server_scrobbler_name_maps_navidrome_only():
    # Navidrome stamps "navidrome"; for any other server we don't have a
    # reliable name → "" → never auto-suppress (in-app scrobbling stays on,
    # correct for vanilla Jellyfin which doesn't forward to LB).
    assert ssd.server_scrobbler_name(True) == "navidrome"
    assert ssd.server_scrobbler_name(False) == ""
