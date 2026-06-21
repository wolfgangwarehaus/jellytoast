"""Cross-platform autostart backend smoke tests. All filesystem and
environment interactions are mocked so the suite runs identically on
Linux CI workers and on headless / non-Linux dev machines.

The autostart package picks its backend at import time via
`jellytoast.platform_compat.IS_LINUX`, so switching backends means flipping
that flag and reloading the package.
"""

from __future__ import annotations

import importlib
import sys

import pytest


def _reload_autostart():
    """Drop and re-import the package so its import-time backend gate
    re-evaluates against the current `platform_compat` flags / mocks."""
    for mod_name in (
        "jellytoast.autostart",
        "jellytoast.autostart._linux",
        "jellytoast.autostart._windows",
        "jellytoast.autostart._unsupported",
    ):
        sys.modules.pop(mod_name, None)
    return importlib.import_module("jellytoast.autostart")


def _force_linux(monkeypatch):
    import jellytoast.platform_compat as pc

    monkeypatch.setattr(pc, "IS_LINUX", True)
    monkeypatch.setattr(pc, "IS_WINDOWS", False)


def _force_windows(monkeypatch):
    import jellytoast.platform_compat as pc

    monkeypatch.setattr(pc, "IS_LINUX", False)
    monkeypatch.setattr(pc, "IS_WINDOWS", True)


def _force_non_linux(monkeypatch):
    import jellytoast.platform_compat as pc

    monkeypatch.setattr(pc, "IS_LINUX", False)
    monkeypatch.setattr(pc, "IS_WINDOWS", False)


def test_imports_cleanly_on_linux(monkeypatch):
    _force_linux(monkeypatch)
    autostart = _reload_autostart()
    assert hasattr(autostart, "enable")
    assert hasattr(autostart, "disable")
    assert hasattr(autostart, "is_enabled")
    assert hasattr(autostart, "is_supported")


def test_imports_cleanly_on_non_linux(monkeypatch):
    _force_non_linux(monkeypatch)
    autostart = _reload_autostart()
    assert hasattr(autostart, "enable")
    assert hasattr(autostart, "disable")
    assert hasattr(autostart, "is_enabled")
    assert hasattr(autostart, "is_supported")


def test_linux_backend_selected_when_linux(monkeypatch):
    _force_linux(monkeypatch)
    autostart = _reload_autostart()
    assert autostart._backend.__name__ == "jellytoast.autostart._linux"


def test_unsupported_backend_selected_when_not_linux(monkeypatch):
    _force_non_linux(monkeypatch)
    autostart = _reload_autostart()
    assert autostart._backend.__name__ == "jellytoast.autostart._unsupported"


def test_windows_backend_selected_when_windows(monkeypatch):
    _force_windows(monkeypatch)
    autostart = _reload_autostart()
    assert autostart._backend.__name__ == "jellytoast.autostart._windows"


def test_unsupported_methods_all_return_false(monkeypatch):
    _force_non_linux(monkeypatch)
    autostart = _reload_autostart()
    assert autostart.is_supported() is False
    assert autostart.is_enabled() is False
    assert autostart.enable() is False
    assert autostart.disable() is False


def test_is_supported_returns_bool():
    autostart = _reload_autostart()
    assert isinstance(autostart.is_supported(), bool)


def test_enable_writes_desktop_file(monkeypatch, tmp_path):
    _force_linux(monkeypatch)
    autostart = _reload_autostart()

    from jellytoast.autostart import _linux

    autostart_dir = tmp_path / "autostart"
    desktop = autostart_dir / "jellytoast.desktop"
    src = tmp_path / "share" / "jellytoast.desktop"
    monkeypatch.setattr(_linux, "_AUTOSTART_DIR", autostart_dir)
    monkeypatch.setattr(_linux, "_AUTOSTART_FILE", desktop)
    monkeypatch.setattr(_linux, "_SOURCE_DESKTOP", src)

    assert autostart.enable() is True
    assert desktop.exists()
    body = desktop.read_text()
    # Should be the synthesized minimal entry.
    assert "[Desktop Entry]" in body
    assert "Name=jellytoast" in body


