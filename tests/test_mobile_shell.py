"""Plasma Mobile quick fixes: mobile-shell detection, the MPRIS desktop
entry, and what closing the main window does where no tray is drawn."""

from __future__ import annotations

import pytest

from jellytoast import platform_compat as pc


@pytest.fixture
def clean_env(monkeypatch):
    for var in ("PLASMA_PLATFORM", "QT_QUICK_CONTROLS_MOBILE", "FLATPAK_ID", "XDG_CURRENT_DESKTOP"):
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

    def test_phosh(self, clean_env):
        clean_env.setenv("XDG_CURRENT_DESKTOP", "Phosh:GNOME")
        assert pc.is_mobile_shell()
        assert pc.is_phosh()

    def test_gnome_is_not_phosh(self, clean_env):
        clean_env.setenv("XDG_CURRENT_DESKTOP", "GNOME")
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


class TestIsDockedPhosh:
    @pytest.fixture
    def phosh(self, tmp_path, clean_env):
        clean_env.setenv("XDG_CURRENT_DESKTOP", "Phosh:GNOME")
        clean_env.setenv("XDG_CONFIG_HOME", str(tmp_path))
        (tmp_path / "dconf").mkdir()
        db = tmp_path / "dconf" / "user"
        db.write_bytes(b"")
        clean_env.setattr(pc, "_phosh_docked_cache", (None, False))
        calls = []
        state = {"out": "true\n", "rc": 0}

        def fake_run(cmd, **_kw):
            import subprocess

            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, state["rc"], state["out"], "")

        clean_env.setattr("subprocess.run", fake_run)
        return db, state, calls

    def test_auto_maximize_off_means_docked(self, phosh):
        _db, state, calls = phosh
        state["out"] = "false\n"
        assert pc.is_docked()
        assert calls[0][1:] == ["get", "sm.puri.phoc", "auto-maximize"]

    def test_auto_maximize_on_means_undocked(self, phosh):
        assert not pc.is_docked()

    def test_missing_schema_means_undocked(self, phosh):
        _db, state, _calls = phosh
        state["rc"], state["out"] = 1, ""
        assert not pc.is_docked()

    def test_rereads_only_when_dconf_changes(self, phosh):
        import os

        db, state, calls = phosh
        assert not pc.is_docked()
        assert not pc.is_docked()
        assert len(calls) == 1
        state["out"] = "false\n"
        st = db.stat()
        os.utime(db, (st.st_atime, st.st_mtime + 5))
        assert pc.is_docked()
        assert len(calls) == 2

    def test_watch_path(self, phosh, tmp_path):
        assert pc.docked_config_path() == str(tmp_path / "dconf" / "user")


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
        # The window's focus child, not hasFocus(): that also needs the
        # window to be active, which depends on test order offscreen.
        fields = (view._server_field, view._username_field, view._password_field)
        focused = view.window().focusWidget() in fields
        assert focused is (not mobile)
    finally:
        view.deleteLater()


class TestPhoneScale:
    """A phone shell scales the whole UI (QT_SCALE_FACTOR, before Qt starts)."""

    @pytest.fixture
    def env(self, monkeypatch):
        for var in ("QT_SCALE_FACTOR", "JT_PHONE_SCALE"):
            monkeypatch.delenv(var, raising=False)
        return monkeypatch

    def _run(self, env, mobile):
        import os

        from jellytoast import app as app_mod

        env.setattr(pc, "is_mobile_shell", lambda: mobile)
        app_mod._apply_phone_scale()
        return os.environ.get("QT_SCALE_FACTOR")

    def test_phone_follows_the_shell_scale_by_default(self, env):
        # No extra scale stacked on the shell's own (402 px phone at 3× →
        # the app gets all 402 px, not 268).
        assert self._run(env, True) is None

    def test_desktop_untouched(self, env):
        assert self._run(env, False) is None

    def test_override_and_opt_out(self, env):
        env.setenv("JT_PHONE_SCALE", "1.75")
        assert self._run(env, True) == "1.75"
        env.delenv("QT_SCALE_FACTOR")
        env.setenv("JT_PHONE_SCALE", "1")
        assert self._run(env, True) is None

    def test_an_exported_scale_wins(self, env):
        env.setenv("QT_SCALE_FACTOR", "2")
        assert self._run(env, True) == "2"


def test_settings_checkbox_label_wraps_and_toggles(qapp):
    """Settings at 360 px (august, 2026-10-26) clipped one-line labels:
    "Hide to system tray when windo". WrapCheckBox wraps instead, and the
    whole row, label included, still toggles it."""
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtTest import QTest

    from jellytoast.settings_widgets import WrapCheckBox

    cb = WrapCheckBox("Show a notification when the track changes")
    one_line = cb.heightForWidth(2000)
    assert cb.heightForWidth(150) > one_line  # wraps when narrow
    assert cb.minimumSizeHint().width() < cb.sizeHint().width()
    cb.resize(150, cb.heightForWidth(150))
    cb.show()
    QTest.mouseClick(cb, Qt.MouseButton.LeftButton, pos=QPoint(140, cb.height() - 4))
    assert cb.isChecked()  # a click on the wrapped label's last line
    cb.setEnabled(False)
    QTest.mouseClick(cb, Qt.MouseButton.LeftButton, pos=QPoint(140, 4))
    assert cb.isChecked()  # a disabled option doesn't toggle
    cb.deleteLater()


_SETTINGS_PAGES = ["General", "Playback", "Casting", "Display", "Hotkeys", "Scrobbling"]


