"""A13 — Multi-server hostname fallback tests.

Covers:
  * ``settings.server_hostnames`` round-trip + JSON shape.
  * Connectivity-tracker fallback walk against mocked health probes.
  * Priority ordering and graceful "all alternates down" path.
  * ``host_switched`` signal payloads.
  * Provider ``with_url`` swap preserving auth state for both
    Jellyfin and Subsonic.
  * Climb-back to the primary on the next successful probe cycle.

No QApplication required — the connectivity tracker emits via
``PlayerBus.get()`` but the bus is a QObject and we drive signals
via a no-Qt listener registered with ``connect`` on a plain Python
callable.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from modules.offline import connectivity as _conn
from modules.player_state import PlayerBus


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_connectivity_state():
    """Wipe the connectivity module's globals before/after every test
    so state can't leak across cases. Also resets the PlayerBus
    singleton's pending emissions by reconstructing receivers each
    test."""
    _conn._reset_for_tests()
    yield
    _conn._reset_for_tests()


@pytest.fixture
def isolated_settings_singleton(tmp_path, monkeypatch):
    """Force ``get_settings()`` to return a Settings rooted in tmp_path
    so we can freely write ``server_url`` / ``server_hostnames`` /
    ``provider_kind`` without touching the user's real config."""
    from modules import settings as _settings_mod

    monkeypatch.setattr(_settings_mod, "_settings", None)
    s = _settings_mod.Settings()
    monkeypatch.setattr(s, "_config_dir", tmp_path)
    monkeypatch.setattr(_settings_mod, "_settings", s)
    s.server_url = "http://primary.example/"
    s.server_hostnames = []
    s.provider_kind = "jellyfin"
    yield s
    monkeypatch.setattr(_settings_mod, "_settings", None)


@pytest.fixture
def bus_signals():
    """Capture every signal emission relevant to A13. Returns a dict
    of lists keyed by signal name."""
    captured: dict = {
        "connectivity_changed": [],
        "host_switched": [],
        "offline_mode_changed": [],
    }
    bus = PlayerBus.get()

    def _on_conn(r):
        captured["connectivity_changed"].append(bool(r))

    def _on_host(label):
        captured["host_switched"].append(str(label))

    def _on_offline(v):
        captured["offline_mode_changed"].append(bool(v))

    bus.connectivity_changed.connect(_on_conn)
    bus.host_switched.connect(_on_host)
    bus.offline_mode_changed.connect(_on_offline)
    yield captured
    try:
        bus.connectivity_changed.disconnect(_on_conn)
        bus.host_switched.disconnect(_on_host)
        bus.offline_mode_changed.disconnect(_on_offline)
    except Exception:
        pass


@pytest.fixture
def stub_provider(monkeypatch):
    """Replace ``modules.providers.get_provider`` with a tiny stub
    whose ``with_url`` records the call. Tests assert on the
    recorded swap list."""

    class _StubProvider:
        def __init__(self, kind="jellyfin", url="http://primary.example"):
            self.kind = kind
            self._url = url
            self.swaps: list = []
            self.access_token = "stub-token"
            self.user_id = "stub-user"
            self.device_id = "stub-device"

        @property
        def server_url(self):
            return self._url

        def with_url(self, new_url):
            self.swaps.append(new_url)
            self._url = new_url.rstrip("/")
            return self

    stub = _StubProvider()
    monkeypatch.setattr(
        "modules.providers.get_provider",
        lambda: stub,
    )
    return stub


def _probe_factory(reachable_urls: set):
    """Build a ``_probe_host`` replacement that returns True only for
    URLs in ``reachable_urls``. URL comparison is exact after the
    rstrip the connectivity module applies."""

    def _probe(url, kind):
        return url.rstrip("/") in reachable_urls

    return _probe


# ── Settings round-trip ─────────────────────────────────────────────────────


