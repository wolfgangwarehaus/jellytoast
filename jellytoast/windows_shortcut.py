"""Start-menu shortcut + real icon for the Windows pip/pipx install.

pip's entry-point launcher (``jellytoast.exe``) is a distlib stub whose
exe resources carry the generic Python-document icon — pip cannot patch
exe resources at install time, so Start search shows a Python doc page
instead of the brand mark (2026-06-10 Windows round). Linux solves app
discoverability with ``dev/create_desktop_entry.sh`` (.desktop + hicolor
icons); this module is the Windows equivalent, run automatically from
the post-show boot hook:

- Render ``%LOCALAPPDATA%/jellytoast/jellytoast.ico`` from the
  in-package brand SVG. The .ico is authored by hand as a single
  PNG-compressed entry (Vista+ shell reads those natively) so we don't
  depend on Qt shipping a writable ICO plugin.
- Write a per-user Start Menu shortcut (no elevation needed):
  ``%APPDATA%/Microsoft/Windows/Start Menu/Programs/jellytoast.lnk``
  targeting the launcher exe with that icon. ``WScript.Shell`` COM via
  a hidden PowerShell is the only stdlib-only way to author a .lnk.

Best-effort and idempotent: a marker file next to the .ico records the
exe the shortcut was last written for, so boots after the first are a
couple of ``Path.exists()`` calls. ``JT_NO_START_MENU_SHORTCUT=1`` opts
out (and a source-checkout ``python -m jellytoast`` run has no launcher
exe to target, so it never fires there).
"""

from __future__ import annotations

import logging
import os
import struct
import subprocess
import sys
from pathlib import Path

from jellytoast.platform_compat import IS_WINDOWS

logger = logging.getLogger(__name__)

_ICON_PX = 256

# Stable Windows taskbar identity. Without an explicit AppUserModelID
# the shell derives one from the process exe — the distlib launcher stub
# (or python.exe on a source run) — so the taskbar button groups under
# Python's identity and shows the generic Python-document icon instead
# of the brand mark, even though Qt's window icon is correct.
APP_USER_MODEL_ID = "wolfgangwarehaus.jellytoast"


def set_process_app_user_model_id() -> None:
    """Pin this process's taskbar identity to ours. Must run before the
    first top-level window exists (the shell samples the AUMID when the
    taskbar button is created). No-op off Windows; best-effort on it.

    The Start-menu .lnk carries the same AUMID (stamped via
    IPropertyStore in ``_shortcut_script``), so the shell resolves this
    group to that shortcut — its icon serves every launch shape (Start
    menu, Run-key autostart, bare exe, ``python -m``), and pinning the
    running window relaunches through it correctly."""
    if not IS_WINDOWS:
        return
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            APP_USER_MODEL_ID
        )
    except Exception as e:
        logger.debug("SetCurrentProcessExplicitAppUserModelID failed: %s", e)


def _launcher_exe() -> Path | None:
    """The launcher exe that (probably) started us. gui-script stubs put
    their own path in ``sys.argv[0]``; fall back to the venv's Scripts
    dir for odd launch shapes. None ⇒ no exe to target (source checkout,
    ``python -m jellytoast``)."""
    try:
        cand = Path(sys.argv[0] or "")
        if (
            cand.suffix.lower() == ".exe"
            and cand.stem.lower().startswith("jellytoast")
            and cand.is_file()
        ):
            return cand
        scripts = Path(sys.executable).parent / "jellytoast.exe"
        if scripts.is_file():
            return scripts
    except Exception:
        pass
    return None


def _icon_path() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
    return base / "jellytoast" / "jellytoast.ico"


