"""Tests for the in-app update check (jellytoast/updates.py).

Focus is the load-bearing logic: the channel gating (only nag MANUAL install
channels, never an auto-updating Store / MAS / AUR build), numeric version
comparison, and release-asset deep-linking. No network — the pure functions are
exercised directly with the runtime probes + settings monkeypatched.
"""

from __future__ import annotations

import platform

import pytest

from jellytoast import updates


class TestVersionCompare:
    @pytest.mark.parametrize(
        "a,b,expected",
        [
            ("0.1.6", "0.1.5", True),
            ("0.1.5", "0.1.5", False),
            ("0.1.4", "0.1.5", False),
            ("v0.2.0", "0.1.9", True),
            ("1.0.0", "0.9.9", True),
            ("0.1.10", "0.1.9", True),  # numeric, not lexical ("10" > "9")
            ("0.1.5", "v0.1.5", False),
        ],
    )
    def test_is_newer(self, a, b, expected):
        assert updates.is_newer(a, b) is expected

    def test_version_tuple_tolerant(self):
        assert updates._version_tuple("0.1.5") == (0, 1, 5)
        assert updates._version_tuple("v0.1.5") == (0, 1, 5)
        assert updates._version_tuple("0.1.5rc1") == (0, 1, 5)
        assert updates._version_tuple("") == (0,)


class TestChannel:
    def test_msix_probe_wins_over_stamp(self, monkeypatch):
        monkeypatch.setattr(updates, "is_msix_packaged", lambda: True)
        monkeypatch.setattr(updates, "is_macos_sandboxed", lambda: False)
        monkeypatch.setattr(updates, "CHANNEL", "dmg")  # ignored — probe hits first
        assert updates.get_channel() == "msix"

    def test_mas_probe_wins(self, monkeypatch):
        monkeypatch.setattr(updates, "is_msix_packaged", lambda: False)
        monkeypatch.setattr(updates, "is_macos_sandboxed", lambda: True)
        assert updates.get_channel() == "mas"

    def test_stamp_used_when_no_probe(self, monkeypatch):
        monkeypatch.setattr(updates, "is_msix_packaged", lambda: False)
        monkeypatch.setattr(updates, "is_macos_sandboxed", lambda: False)
        monkeypatch.setattr(updates, "CHANNEL", "dmg")
        assert updates.get_channel() == "dmg"

    def test_defaults_to_source(self, monkeypatch):
        monkeypatch.setattr(updates, "is_msix_packaged", lambda: False)
        monkeypatch.setattr(updates, "is_macos_sandboxed", lambda: False)
        monkeypatch.setattr(updates, "CHANNEL", "")
        assert updates.get_channel() == "source"

    @pytest.mark.parametrize(
        "channel,is_auto",
        [
            ("msix", True),
            ("mas", True),
            ("aur", True),
            ("dmg", False),
            ("deb", False),
            ("appimage", False),
            ("inno", False),
            ("portable", False),
            ("source", False),
        ],
    )
    def test_auto_vs_manual(self, channel, is_auto, monkeypatch):
        monkeypatch.setattr(updates, "get_channel", lambda: channel)
        assert updates.is_auto_update_channel() is is_auto


class _FakeSettings:
    def __init__(self, enabled=True):
        self.check_for_updates_enabled = enabled


class TestShouldCheck:
    def _patch(self, monkeypatch, channel, enabled):
        monkeypatch.setattr(updates, "get_channel", lambda: channel)
        import jellytoast.settings as s

        monkeypatch.setattr(s, "get_settings", lambda: _FakeSettings(enabled))

    def test_manual_enabled_checks(self, monkeypatch):
        self._patch(monkeypatch, "dmg", True)
        assert updates.should_check() is True

    def test_setting_off_suppresses(self, monkeypatch):
        self._patch(monkeypatch, "dmg", False)
        assert updates.should_check() is False

    def test_auto_channel_never_nags_even_if_enabled(self, monkeypatch):
        # The whole point: a Store user must never be told to download an
        # installer (it would conflict with the store-managed copy).
        self._patch(monkeypatch, "msix", True)
        assert updates.should_check() is False


class TestPickDownloadUrl:
    ASSETS = [
        {"name": "jellytoast-0.1.6-macos-arm64.dmg", "browser_download_url": "ARM"},
        {"name": "jellytoast-0.1.6-macos-x86_64.dmg", "browser_download_url": "X64"},
        {"name": "jellytoast_0.1.6_amd64.deb", "browser_download_url": "DEB"},
        {"name": "jellytoast-0.1.6-x86_64.AppImage", "browser_download_url": "APP"},
    ]

    def test_deb(self):
        assert updates._pick_download_url(self.ASSETS, "deb", "PAGE") == "DEB"

    def test_appimage(self):
        assert updates._pick_download_url(self.ASSETS, "appimage", "PAGE") == "APP"

    def test_dmg_picks_by_arch(self, monkeypatch):
        monkeypatch.setattr(platform, "machine", lambda: "arm64")
        assert updates._pick_download_url(self.ASSETS, "dmg", "PAGE") == "ARM"
        monkeypatch.setattr(platform, "machine", lambda: "x86_64")
        assert updates._pick_download_url(self.ASSETS, "dmg", "PAGE") == "X64"

    def test_no_match_falls_back_to_release_page(self):
        # source / pip / unknown asset name → the releases page, never a wrong file.
        assert updates._pick_download_url(self.ASSETS, "source", "PAGE") == "PAGE"
        assert updates._pick_download_url([], "deb", "PAGE") == "PAGE"