class TestSettingsHostnames:
    def test_default_is_empty_list(self, isolated_settings_singleton):
        assert isolated_settings_singleton.server_hostnames == []

    def test_round_trip_full_entries(self, isolated_settings_singleton):
        isolated_settings_singleton.server_hostnames = [
            {"label": "LAN", "url": "http://192.168.1.50:4533", "priority": 1},
            {"label": "Tailscale", "url": "https://music.tail.ts.net", "priority": 2},
        ]
        out = isolated_settings_singleton.server_hostnames
        assert len(out) == 2
        assert out[0]["label"] == "LAN"
        assert out[0]["url"] == "http://192.168.1.50:4533"
        assert out[0]["priority"] == 1
        assert out[1]["label"] == "Tailscale"

    def test_trailing_slash_normalized(self, isolated_settings_singleton):
        isolated_settings_singleton.server_hostnames = [
            {"label": "x", "url": "https://x.example/", "priority": 0},
        ]
        assert isolated_settings_singleton.server_hostnames[0]["url"] == "https://x.example"

    def test_empty_url_entries_dropped(self, isolated_settings_singleton):
        isolated_settings_singleton.server_hostnames = [
            {"label": "ok", "url": "https://ok.example", "priority": 0},
            {"label": "drop", "url": "", "priority": 1},
            {"label": "drop2", "priority": 2},
        ]
        out = isolated_settings_singleton.server_hostnames
        assert len(out) == 1
        assert out[0]["label"] == "ok"

    def test_invalid_priority_coerces_to_zero(self, isolated_settings_singleton):
        isolated_settings_singleton.server_hostnames = [
            {"label": "x", "url": "https://x.example", "priority": "junk"},
        ]
        assert isolated_settings_singleton.server_hostnames[0]["priority"] == 0

    def test_missing_label_falls_back_to_url(
        self,
        isolated_settings_singleton,
    ):
        isolated_settings_singleton.server_hostnames = [
            {"url": "https://nolabel.example", "priority": 0},
        ]
        out = isolated_settings_singleton.server_hostnames
        assert out[0]["label"] == "https://nolabel.example"


# ── Ordered-hosts helper ────────────────────────────────────────────────────


class TestOrderedHosts:
    def test_primary_only(self, isolated_settings_singleton):
        hosts = _conn._ordered_hosts()
        assert hosts == [("Primary", "http://primary.example", 0)]

    def test_primary_plus_alternates_sorted(
        self,
        isolated_settings_singleton,
    ):
        isolated_settings_singleton.server_hostnames = [
            {"label": "Tailscale", "url": "https://ts.example", "priority": 3},
            {"label": "LAN", "url": "https://lan.example", "priority": 1},
        ]
        hosts = _conn._ordered_hosts()
        # Primary is priority 0, so it stays head-of-list.
        assert [h[0] for h in hosts] == ["Primary", "LAN", "Tailscale"]

    def test_empty_when_no_primary_url(
        self,
        isolated_settings_singleton,
    ):
        isolated_settings_singleton.server_url = ""
        assert _conn._ordered_hosts() == []


# ── Failure path without hostnames (regression guard) ──────────────────────


class TestFallbackDisabledByEmptyList:
    def test_empty_hostnames_behaves_like_today(
        self,
        isolated_settings_singleton,
        bus_signals,
        stub_provider,
    ):
        """No alternates configured → tracker flips to unreachable at
        the threshold (currently 2 consecutive failures), same path
        as the pre-A13 single-server case."""
        with patch.object(_conn, "_probe_host", return_value=False):
            _conn.note_network_failure()
            _conn.note_network_failure()
        assert bus_signals["connectivity_changed"] == [False]
        assert bus_signals["host_switched"] == []
        assert stub_provider.swaps == []
        assert _conn.is_server_reachable() is False

    def test_single_failure_does_not_trip(
        self,
        isolated_settings_singleton,
        bus_signals,
    ):
        _conn.note_network_failure()
        assert bus_signals["connectivity_changed"] == []
        assert _conn.is_server_reachable() is True


# ── Fallback walk ───────────────────────────────────────────────────────────