def _shortcut_path() -> Path:
    base = Path(os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming"))
    return base / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "jellytoast.lnk"


def _marker_path() -> Path:
    return _icon_path().with_suffix(".target")


def _png_to_ico_bytes(png_bytes: bytes, size: int = _ICON_PX) -> bytes:
    """Wrap PNG bytes in a single-entry .ico container. The shell (Vista+)
    reads PNG-compressed entries natively, and hand-rolling the 22-byte
    header beats depending on an ICO image plugin. Width/height bytes use
    0 to mean 256 per the ICONDIR spec."""
    dim = 0 if size >= 256 else size
    header = struct.pack("<HHH", 0, 1, 1)  # reserved, type=icon, count=1
    entry = struct.pack(
        "<BBBBHHII",
        dim,  # width
        dim,  # height
        0,  # palette count (none)
        0,  # reserved
        1,  # color planes
        32,  # bits per pixel
        len(png_bytes),
        22,  # payload offset: 6-byte header + 16-byte entry
    )
    return header + entry + png_bytes


def _render_icon(path: Path) -> bool:
    """Render the brand mark to ``path`` as a PNG-compressed .ico.
    GUI-thread only — ``make_app_icon`` returns a QPixmap."""
    try:
        from PySide6.QtCore import QBuffer, QIODevice

        from jellytoast.ui_helpers import make_app_icon

        pm = make_app_icon(_ICON_PX)
        if pm is None or pm.isNull():
            return False
        buf = QBuffer()
        buf.open(QIODevice.OpenModeFlag.WriteOnly)
        if not pm.toImage().save(buf, "PNG"):
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(_png_to_ico_bytes(bytes(buf.data()), _ICON_PX))
        return True
    except Exception as e:
        logger.debug("start-menu icon render failed: %s", e)
        return False


def _ps_quote(p: Path) -> str:
    """Single-quoted PowerShell string literal — '' escapes a quote."""
    return "'" + str(p).replace("'", "''") + "'"


# C# shim compiled in-session by PowerShell's Add-Type: writes
# System.AppUserModel.ID onto a .lnk via IShellLink's IPropertyStore.
# WScript.Shell cannot author that property, and without it the taskbar
# has no icon source for our AppUserModelID group — every launch shape
# that isn't the Start-menu shortcut itself (Run-key autostart, the
# bare exe, python -m) showed the generic python icon. With the stamp,
# the shell resolves the group to this .lnk and uses its IconLocation
# everywhere. PKEY_AppUserModel_ID = {9F4C2855-...}/5 per propkey.h.
_APPID_CSHARP = """
using System;
using System.Runtime.InteropServices;

namespace JT {
  [StructLayout(LayoutKind.Sequential, Pack = 4)]
  public struct PropertyKey {
    public Guid fmtid; public uint pid;
    public PropertyKey(Guid f, uint p) { fmtid = f; pid = p; }
  }

  [StructLayout(LayoutKind.Explicit)]
  public struct PropVariant {
    [FieldOffset(0)] public ushort vt;
    [FieldOffset(8)] public IntPtr p;
  }

  [ComImport, Guid("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99"),
   InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
  public interface IPropertyStore {
    int GetCount(out uint count);
    int GetAt(uint index, out PropertyKey key);
    int GetValue(ref PropertyKey key, out PropVariant value);
    int SetValue(ref PropertyKey key, ref PropVariant value);
    int Commit();
  }

  [ComImport, Guid("0000010b-0000-0000-C000-000000000046"),
   InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
  public interface IPersistFile {
    void GetClassID(out Guid pClassID);
    [PreserveSig] int IsDirty();
    void Load([MarshalAs(UnmanagedType.LPWStr)] string f, uint mode);
    void Save([MarshalAs(UnmanagedType.LPWStr)] string f,
              [MarshalAs(UnmanagedType.Bool)] bool remember);
    void SaveCompleted([MarshalAs(UnmanagedType.LPWStr)] string f);
    void GetCurFile(out IntPtr name);
  }

  public static class Lnk {
    public static void SetAppId(string path, string appId) {
      var clsid = new Guid("00021401-0000-0000-C000-000000000046");
      object link = Activator.CreateInstance(Type.GetTypeFromCLSID(clsid));
      ((IPersistFile)link).Load(path, 2 /* STGM_READWRITE */);
      var key = new PropertyKey(
          new Guid("9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3"), 5);
      var v = new PropVariant();
      v.vt = 31; /* VT_LPWSTR */
      v.p = Marshal.StringToCoTaskMemUni(appId);
      var store = (IPropertyStore)link;
      store.SetValue(ref key, ref v);
      store.Commit();
      ((IPersistFile)link).Save(path, true);
      Marshal.FreeCoTaskMem(v.p);
    }
  }
}
"""


def _shortcut_script(lnk: Path, exe: Path, ico: Path) -> str:
    return (
        "$ws = New-Object -ComObject WScript.Shell; "
        f"$s = $ws.CreateShortcut({_ps_quote(lnk)}); "
        f"$s.TargetPath = {_ps_quote(exe)}; "
        f"$s.WorkingDirectory = {_ps_quote(exe.parent)}; "
        f"$s.IconLocation = {_ps_quote(ico)} + ',0'; "
        "$s.Description = 'jellytoast — audio-first Jellyfin / Subsonic music client'; "
        "$s.Save();\n"
        f"Add-Type -TypeDefinition @'\n{_APPID_CSHARP}\n'@;\n"
        f"[JT.Lnk]::SetAppId({_ps_quote(lnk)}, '{APP_USER_MODEL_ID}')"
    )


def _write_shortcut(lnk: Path, exe: Path, ico: Path) -> bool:
    """Author the .lnk via WScript.Shell COM in a hidden PowerShell.
    Blocking (a one-shot ~100 ms spawn) — callers run it off the GUI
    thread via run_async."""
    try:
        lnk.parent.mkdir(parents=True, exist_ok=True)
        r = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-WindowStyle",
                "Hidden",
                "-Command",
                _shortcut_script(lnk, exe, ico),
            ],
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            capture_output=True,
            timeout=30,
        )
        if r.returncode != 0:
            logger.debug(
                "start-menu shortcut write failed (%s): %s",
                r.returncode,
                r.stderr.decode(errors="replace")[:200],
            )
            return False
        return True
    except Exception as e:
        logger.debug("start-menu shortcut write failed: %s", e)
        return False


