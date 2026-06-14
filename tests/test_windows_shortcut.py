"""Windows Start-menu shortcut bootstrap (jellytoast/windows_shortcut.py).

The pip gui-script stub ships the generic Python icon (2026-06-10
Windows round); the bootstrap writes a per-user .lnk + a hand-rolled
PNG-compressed .ico. The COM/PowerShell authoring itself is
Windows-only and exercised on the laptop; these pin the cross-platform
logic: the ICO container format, PowerShell quoting, exe resolution,
the idempotence marker, and the off-Windows no-op.
"""

from __future__ import annotations

import struct
from pathlib import Path

from jellytoast import windows_shortcut as ws


class TestIcoContainer:
    def test_header_entry_and_payload(self):
        png = b"\x89PNG-fake-payload"
        blob = ws._png_to_ico_bytes(png, 256)
        reserved, kind, count = struct.unpack("<HHH", blob[:6])
        assert (reserved, kind, count) == (0, 1, 1)
        w, h, _pal, _res, planes, bpp, size, offset = struct.unpack(
            "<BBBBHHII", blob[6:22]
        )
        # 0 means 256 per the ICONDIR spec.
        assert (w, h) == (0, 0)
        assert (planes, bpp) == (1, 32)
        assert size == len(png) and offset == 22
        assert blob[22:] == png

    def test_sub_256_sizes_keep_real_dimensions(self):
        blob = ws._png_to_ico_bytes(b"x", 48)
        w, h = blob[6], blob[7]
        assert (w, h) == (48, 48)


class TestPowershellAuthoring:
    def test_quote_escapes_apostrophes(self):
        # A username like O'Brien must not break out of the PS literal.
        assert ws._ps_quote(Path("C:/Users/O'Brien/x.lnk")) == "'C:\\Users\\O''Brien\\x.lnk'" or (
            "O''Brien" in ws._ps_quote(Path("C:/Users/O'Brien/x.lnk"))
        )

    def test_script_carries_target_icon_and_workdir(self):
        script = ws._shortcut_script(
            Path("C:/sm/jellytoast.lnk"),
            Path("C:/venv/Scripts/jellytoast.exe"),
            Path("C:/icons/jellytoast.ico"),
        )
        assert "CreateShortcut" in script
        assert "jellytoast.exe" in script
        assert "jellytoast.ico" in script and ",0'" in script
        assert "Scripts" in script  # working directory = exe's folder


class TestLauncherResolution:
    def test_non_exe_argv_falls_back_then_none(self, monkeypatch, tmp_path):
        import sys

        monkeypatch.setattr(sys, "argv", ["jellytoast"])
        monkeypatch.setattr(sys, "executable", str(tmp_path / "python"))
        assert ws._launcher_exe() is None

    def test_exe_argv_wins(self, monkeypatch, tmp_path):
        import sys

        exe = tmp_path / "jellytoast.exe"
        exe.write_bytes(b"MZ")
        monkeypatch.setattr(sys, "argv", [str(exe)])
        assert ws._launcher_exe() == exe

    def test_scripts_dir_fallback(self, monkeypatch, tmp_path):
        import sys

        scripts = tmp_path / "Scripts"
        scripts.mkdir()
        exe = scripts / "jellytoast.exe"
        exe.write_bytes(b"MZ")
        monkeypatch.setattr(sys, "argv", ["something-else"])
        monkeypatch.setattr(sys, "executable", str(scripts / "python.exe"))
        assert ws._launcher_exe() == exe