class TestFallbackWalk:
    def test_first_alternate_up_swaps(
        self,
        isolated_settings_singleton,
        bus_signals,
        stub_provider,
    ):
        isolated_settings_singleton.server_hostnames = [
            {"label": "LAN", "url": "https://lan.example", "priority": 1},
            {"label": "Tailscale", "url": "https://ts.example", "priority": 2},
        ]
        # Primary fails the threshold; LAN is up.
        probe = _probe_factory({"https://lan.example"})
        with patch.object(_conn, "_probe_host", side_effect=probe):
            _conn.note_network_failure()
            _conn.note_network_failure()
        assert bus_signals["host_switched"] == ["LAN"]
        assert bus_signals["connectivity_changed"] == []  # absorbed
        assert stub_provider.swaps == ["https://lan.example"]
        assert _conn.is_server_reachable() is True

    def test_priority_ordering_respected(
        self,
        isolated_settings_singleton,
        bus_signals,
        stub_provider,
    ):
        """Tailscale (priority 1) wins over LAN-far (priority 5) even
        though both probe True."""
        isolated_settings_singleton.server_hostnames = [
            {"label": "LAN-far", "url": "https://far.example", "priority": 5},
            {"label": "Tailscale", "url": "https://ts.example", "priority": 1},
        ]
        probe = _probe_factory({"https://far.example", "https://ts.example"})
        with patch.object(_conn, "_probe_host", side_effect=probe):
            _conn.note_network_failure()
            _conn.note_network_failure()
        assert bus_signals["host_switched"] == ["Tailscale"]
        assert stub_provider.swaps == ["https://ts.example"]

    def test_skips_unreachable_alternates(
        self,
        isolated_settings_singleton,
        bus_signals,
        stub_provider,
    ):
        """LAN is the first alternate by priority but probe-down;
        Tailscale comes next and wins."""
        isolated_settings_singleton.server_hostnames = [
            {"label": "LAN", "url": "https://lan.example", "priority": 1},
            {"label": "Tailscale", "url": "https://ts.example", "priority": 2},
        ]
        probe = _probe_factory({"https://ts.example"})
        with patch.object(_conn, "_probe_host", side_effect=probe):
            _conn.note_network_failure()
            _conn.note_network_failure()
        assert bus_signals["host_switched"] == ["Tailscale"]
        assert stub_provider.swaps == ["https://ts.example"]

    def test_all_alternates_down_emits_unreachable(
        self,
        isolated_settings_singleton,
        bus_signals,
        stub_provider,
    ):
        isolated_settings_singleton.server_hostnames = [
            {"label": "LAN", "url": "https://lan.example", "priority": 1},
            {"label": "Tailscale", "url": "https://ts.example", "priority": 2},
        ]
        with patch.object(_conn, "_probe_host", return_value=False):
            _conn.note_network_failure()
            _conn.note_network_failure()
        assert bus_signals["connectivity_changed"] == [False]
        assert bus_signals["host_switched"] == []
        assert stub_provider.swaps == []
        assert _conn.is_server_reachable() is False

    def test_failover_resets_failure_counter(
        self,
        isolated_settings_singleton,
        bus_signals,
        stub_provider,
    ):
        """After a successful failover, the failure counter is zeroed
        so the *next* string of failures gets its own threshold budget.
        Without this, every subsequent miss while on the alternate
        would re-trigger another failover walk."""
        isolated_settings_singleton.server_hostnames = [
            {"label": "LAN", "url": "https://lan.example", "priority": 1},
        ]
        probe = _probe_factory({"https://lan.example"})
        with patch.object(_conn, "_probe_host", side_effect=probe):
            _conn.note_network_failure()
            _conn.note_network_failure()
        assert _conn._consecutive_failures == 0
        assert _conn.active_host_label() == "LAN"

    def test_active_host_label_set(
        self,
        isolated_settings_singleton,
        bus_signals,
        stub_provider,
    ):
        isolated_settings_singleton.server_hostnames = [
            {"label": "Backup", "url": "https://b.example", "priority": 1},
        ]
        probe = _probe_factory({"https://b.example"})
        with patch.object(_conn, "_probe_host", side_effect=probe):
            _conn.note_network_failure()
            _conn.note_network_failure()
        assert _conn.active_host_label() == "Backup"

    def test_failover_skips_current_host(
        self,
        isolated_settings_singleton,
        bus_signals,
        stub_provider,
    ):
        """After failing over to LAN, a second outage burst tries the
        Tailscale alternate next — not LAN (the host that just
        failed)."""
        isolated_settings_singleton.server_hostnames = [
            {"label": "LAN", "url": "https://lan.example", "priority": 1},
            {"label": "Tailscale", "url": "https://ts.example", "priority": 2},
        ]
        # First burst: only LAN is up.
        probe1 = _probe_factory({"https://lan.example"})
        with patch.object(_conn, "_probe_host", side_effect=probe1):
            _conn.note_network_failure()
            _conn.note_network_failure()
        assert _conn.active_host_label() == "LAN"
        # Second burst: LAN is down (it's the one we're currently on)
        # but Tailscale is up. The walk should NOT re-probe LAN.
        probe2 = _probe_factory({"https://ts.example"})
        with patch.object(_conn, "_probe_host", side_effect=probe2):
            _conn.note_network_failure()
            _conn.note_network_failure()
        assert _conn.active_host_label() == "Tailscale"
        assert bus_signals["host_switched"] == ["LAN", "Tailscale"]
        # Connectivity stayed reachable throughout.
        assert bus_signals["connectivity_changed"] == []