def test_settings_pages_fit_a_phone(qapp, isolated_settings, monkeypatch):
    """At phone scale the Settings page is ~370 px wide, and its scroll area
    doesn't scroll sideways: anything wider is clipped (the EQ's 16k band,
    Save / Delete, "(needs restart)", the Reset buttons)."""
    from jellytoast.settings_dialog import SettingsDialog

    monkeypatch.setattr(pc, "is_mobile_shell", lambda: True)
    dlg = SettingsDialog()
    try:
        for name in _SETTINGS_PAGES:
            assert dlg.show_page(name)
        qapp.processEvents()
        for builder, _lazy, lay in dlg._page_builders:
            if builder.__name__ == "_build_downloads":
                continue  # its own scroll area; covered below
            assert lay.parentWidget().minimumSizeHint().width() <= 360, builder.__name__
    finally:
        dlg.deleteLater()


class _FakeBar:
    def __init__(self):
        self._left_suppressed = False
        self.np_open = False

    def set_left_cluster_visible(self, visible):
        self._left_suppressed = not visible

    def set_now_playing_open(self, open_):
        self.np_open = open_


class _FakeNav:
    def __init__(self):
        from types import SimpleNamespace

        self.np_page = SimpleNamespace(_preview_id="")
        self.np_bar = _FakeBar()
        page = self.np_page
        self.content_stack = SimpleNamespace(currentWidget=lambda: page)


@pytest.mark.parametrize("compact", [True, False])
def test_compact_now_playing_hides_the_bar_cover(monkeypatch, compact):
    """The compact Now Playing page shows the cover big; the bar's copy steps
    out. Wide is left as it was."""
    from jellytoast import responsive
    from jellytoast.nav_controller import _NavMixin

    monkeypatch.setattr(responsive, "is_compact", lambda: compact)
    nav = _FakeNav()
    _NavMixin._sync_np_bar_cover(nav)
    assert nav.np_bar._left_suppressed is compact


def test_bar_cover_returns_when_leaving_compact(monkeypatch):
    from jellytoast import responsive
    from jellytoast.nav_controller import _NavMixin

    nav = _FakeNav()
    monkeypatch.setattr(responsive, "is_compact", lambda: True)
    _NavMixin._sync_np_bar_cover(nav)
    monkeypatch.setattr(responsive, "is_compact", lambda: False)
    _NavMixin._sync_np_bar_cover(nav)
    assert nav.np_bar._left_suppressed is False


def test_previewing_keeps_the_bar_cover(monkeypatch):
    """Browsing an album keeps the playing track visible in the bar."""
    from jellytoast import responsive
    from jellytoast.nav_controller import _NavMixin

    monkeypatch.setattr(responsive, "is_compact", lambda: True)
    nav = _FakeNav()
    nav.np_page._preview_id = "album-1"
    _NavMixin._sync_np_bar_cover(nav)
    assert nav.np_bar._left_suppressed is False


def test_bar_cover_returns_when_leaving_now_playing(monkeypatch):
    from types import SimpleNamespace

    from jellytoast import responsive
    from jellytoast.nav_controller import _NavMixin

    monkeypatch.setattr(responsive, "is_compact", lambda: True)
    nav = _FakeNav()
    nav.top_bar = SimpleNamespace(
        set_now_playing_mode=lambda *a, **k: None, set_active_tab=lambda label: None
    )
    nav._tab_for_surface = lambda w: ""
    _NavMixin._sync_np_bar_cover(nav)
    assert nav.np_bar._left_suppressed
    other = object()
    nav.content_stack = SimpleNamespace(currentWidget=lambda: other)
    _NavMixin._on_content_changed(nav, 1)
    assert not nav.np_bar._left_suppressed


@pytest.mark.parametrize("mobile", [True, False])
def test_smart_mix_editor_fits_a_phone(qapp, monkeypatch, mobile):
    """Side by side (form | preview) the editor needs ~710 px; on a phone the
    form stacks over the preview and each rule wraps to two lines."""
    from jellytoast.smart_playlist_editor import SmartPlaylistEditorDialog

    monkeypatch.setattr(pc, "is_mobile_shell", lambda: mobile)
    dlg = SmartPlaylistEditorDialog()
    try:
        dlg._add_chip(None)
        width = max(dlg.minimumSizeHint().width(), dlg.minimumWidth())
        assert (width <= 360) is mobile
    finally:
        dlg.deleteLater()


@pytest.mark.parametrize("mobile", [True, False])
def test_tag_editor_fits_a_phone(qapp, monkeypatch, mobile):
    from jellytoast.tag_editor import TagEditorDialog

    monkeypatch.setattr(pc, "is_mobile_shell", lambda: mobile)
    dlg = TagEditorDialog({"Id": "x", "Name": "Song", "Album": "Album"})
    try:
        width = max(dlg.minimumSizeHint().width(), dlg.minimumWidth())
        assert (width <= 360) is mobile
    finally:
        dlg.deleteLater()


@pytest.mark.parametrize("mobile", [True, False])
def test_cast_dialog_is_not_fixed_wider_than_a_phone(qapp, monkeypatch, mobile):
    from jellytoast.cast_dialog import CastDialog
    from tests.test_dialog_lifetime import _FakeCastManager

    monkeypatch.setattr(pc, "is_mobile_shell", lambda: mobile)
    dlg = CastDialog(_FakeCastManager(), parent=None)
    try:
        assert (dlg.maximumWidth() == 440) is not mobile
        assert dlg.minimumWidth() <= (360 if mobile else 440)
    finally:
        dlg.reject()


def test_compact_track_gutter():
    from jellytoast.np_track_list import _TrackDelegate

    d = _TrackDelegate()
    assert d.text_left() == 58
    d.set_compact(True)
    assert d.text_left() == 38
    d.set_compact(False)
    assert d.text_left() == 58