class TestSyncGates:
    def test_noop_off_windows(self, monkeypatch):
        # On this (Linux) box sync() must return before touching anything.
        called = []
        monkeypatch.setattr(ws, "_launcher_exe", lambda: called.append(1))
        ws.sync()
        if not ws.IS_WINDOWS:
            assert called == []

    def test_opt_out_env(self, monkeypatch):
        monkeypatch.setattr(ws, "IS_WINDOWS", True)
        monkeypatch.setenv("JT_NO_START_MENU_SHORTCUT", "1")
        called = []
        monkeypatch.setattr(ws, "_launcher_exe", lambda: called.append(1))
        ws.sync()
        assert called == []

    def test_marker_makes_it_idempotent(self, monkeypatch, tmp_path):
        exe = tmp_path / "jellytoast.exe"
        exe.write_bytes(b"MZ")
        ico = tmp_path / "jellytoast.ico"
        ico.write_bytes(b"icon")
        lnk = tmp_path / "jellytoast.lnk"
        lnk.write_bytes(b"lnk")
        marker = tmp_path / "jellytoast.target"
        marker.write_text(str(exe), encoding="utf-8")
        monkeypatch.setattr(ws, "IS_WINDOWS", True)
        monkeypatch.delenv("JT_NO_START_MENU_SHORTCUT", raising=False)
        monkeypatch.setattr(ws, "_launcher_exe", lambda: exe)
        monkeypatch.setattr(ws, "_icon_path", lambda: ico)
        monkeypatch.setattr(ws, "_shortcut_path", lambda: lnk)
        monkeypatch.setattr(ws, "_marker_path", lambda: marker)
        rendered = []
        monkeypatch.setattr(ws, "_render_icon", lambda p: rendered.append(p))
        ws.sync()  # current → no render, no authoring dispatch
        assert rendered == []

    def test_stale_marker_triggers_resync(self, monkeypatch, tmp_path, qapp):
        exe = tmp_path / "jellytoast.exe"
        exe.write_bytes(b"MZ")
        ico = tmp_path / "jellytoast.ico"
        ico.write_bytes(b"icon")
        lnk = tmp_path / "jellytoast.lnk"
        lnk.write_bytes(b"lnk")
        marker = tmp_path / "jellytoast.target"
        marker.write_text("C:/old/venv/jellytoast.exe", encoding="utf-8")
        monkeypatch.setattr(ws, "IS_WINDOWS", True)
        monkeypatch.delenv("JT_NO_START_MENU_SHORTCUT", raising=False)
        monkeypatch.setattr(ws, "_launcher_exe", lambda: exe)
        monkeypatch.setattr(ws, "_icon_path", lambda: ico)
        monkeypatch.setattr(ws, "_shortcut_path", lambda: lnk)
        monkeypatch.setattr(ws, "_marker_path", lambda: marker)
        wrote = []
        monkeypatch.setattr(
            ws, "_write_shortcut", lambda lnk, exe, ico: wrote.append(exe) or True
        )
        # run_async patched to run inline so the marker lands synchronously.
        import jellytoast.async_io as aio

        monkeypatch.setattr(
            aio,
            "run_async",
            lambda fn, on_result=None, on_error=None: on_result(fn())
            if on_result
            else fn(),
        )
        ws.sync()
        assert wrote == [exe]
        assert marker.read_text(encoding="utf-8") == ws._marker_value(exe)


class TestAppUserModelIdStamp:
    """The .lnk must carry System.AppUserModel.ID — without it the
    taskbar has no icon source for our AUMID group, and every non-
    Start-menu launch (autostart, bare exe, python -m) shows the
    generic python icon (Win 11 live find 2026-06-12)."""

    _LNK = Path("C:/Users/u/Start Menu/Programs/jellytoast.lnk")
    _EXE = Path("C:/Users/u/pipx/venvs/jellytoast/Scripts/jellytoast.exe")
    _ICO = Path("C:/Users/u/AppData/Local/jellytoast/jellytoast.ico")

    def test_script_stamps_app_user_model_id(self):
        script = ws._shortcut_script(self._LNK, self._EXE, self._ICO)
        # PKEY_AppUserModel_ID fmtid + pid 5, and our id as the value.
        assert "9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3" in script
        assert ", 5)" in script
        assert f"'{ws.APP_USER_MODEL_ID}')" in script
        # WScript.Shell authoring still present and runs FIRST.
        assert script.index("CreateShortcut") < script.index("SetAppId")
        # Errors must terminate so a failed stamp can't print the
        # sentinel, and the sentinel must be the last thing emitted.
        assert "$ErrorActionPreference = 'Stop'" in script
        assert script.rstrip().endswith(f"'{ws._STAMP_SENTINEL}'")

    def test_here_string_is_line_aligned(self):
        """PowerShell here-strings demand @' at end-of-line and '@ at
        start-of-line — an indent regression silently breaks the stamp."""
        script = ws._shortcut_script(self._LNK, self._EXE, self._ICO)
        lines = script.splitlines()
        assert any(line.endswith("@'") for line in lines)
        assert any(line.startswith("'@") for line in lines)

    def test_encoded_command_round_trips_utf16(self):
        """-EncodedCommand expects base64 of UTF-16LE; the decode must
        reproduce the script verbatim (the `-Command` string form
        mangled the here-string on some hosts — this is the fix)."""
        import base64

        script = ws._shortcut_script(self._LNK, self._EXE, self._ICO)
        encoded = ws._encode_ps(script)
        assert base64.b64decode(encoded).decode("utf-16-le") == script

    def test_marker_includes_aumid_so_old_installs_resync(
        self, tmp_path, monkeypatch
    ):
        """A marker written before the AUMID stamp (bare exe path) must
        NOT count as current — the shortcut needs one rewrite to gain
        the property."""
        marker = tmp_path / "jellytoast.target"
        lnk = tmp_path / "jellytoast.lnk"
        ico = tmp_path / "jellytoast.ico"
        lnk.write_text("x")
        ico.write_text("x")
        monkeypatch.setattr(ws, "_marker_path", lambda: marker)
        monkeypatch.setattr(ws, "_shortcut_path", lambda: lnk)
        monkeypatch.setattr(ws, "_icon_path", lambda: ico)

        marker.write_text(str(self._EXE), encoding="utf-8")  # pre-stamp
        assert ws._is_current(self._EXE) is False

        marker.write_text(ws._marker_value(self._EXE), encoding="utf-8")
        assert ws._is_current(self._EXE) is True
