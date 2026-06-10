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
        assert marker.read_text(encoding="utf-8") == str(exe)
