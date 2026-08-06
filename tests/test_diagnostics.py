"""jellytoast.diagnostics — the support report: complete, bounded, secret-free.

The report is written to be pasted into a public GitHub issue, so the
redaction tests below are the load-bearing ones: they plant fake secrets in
every place jellytoast actually keeps one (the access token, a scrobbling
token, a password, an AirPlay pairing blob, an auth-carrying server URL) and
assert none of the values reach the output.
"""

from __future__ import annotations

import pytest

from jellytoast import diagnostics


@pytest.fixture(autouse=True)
def _no_log_tail(monkeypatch):
    """The log tail is exercised on its own below; everywhere else, pin it so
    a real installed log (another test's, or a dev's own) can't leak lines
    into these assertions."""
    from jellytoast import log as jlog

    monkeypatch.setattr(jlog, "_file_path", None)


class TestContents:
    def test_carries_identity_and_versions(self, qapp, isolated_settings):
        from jellytoast.version import __version__

        report = diagnostics.collect_report()
        assert "jellytoast diagnostics" in report
        assert __version__ in report
        assert "qt:" in report and "PySide6" in report
        assert "session:" in report
        assert "python:" in report

    def test_carries_the_jellytoast_specific_sections(self, qapp, isolated_settings):
        report = diagnostics.collect_report()
        for marker in ("app:", "os:", "mpv:", "theme:", "blur:", "provider:",
                       "server:", "offline mode:", "covers:"):
            assert marker in report, marker

    def test_reflects_settings_values(self, qapp, isolated_settings):
        isolated_settings.theme_mode = "light"
        report = diagnostics.collect_report()
        assert "mode=light" in report
        assert "ui/theme_mode = light" in report

    def test_provider_and_offline_state(self, qapp, isolated_settings, monkeypatch):
        isolated_settings.provider_kind = "subsonic"
        from jellytoast import offline

        monkeypatch.setattr(offline, "is_offline_mode", lambda: True)
        report = diagnostics.collect_report()
        assert "provider: subsonic" in report
        assert "offline mode: on" in report

    def test_cover_counters_present(self, qapp, isolated_settings):
        from jellytoast.ui_helpers import cover_pipeline_stats

        report = diagnostics.collect_report()
        for key in cover_pipeline_stats():
            assert f"{key}=" in report

    def test_binary_values_summarized(self, qapp, isolated_settings):
        from PySide6.QtCore import QByteArray

        isolated_settings._s.setValue("win/geometry", QByteArray(b"\x01\x02\x03\x04"))
        report = diagnostics.collect_report()
        assert "win/geometry = <binary 4B>" in report

    def test_survives_a_failing_probe(self, qapp, isolated_settings, monkeypatch):
        # A broken section must degrade to a line, never take the support path
        # down with it — this button gets pressed when things are ALREADY bad.
        from jellytoast import blur

        def _boom(*_a, **_kw):
            raise RuntimeError("compositor exploded")

        monkeypatch.setattr(blur, "status", _boom)
        report = diagnostics.collect_report()
        assert "blur: <unavailable>" in report


class TestRedaction:
    def test_planted_secrets_never_appear(self, qapp, isolated_settings):
        qs = isolated_settings._s
        qs.setValue("server/token", "v1:PLANTEDACCESSTOKEN")
        qs.setValue("credentials/api_token", "PLANTEDCREDBLOB")
        qs.setValue("scrobble/lastfm_password", "PLANTEDHUNTER2")
        qs.setValue("scrobble/listenbrainz_token", "PLANTEDLBTOKEN")
        qs.setValue("scrobble/lastfm_session_key", "PLANTEDSESSIONKEY")
        qs.setValue("airplay/pairing_credentials", "PLANTEDPAIRING")
        qs.setValue("server/subsonic_auth_mode_plain", "PLANTEDAUTHMODE")
        qs.setValue("ui/theme_mode", "dark")
        qs.sync()

        report = diagnostics.collect_report()
        for planted in (
            "PLANTEDACCESSTOKEN",
            "PLANTEDCREDBLOB",
            "PLANTEDHUNTER2",
            "PLANTEDLBTOKEN",
            "PLANTEDSESSIONKEY",
            "PLANTEDPAIRING",
            "PLANTEDAUTHMODE",
        ):
            assert planted not in report, planted
        # …and the key NAMES from the credentials subtree don't show up either
        # (the section header legitimately mentions the word "credentials").
        settings_section = report.split("---")[1]
        assert "credentials/api_token" not in settings_section
        assert "ui/theme_mode = dark" in report  # normal keys still present

    def test_server_url_reduced_to_scheme_and_host(self, qapp, isolated_settings):
        isolated_settings.server_url = (
            "https://music.example.com:8920/jf?api_key=PLANTEDAPIKEY&u=bob"
        )
        isolated_settings.username = "PLANTEDUSERNAME"
        report = diagnostics.collect_report()
        assert "server: https://music.example.com:8920" in report
        assert "PLANTEDAPIKEY" not in report
        assert "PLANTEDUSERNAME" not in report
        assert "/jf" not in report

    def test_signed_in_is_a_boolean_not_the_token(self, qapp, isolated_settings, monkeypatch):
        monkeypatch.setattr(
            type(isolated_settings),
            "access_token",
            property(lambda self: "PLANTEDLIVETOKEN"),
        )
        report = diagnostics.collect_report()
        assert "signed in: yes" in report
        assert "PLANTEDLIVETOKEN" not in report

    def test_unconfigured_server_says_so(self, qapp, isolated_settings):
        isolated_settings.server_url = ""
        assert "server: (not configured)" in diagnostics.collect_report()

    def test_never_touches_the_credentials_module(self, qapp, isolated_settings, monkeypatch):
        # Defence in depth: even the DECRYPTED-token path must not be reachable
        # from the report, so blow up if anything here calls into it.
        from jellytoast import credentials

        def _tripwire(*_a, **_kw):
            raise AssertionError("diagnostics must never read the keyring")

        monkeypatch.setattr(credentials, "_keyring_get_token", _tripwire)
        diagnostics.collect_report()  # must not raise


class TestLogTail:
    def test_tails_the_log_when_installed(self, qapp, isolated_settings, tmp_path, monkeypatch):
        from jellytoast import log as jlog

        logf = tmp_path / "jellytoast.log"
        logf.write_text(
            "\n".join(f"line {i}" for i in range(150)) + "\n", encoding="utf-8"
        )
        monkeypatch.setattr(jlog, "_file_path", logf)
        report = diagnostics.collect_report()
        assert "  line 149\n" in report  # the newest line survives
        assert "  line 10\n" not in report  # …the oldest of 150 is past the tail

    def test_explains_a_missing_log(self, qapp, isolated_settings):
        # The autouse fixture already pinned _file_path to None — i.e. the
        # "logging was never installed" shape.
        assert "file logging not installed" in diagnostics.collect_report()


class TestClipboard:
    def test_copy_to_clipboard(self, qapp, isolated_settings):
        from PySide6.QtWidgets import QApplication

        assert diagnostics.copy_to_clipboard() is True
        assert "jellytoast diagnostics" in QApplication.clipboard().text()

    def test_settings_dialog_button_copies(self, qapp, isolated_settings):
        from PySide6.QtWidgets import QApplication

        from jellytoast.settings_dialog import SettingsDialog

        dlg = SettingsDialog()
        try:
            dlg.show_page("General")
            dlg._diagnostics_btn.click()
            assert "jellytoast diagnostics" in QApplication.clipboard().text()
            assert dlg._diagnostics_note.text() != ""
        finally:
            dlg.close()
            dlg.deleteLater()