# ── Climb back to primary ───────────────────────────────────────────────────


class TestClimbBackToPrimary:
    def test_primary_returns_swaps_back(
        self,
        isolated_settings_singleton,
        bus_signals,
        stub_provider,
    ):
        isolated_settings_singleton.server_hostnames = [
            {"label": "LAN", "url": "https://lan.example", "priority": 1},
        ]
        # First fail over to LAN.
        probe1 = _probe_factory({"https://lan.example"})
        with patch.object(_conn, "_probe_host", side_effect=probe1):
            _conn.note_network_failure()
            _conn.note_network_failure()
        assert _conn.active_host_label() == "LAN"
        # Now a successful call lands while LAN is the active host AND
        # the primary is reachable — we should climb back.
        probe2 = _probe_factory({"http://primary.example"})
        with patch.object(_conn, "_probe_host", side_effect=probe2):
            _conn.note_success()
        assert _conn.active_host_label() == ""  # back on primary
        assert bus_signals["host_switched"] == ["LAN", "Primary"]
        assert stub_provider.swaps == [
            "https://lan.example",
            "http://primary.example",
        ]

    def test_no_climb_back_when_primary_still_down(
        self,
        isolated_settings_singleton,
        bus_signals,
        stub_provider,
    ):
        isolated_settings_singleton.server_hostnames = [
            {"label": "LAN", "url": "https://lan.example", "priority": 1},
        ]
        probe1 = _probe_factory({"https://lan.example"})
        with patch.object(_conn, "_probe_host", side_effect=probe1):
            _conn.note_network_failure()
            _conn.note_network_failure()
        # Primary still down — note_success doesn't climb back.
        with patch.object(_conn, "_probe_host", return_value=False):
            _conn.note_success()
        assert _conn.active_host_label() == "LAN"
        assert bus_signals["host_switched"] == ["LAN"]

    def test_note_success_without_alternate_is_a_noop_for_climb(
        self,
        isolated_settings_singleton,
        bus_signals,
        stub_provider,
    ):
        """When we're already on the primary, ``note_success`` does
        not probe anything — the climb-back is gated on having an
        active alternate."""
        called: list = []

        def _probe(url, kind):
            called.append(url)
            return True

        with patch.object(_conn, "_probe_host", side_effect=_probe):
            _conn.note_success()
        assert called == []


# ── note_success transition tests ──────────────────────────────────────────


