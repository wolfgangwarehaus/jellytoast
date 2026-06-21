"""Demo-server data layer + the login-view 'Try a demo' wiring.

The data tests are pure (no Qt). The wiring test builds a real LoginView under
the shared QApplication and drives _try_demo with run_async mocked, so it proves
the button fills the form for the selected server type and dispatches through the
ordinary auth path — without any network.
"""

from __future__ import annotations

import json

import pytest

from jellytoast import demo_servers as ds

# ── data layer ───────────────────────────────────────────────────────────────


def test_defaults_cover_both_provider_kinds():
    kinds = {d.kind for d in ds.get_demo_servers()}
    assert {"jellyfin", "subsonic"} <= kinds


def test_demo_for_kind_returns_match():
    nav = ds.demo_for_kind("subsonic")
    assert nav is not None
    assert nav.url == "https://demo.navidrome.org"
    assert nav.username == "demo"
    assert nav.password == "demo"

    jf = ds.demo_for_kind("jellyfin")
    assert jf is not None
    assert jf.url == "https://demo.jellyfin.org/stable"
    assert jf.username == "demo"
    assert jf.password == ""  # passwordless public account


def test_demo_for_kind_is_case_insensitive():
    assert ds.demo_for_kind("SubSonic") is not None


def test_demo_for_kind_unknown_returns_none():
    assert ds.demo_for_kind("plex") is None
    assert ds.demo_for_kind("") is None


def test_env_override_replaces_defaults(monkeypatch):
    monkeypatch.setenv(
        "JT_DEMO_SERVERS",
        json.dumps(
            [{"kind": "subsonic", "url": "https://example.test", "username": "u", "password": "p"}]
        ),
    )
    servers = ds.get_demo_servers()
    assert len(servers) == 1
    assert servers[0].url == "https://example.test"
    # The replaced list has no jellyfin entry now.
    assert ds.demo_for_kind("jellyfin") is None


def test_env_override_fills_optional_fields(monkeypatch):
    monkeypatch.setenv("JT_DEMO_SERVERS", json.dumps([{"kind": "subsonic", "url": "https://x.test"}]))
    s = ds.demo_for_kind("subsonic")
    assert s is not None and s.username == "" and s.password == "" and s.label == "subsonic"


@pytest.mark.parametrize("bad", ["not json", "{}", "[{}]", '[{"url": "x"}]', "null", "[]"])
def test_malformed_or_empty_override_falls_back_to_defaults(monkeypatch, bad):
    monkeypatch.setenv("JT_DEMO_SERVERS", bad)
    # A bad override must never break the login screen — defaults are restored.
    assert ds.get_demo_servers() == ds._DEFAULT_DEMOS


# ── login-view wiring ────────────────────────────────────────────────────────


def _build_login_view(qapp, monkeypatch):
    """Construct a LoginView with run_async stubbed to capture dispatches."""
    from jellytoast import login_view as lv

    calls: list = []
    monkeypatch.setattr(lv, "run_async", lambda fn, *a, **k: calls.append((fn, a, k)))
    view = lv.LoginView()
    return view, calls


def test_try_demo_fills_subsonic_form_and_dispatches(qapp, isolated_settings, monkeypatch):
    view, calls = _build_login_view(qapp, monkeypatch)
    try:
        idx = view._kind_combo.findData("subsonic")
        view._kind_combo.setCurrentIndex(idx)

        view._try_demo()

        assert view._server_field.text() == "https://demo.navidrome.org"
        assert view._username_field.text() == "demo"
        assert view._password_field.text() == "demo"
        assert view._pending_demo is True
        # A probe was dispatched through the NORMAL path with the demo URL.
        assert calls, "expected _do_submit to dispatch a probe via run_async"
        _fn, args, _kw = calls[0]
        assert args[0] == "https://demo.navidrome.org"
    finally:
        view.deleteLater()


def test_try_demo_fills_jellyfin_form(qapp, isolated_settings, monkeypatch):
    view, calls = _build_login_view(qapp, monkeypatch)
    try:
        idx = view._kind_combo.findData("jellyfin")
        view._kind_combo.setCurrentIndex(idx)

        view._try_demo()

        assert view._server_field.text() == "https://demo.jellyfin.org/stable"
        assert view._username_field.text() == "demo"
        assert view._password_field.text() == ""  # passwordless
        assert view._pending_demo is True
        assert calls
    finally:
        view.deleteLater()


def test_normal_submit_leaves_pending_demo_false(qapp, isolated_settings, monkeypatch):
    view, calls = _build_login_view(qapp, monkeypatch)
    try:
        view._server_field.setText("http://my.server:8096")
        view._username_field.setText("alice")
        view._password_field.setText("secret")

        view._submit()

        assert view._pending_demo is False
        assert calls  # dispatched normally
    finally:
        view.deleteLater()