def _marker_value(exe: Path) -> str:
    # AUMID included so installs whose shortcut predates the property
    # stamp resync once and pick it up.
    return f"{exe}|{APP_USER_MODEL_ID}"


def _is_current(exe: Path) -> bool:
    """True when the shortcut + icon exist and were written for this exe
    (the marker records the target so a venv move re-syncs)."""
    try:
        return (
            _shortcut_path().exists()
            and _icon_path().exists()
            and _marker_path().read_text(encoding="utf-8").strip()
            == _marker_value(exe)
        )
    except Exception:
        return False


def sync() -> None:
    """Ensure the Start-menu shortcut exists and points at the current
    launcher exe. Call from the GUI thread post-show — the icon render
    is a few ms of QPixmap work; the PowerShell .lnk authoring is
    dispatched to the shared pool. No-op off Windows, when opted out,
    on a no-exe launch, or when everything is already current."""
    if not IS_WINDOWS or os.environ.get("JT_NO_START_MENU_SHORTCUT"):
        return
    exe = _launcher_exe()
    if exe is None:
        return
    if _is_current(exe):
        return
    ico = _icon_path()
    if not ico.exists() and not _render_icon(ico):
        return
    lnk = _shortcut_path()

    def _go() -> bool:
        return _write_shortcut(lnk, exe, ico)

    def _done(ok: bool) -> None:
        if not ok:
            return
        try:
            _marker_path().write_text(_marker_value(exe), encoding="utf-8")
        except Exception:
            pass
        logger.info("start-menu shortcut synced: %s -> %s", lnk, exe)

    from jellytoast.async_io import run_async

    run_async(_go, on_result=_done)