class TestSuccessTransition:
    def test_recovery_from_unreachable_emits_connectivity_true(
        self,
        isolated_settings_singleton,
        bus_signals,
        stub_provider,
    ):
        """When all alternates are down we go unreachable; the next
        success transitions back to reachable and emits
        connectivity_changed(True)."""
        with patch.object(_conn, "_probe_host", return_value=False):
            _conn.note_network_failure()
            _conn.note_network_failure()
        assert bus_signals["connectivity_changed"] == [False]
        _conn.note_success()
        assert bus_signals["connectivity_changed"] == [False, True]


# ── Provider with_url ──────────────────────────────────────────────────────


class TestProviderWithUrl:
    def test_jellyfin_with_url_preserves_auth(
        self,
        isolated_settings_singleton,
    ):
        from modules.providers.jellyfin import JellyfinProvider

        provider = JellyfinProvider()
        provider.api.token = "TOKEN-A"
        provider.api.user_id = "USER-A"
        original_device = provider.device_id
        provider.with_url("https://new.example/")
        assert provider.server_url == "https://new.example"
        assert provider.access_token == "TOKEN-A"
        assert provider.user_id == "USER-A"
        # device_id is QSettings-backed and stable across the swap.
        assert provider.device_id == original_device

    def test_jellyfin_with_url_strips_trailing_slash(
        self,
        isolated_settings_singleton,
    ):
        from modules.providers.jellyfin import JellyfinProvider

        p = JellyfinProvider()
        p.with_url("https://lan.example:8096/")
        assert p.server_url == "https://lan.example:8096"

    def test_jellyfin_with_url_returns_self(
        self,
        isolated_settings_singleton,
    ):
        from modules.providers.jellyfin import JellyfinProvider

        p = JellyfinProvider()
        assert p.with_url("https://x.example") is p

    def test_jellyfin_with_url_clears_meta_cache(
        self,
        isolated_settings_singleton,
    ):
        from modules.providers.jellyfin import JellyfinProvider

        p = JellyfinProvider()
        p.api._meta_cache[("item", "id1")] = {"cached": True}
        p.with_url("https://other.example")
        assert len(p.api._meta_cache) == 0

    def test_subsonic_with_url_preserves_auth(
        self,
        isolated_settings_singleton,
    ):
        from modules.providers.subsonic import SubsonicProvider

        isolated_settings_singleton.provider_kind = "subsonic"
        isolated_settings_singleton.username = "alice"
        isolated_settings_singleton.access_token = "hunter2"
        provider = SubsonicProvider()
        before_user = provider.user_id
        before_token = provider.access_token
        provider.with_url("https://navi.example/")
        assert provider.server_url == "https://navi.example"
        assert provider.user_id == before_user
        assert provider.access_token == before_token

    def test_subsonic_with_url_returns_self(
        self,
        isolated_settings_singleton,
    ):
        from modules.providers.subsonic import SubsonicProvider

        isolated_settings_singleton.provider_kind = "subsonic"
        isolated_settings_singleton.username = "alice"
        isolated_settings_singleton.access_token = "hunter2"
        p = SubsonicProvider()
        assert p.with_url("https://navi.example") is p


# ── Probe behavior ─────────────────────────────────────────────────────────


class TestProbeHost:
    def test_probe_jellyfin_hits_system_info_public(self):
        captured: dict = {}

        class _FakeResp:
            status_code = 200

        def _get(url, *_a, **kw):
            captured["url"] = url
            captured["timeout"] = kw.get("timeout")
            return _FakeResp()

        with patch("requests.get", side_effect=_get):
            ok = _conn._probe_host("https://j.example", "jellyfin")
        assert ok is True
        assert captured["url"].endswith("/System/Info/Public")
        # Bounded so a failover walk stays snappy.
        assert captured["timeout"] == _conn._PROBE_TIMEOUT_S

    def test_probe_subsonic_hits_rest_ping(self):
        captured: dict = {}

        class _FakeResp:
            status_code = 200

        def _get(url, *_a, **kw):
            captured["url"] = url
            captured["params"] = kw.get("params")
            return _FakeResp()

        with patch("requests.get", side_effect=_get):
            ok = _conn._probe_host("https://s.example", "subsonic")
        assert ok is True
        assert captured["url"].endswith("/rest/ping.view")
        assert (captured["params"] or {}).get("c") == "jellytoast"

    def test_probe_returns_false_on_exception(self):
        def _boom(*_a, **_kw):
            raise RuntimeError("boom")

        with patch("requests.get", side_effect=_boom):
            assert _conn._probe_host("https://x.example", "jellyfin") is False

    def test_probe_returns_false_on_http_error_status(self):
        class _FakeResp:
            status_code = 502

        with patch("requests.get", return_value=_FakeResp()):
            assert _conn._probe_host("https://x.example", "jellyfin") is False

    def test_probe_empty_url_returns_false(self):
        assert _conn._probe_host("", "jellyfin") is False