def test_enable_copies_source_desktop_when_available(monkeypatch, tmp_path):
    _force_linux(monkeypatch)
    autostart = _reload_autostart()

    from jellytoast.autostart import _linux

    autostart_dir = tmp_path / "autostart"
    desktop = autostart_dir / "jellytoast.desktop"
    src = tmp_path / "share" / "jellytoast.desktop"
    src.parent.mkdir(parents=True)
    src.write_text(
        "[Desktop Entry]\nType=Application\nName=jellytoast\n"
        "Hidden=true\nExec=/opt/jellytoast/jellytoast\n"
    )

    monkeypatch.setattr(_linux, "_AUTOSTART_DIR", autostart_dir)
    monkeypatch.setattr(_linux, "_AUTOSTART_FILE", desktop)
    monkeypatch.setattr(_linux, "_SOURCE_DESKTOP", src)

    assert autostart.enable() is True
    body = desktop.read_text()
    # Hidden flag must be stripped when re-enabling.
    assert "Hidden=true" not in body
    assert "Exec=/opt/jellytoast/jellytoast" in body


def test_disable_returns_false_when_nothing_to_remove(monkeypatch, tmp_path):
    _force_linux(monkeypatch)
    autostart = _reload_autostart()

    from jellytoast.autostart import _linux

    monkeypatch.setattr(_linux, "_AUTOSTART_FILE", tmp_path / "missing.desktop")
    assert autostart.disable() is False


def test_disable_removes_existing_file(monkeypatch, tmp_path):
    _force_linux(monkeypatch)
    autostart = _reload_autostart()

    from jellytoast.autostart import _linux

    desktop = tmp_path / "jellytoast.desktop"
    desktop.write_text("[Desktop Entry]\nName=jellytoast\n")
    monkeypatch.setattr(_linux, "_AUTOSTART_FILE", desktop)

    assert autostart.disable() is True
    assert not desktop.exists()


def test_is_enabled_false_when_missing(monkeypatch, tmp_path):
    _force_linux(monkeypatch)
    autostart = _reload_autostart()

    from jellytoast.autostart import _linux

    monkeypatch.setattr(_linux, "_AUTOSTART_FILE", tmp_path / "missing.desktop")
    assert autostart.is_enabled() is False


def test_is_enabled_false_when_hidden_flag_set(monkeypatch, tmp_path):
    _force_linux(monkeypatch)
    autostart = _reload_autostart()

    from jellytoast.autostart import _linux

    desktop = tmp_path / "jellytoast.desktop"
    desktop.write_text(
        "[Desktop Entry]\nName=jellytoast\nHidden=true\n"
    )
    monkeypatch.setattr(_linux, "_AUTOSTART_FILE", desktop)
    assert autostart.is_enabled() is False


def test_is_enabled_false_when_gnome_flag_disabled(monkeypatch, tmp_path):
    _force_linux(monkeypatch)
    autostart = _reload_autostart()

    from jellytoast.autostart import _linux

    desktop = tmp_path / "jellytoast.desktop"
    desktop.write_text(
        "[Desktop Entry]\nName=jellytoast\nX-GNOME-Autostart-enabled=false\n"
    )
    monkeypatch.setattr(_linux, "_AUTOSTART_FILE", desktop)
    assert autostart.is_enabled() is False


def test_is_enabled_true_when_active_entry_present(monkeypatch, tmp_path):
    _force_linux(monkeypatch)
    autostart = _reload_autostart()

    from jellytoast.autostart import _linux

    desktop = tmp_path / "jellytoast.desktop"
    desktop.write_text("[Desktop Entry]\nName=jellytoast\n")
    monkeypatch.setattr(_linux, "_AUTOSTART_FILE", desktop)
    assert autostart.is_enabled() is True


def test_enable_returns_false_when_filesystem_hostile(monkeypatch, tmp_path):
    """If mkdir blows up (read-only home), the public API must not
    raise — it must just return False so the settings UI can tell the
    user the toggle failed."""
    _force_linux(monkeypatch)
    autostart = _reload_autostart()

    from jellytoast.autostart import _linux

    class BadPath:
        def mkdir(self, *args, **kwargs):
            raise PermissionError("read-only home")

    monkeypatch.setattr(_linux, "_AUTOSTART_DIR", BadPath())
    # _AUTOSTART_FILE never gets touched on this path; leave it alone.
    assert autostart.enable() is False


# ---------------------------------------------------------------------------
# Windows backend — winreg is absent on Linux CI, so the tests run the
# backend against an in-memory fake patched over the module attribute.
# ---------------------------------------------------------------------------


class _FakeWinreg:
    """Minimal in-memory winreg: just the API surface _windows.py uses.
    Mirrors real winreg error behavior — missing keys/values raise
    FileNotFoundError."""

    HKEY_CURRENT_USER = "HKCU"
    KEY_READ = 0x20019
    KEY_SET_VALUE = 0x0002
    REG_SZ = 1

    class _Key:
        def __init__(self, values):
            self.values = values

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def __init__(self):
        self.keys = {}  # path -> {value_name: value}

    def OpenKey(self, root, path, reserved, access):
        if path not in self.keys:
            raise FileNotFoundError(2, "key not found", path)
        return self._Key(self.keys[path])

    def CreateKeyEx(self, root, path, reserved, access):
        return self._Key(self.keys.setdefault(path, {}))

    def QueryValueEx(self, key, name):
        if name not in key.values:
            raise FileNotFoundError(2, "value not found", name)
        return key.values[name], self.REG_SZ

    def SetValueEx(self, key, name, reserved, kind, value):
        key.values[name] = value

    def DeleteValue(self, key, name):
        if name not in key.values:
            raise FileNotFoundError(2, "value not found", name)
        del key.values[name]


