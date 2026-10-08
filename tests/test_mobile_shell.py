"""Plasma Mobile quick fixes: mobile-shell detection, the MPRIS desktop
entry, and what closing the main window does where no tray is drawn."""

from __future__ import annotations

import pytest

from jellytoast import platform_compat as pc


@pytest.fixture
def clean_env(monkeypatch):
    for var in ("PLASMA_PLATFORM", "QT_QUICK_CONTROLS_MOBILE", "FLATPAK_ID"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(pc, "IS_LINUX", True)
    return monkeypatch


class TestIsMobileShell:
    def test_plasma_mobile_platform(self, clean_env):
        clean_env.setenv("PLASMA_PLATFORM", "phone:handset")
        assert pc.is_mobile_shell()

    def test_quick_controls_mobile(self, clean_env):
        clean_env.setenv("QT_QUICK_CONTROLS_MOBILE", "true")
        assert pc.is_mobile_shell()

    def test_desktop(self, clean_env):
        clean_env.setenv("QT_QUICK_CONTROLS_MOBILE", "0")
        assert not pc.is_mobile_shell()

    def test_never_off_linux(self, clean_env):
        clean_env.setenv("PLASMA_PLATFORM", "phone:handset")
        clean_env.setattr(pc, "IS_LINUX", False)
        assert not pc.is_mobile_shell()


class TestDesktopEntryId:
    def _dirs(self, tmp_path, clean_env, layout):
        home, system = tmp_path / "home", tmp_path / "system"
        for root, names in ((home, layout.get("home", [])), (system, layout.get("system", []))):
            (root / "applications").mkdir(parents=True)
            for n in names:
                (root / "applications" / f"{n}.desktop").write_text("[Desktop Entry]\n")
        clean_env.setenv("XDG_DATA_HOME", str(home))
        clean_env.setenv("XDG_DATA_DIRS", str(system))

    def test_packaged_install_uses_reverse_dns(self, tmp_path, clean_env):
        self._dirs(tmp_path, clean_env, {"system": [pc.APP_ID]})
        assert pc.desktop_entry_id() == pc.APP_ID

    def test_user_level_dev_entry_wins(self, tmp_path, clean_env):
        # dev/create_desktop_entry.sh + a flatpak/system install side by side
        self._dirs(tmp_path, clean_env, {"home": ["jellytoast"], "system": [pc.APP_ID]})
        assert pc.desktop_entry_id() == "jellytoast"

    def test_flatpak_id(self, tmp_path, clean_env):
        self._dirs(tmp_path, clean_env, {"home": ["jellytoast"]})
        clean_env.setenv("FLATPAK_ID", pc.APP_ID)
        assert pc.desktop_entry_id() == pc.APP_ID

    def test_nothing_installed_defaults_to_app_id(self, tmp_path, clean_env):
        self._dirs(tmp_path, clean_env, {})
        assert pc.desktop_entry_id() == pc.APP_ID


class TestCloseBehaviour:
    @pytest.fixture
    def close(self, monkeypatch, isolated_settings):
        from jellytoast import app as app_mod
        from jellytoast.player_state import NowPlaying

        state = {"np": NowPlaying()}
        monkeypatch.setattr(app_mod, "get_now_playing", lambda: state["np"])
        return app_mod._close_hides_window, state, NowPlaying

    def test_mobile_keeps_playing_in_background(self, close, monkeypatch):
        fn, state, NP = close
        monkeypatch.setattr(pc, "is_mobile_shell", lambda: True)
        state["np"] = NP(item_id="t1", is_paused=False)
        assert fn(False) is True

    @pytest.mark.parametrize("np_kwargs", [{}, {"item_id": "t1", "is_paused": True}])
    def test_mobile_quits_when_not_playing(self, close, monkeypatch, np_kwargs):
        fn, state, NP = close
        monkeypatch.setattr(pc, "is_mobile_shell", lambda: True)
        state["np"] = NP(**np_kwargs)
        assert fn(False) is False

    def test_desktop_follows_the_tray_setting(self, close, monkeypatch):
        from jellytoast.settings import get_settings

        fn, _state, _NP = close
        monkeypatch.setattr(pc, "is_mobile_shell", lambda: False)
        get_settings().minimize_to_tray = True
        assert fn(False) is True
        get_settings().minimize_to_tray = False
        assert fn(False) is False

    def test_explicit_quit_always_quits(self, close, monkeypatch):
        fn, state, NP = close
        monkeypatch.setattr(pc, "is_mobile_shell", lambda: True)
        state["np"] = NP(item_id="t1", is_paused=False)
        assert fn(True) is False


class TestIsDocked:
    def _cfg(self, tmp_path, clean_env, text):
        clean_env.setenv("XDG_CONFIG_HOME", str(tmp_path))
        (tmp_path / "plasmamobilerc").write_text(text)

    def test_docked(self, tmp_path, clean_env):
        clean_env.setenv("PLASMA_PLATFORM", "phone:handset")
        self._cfg(tmp_path, clean_env, "[General]\nconvergenceModeEnabled=true\n")
        assert pc.is_docked()

    def test_not_docked(self, tmp_path, clean_env):
        clean_env.setenv("PLASMA_PLATFORM", "phone:handset")
        self._cfg(tmp_path, clean_env, "[General]\nconvergenceModeEnabled=false\n")
        assert not pc.is_docked()

    def test_key_in_another_group_is_ignored(self, tmp_path, clean_env):
        clean_env.setenv("PLASMA_PLATFORM", "phone:handset")
        self._cfg(tmp_path, clean_env, "[Other]\nconvergenceModeEnabled=true\n")
        assert not pc.is_docked()

    def test_never_off_a_mobile_shell(self, tmp_path, clean_env):
        self._cfg(tmp_path, clean_env, "[General]\nconvergenceModeEnabled=true\n")
        assert not pc.is_docked()

    def test_missing_file(self, tmp_path, clean_env):
        clean_env.setenv("PLASMA_PLATFORM", "phone:handset")
        clean_env.setenv("XDG_CONFIG_HOME", str(tmp_path))
        assert not pc.is_docked()


class TestSettingsOnAMobileShell:
    """No mini player and no tray on a phone shell, so Settings doesn't
    offer their toggles there (a toggle that does nothing reads as broken)."""

    @pytest.mark.parametrize("mobile", [True, False])
    def test_mini_player_and_tray_rows(self, qapp, isolated_settings, monkeypatch, mobile):
        from jellytoast.settings_dialog import SettingsDialog

        monkeypatch.setattr(pc, "is_mobile_shell", lambda: mobile)
        dlg = SettingsDialog()
        try:
            assert dlg._mini_check.isHidden() is mobile
            assert dlg._tray_check.isHidden() is mobile
            if hasattr(dlg, "_keep_above_check"):
                assert dlg._keep_above_check.isHidden() is mobile
        finally:
            dlg.deleteLater()


@pytest.mark.parametrize("mobile", [True, False])
def test_login_focuses_a_field_only_off_a_phone_shell(qapp, isolated_settings, monkeypatch, mobile):
    """On a phone, focusing a field at launch pops the on-screen keyboard
    over the form before the user has touched anything."""
    from jellytoast.login_view import LoginView

    monkeypatch.setattr(pc, "is_mobile_shell", lambda: mobile)
    view = LoginView()
    try:
        view.show()
        qapp.processEvents()
        focused = any(
            f.hasFocus() for f in (view._server_field, view._username_field, view._password_field)
        )
        assert focused is (not mobile)
    finally:
        view.deleteLater()