# ── Auto-offline interaction (regression guard) ────────────────────────────


class TestAutoOfflineUnaffectedByAlternates:
    def test_auto_offline_still_engages_when_all_down(
        self,
        isolated_settings_singleton,
        bus_signals,
        stub_provider,
    ):
        """A13 must not break the existing auto-offline path. When
        every alternate is down and auto-offline is on, we still
        flip into offline mode."""
        isolated_settings_singleton.auto_offline_mode = True
        isolated_settings_singleton.server_hostnames = [
            {"label": "alt", "url": "https://alt.example", "priority": 1},
        ]
        with patch.object(_conn, "_probe_host", return_value=False):
            _conn.note_network_failure()
            _conn.note_network_failure()
        assert bus_signals["offline_mode_changed"] == [True]
        assert bus_signals["connectivity_changed"] == [False]


class TestAlternateUrlsDialog:
    """The login-screen dialog that edits settings.server_hostnames."""

    def test_loads_existing_entries_into_rows(self, qapp, isolated_settings_singleton):
        from modules.login_view import _AlternateUrlsDialog

        isolated_settings_singleton.server_hostnames = [
            {"url": "http://lan.example", "label": "LAN", "priority": 0},
            {"url": "http://ts.example", "label": "Tailscale", "priority": 1},
        ]
        dlg = _AlternateUrlsDialog(isolated_settings_singleton)
        try:
            assert len(dlg._rows) == 2
            assert dlg._rows[0].url_edit.text() == "http://lan.example"
            assert dlg._rows[1].label_edit.text() == "Tailscale"
        finally:
            dlg.deleteLater()

    def test_accept_writes_rows_back(self, qapp, isolated_settings_singleton):
        from modules.login_view import _AlternateUrlsDialog

        dlg = _AlternateUrlsDialog(isolated_settings_singleton)
        try:
            dlg._add_row("http://new.example", "New")
            dlg._on_accept()
        finally:
            dlg.deleteLater()
        out = isolated_settings_singleton.server_hostnames
        assert len(out) == 1
        assert out[0]["url"] == "http://new.example"
        assert out[0]["label"] == "New"
        assert out[0]["priority"] == 0

    def test_blank_url_rows_dropped_on_accept(self, qapp, isolated_settings_singleton):
        from modules.login_view import _AlternateUrlsDialog

        dlg = _AlternateUrlsDialog(isolated_settings_singleton)
        try:
            dlg._add_row("", "blank")
            dlg._add_row("http://real.example", "Real")
            dlg._on_accept()
        finally:
            dlg.deleteLater()
        out = isolated_settings_singleton.server_hostnames
        assert [e["url"] for e in out] == ["http://real.example"]

    def test_remove_row(self, qapp, isolated_settings_singleton):
        from modules.login_view import _AlternateUrlsDialog

        dlg = _AlternateUrlsDialog(isolated_settings_singleton)
        try:
            dlg._add_row("http://a.example")
            dlg._add_row("http://b.example")
            dlg._remove_row(dlg._rows[0])
            assert len(dlg._rows) == 1
            assert dlg._rows[0].url_edit.text() == "http://b.example"
        finally:
            dlg.deleteLater()