def _windows_backend(monkeypatch, fake=None):
    """Reload the package forced to Windows and patch the fake registry
    over the backend's winreg attribute. Returns (autostart, _windows, fake)."""
    _force_windows(monkeypatch)
    autostart = _reload_autostart()
    from jellytoast.autostart import _windows

    fake = fake or _FakeWinreg()
    monkeypatch.setattr(_windows, "winreg", fake)
    return autostart, _windows, fake


def test_windows_unsupported_without_winreg(monkeypatch):
    _force_windows(monkeypatch)
    autostart = _reload_autostart()
    from jellytoast.autostart import _windows

    monkeypatch.setattr(_windows, "winreg", None)
    assert autostart.is_supported() is False
    assert autostart.is_enabled() is False
    assert autostart.enable() is False
    assert autostart.disable() is False


def test_windows_supported_with_winreg(monkeypatch):
    autostart, _, _ = _windows_backend(monkeypatch)
    assert autostart.is_supported() is True


def test_windows_enable_writes_run_value(monkeypatch):
    autostart, _windows, fake = _windows_backend(monkeypatch)
    monkeypatch.setattr(_windows, "_launch_command", lambda: '"C:\\app\\jellytoast.exe"')

    assert autostart.enable() is True
    assert fake.keys[_windows._RUN_KEY]["jellytoast"] == '"C:\\app\\jellytoast.exe"'
    assert autostart.is_enabled() is True


def test_windows_is_enabled_false_when_value_absent(monkeypatch):
    autostart, _windows, fake = _windows_backend(monkeypatch)
    fake.keys[_windows._RUN_KEY] = {}  # Run key exists, our value doesn't
    assert autostart.is_enabled() is False


def test_windows_is_enabled_false_when_key_missing(monkeypatch):
    autostart, _, _ = _windows_backend(monkeypatch)
    assert autostart.is_enabled() is False


def test_windows_disable_removes_value(monkeypatch):
    autostart, _windows, fake = _windows_backend(monkeypatch)
    fake.keys[_windows._RUN_KEY] = {"jellytoast": '"C:\\app\\jellytoast.exe"'}

    assert autostart.disable() is True
    assert "jellytoast" not in fake.keys[_windows._RUN_KEY]
    assert autostart.is_enabled() is False


def test_windows_disable_returns_false_when_nothing_to_remove(monkeypatch):
    autostart, _windows, fake = _windows_backend(monkeypatch)
    fake.keys[_windows._RUN_KEY] = {}
    assert autostart.disable() is False


def test_windows_enable_returns_false_on_registry_error(monkeypatch):
    autostart, _windows, fake = _windows_backend(monkeypatch)

    def _boom(*a, **k):
        raise OSError(5, "access denied")

    monkeypatch.setattr(fake, "CreateKeyEx", _boom)
    assert autostart.enable() is False


def test_windows_launch_command_prefers_launcher_exe(monkeypatch, tmp_path):
    _, _windows, _ = _windows_backend(monkeypatch)
    import jellytoast.windows_shortcut as ws

    exe = tmp_path / "jellytoast.exe"
    monkeypatch.setattr(ws, "_launcher_exe", lambda: exe)
    assert _windows._launch_command() == f'"{exe}"'


def test_windows_launch_command_falls_back_to_module_run(monkeypatch):
    _, _windows, _ = _windows_backend(monkeypatch)
    import jellytoast.windows_shortcut as ws

    monkeypatch.setattr(ws, "_launcher_exe", lambda: None)
    cmd = _windows._launch_command()
    assert cmd.endswith(' -m jellytoast')
    assert sys.executable.rsplit("/", 1)[0] in cmd or "python" in cmd.lower()


@pytest.fixture(autouse=True)
def _restore_autostart_module():
    """Leave a fresh import after each test so the next test gets a
    clean module + backend reference."""
    yield
    for mod_name in (
        "jellytoast.autostart",
        "jellytoast.autostart._linux",
        "jellytoast.autostart._windows",
        "jellytoast.autostart._unsupported",
    ):
        sys.modules.pop(mod_name, None)
