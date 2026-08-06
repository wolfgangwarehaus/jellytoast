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
    re-evaluates against the current `platform_compat` flags / mocks.

    `_portal` is deliberately NOT dropped: keeping one module object alive
    across reloads is what lets the fixtures below patch it once (state
    file, fake bus) and have `_linux` see the patched version."""
    for mod_name in (
        "jellytoast.autostart",
        "jellytoast.autostart._linux",
        "jellytoast.autostart._windows",
        "jellytoast.autostart._macos",
        "jellytoast.autostart._unsupported",
    ):
        sys.modules.pop(mod_name, None)
    return importlib.import_module("jellytoast.autostart")


def _force_linux(monkeypatch):
    import jellytoast.platform_compat as pc

    monkeypatch.setattr(pc, "IS_LINUX", True)
    monkeypatch.setattr(pc, "IS_WINDOWS", False)
    monkeypatch.setattr(pc, "IS_MACOS", False)


def _force_windows(monkeypatch):
    import jellytoast.platform_compat as pc

    monkeypatch.setattr(pc, "IS_LINUX", False)
    monkeypatch.setattr(pc, "IS_WINDOWS", True)
    monkeypatch.setattr(pc, "IS_MACOS", False)


def _force_macos(monkeypatch):
    import jellytoast.platform_compat as pc

    monkeypatch.setattr(pc, "IS_LINUX", False)
    monkeypatch.setattr(pc, "IS_WINDOWS", False)
    monkeypatch.setattr(pc, "IS_MACOS", True)


def _force_non_linux(monkeypatch):
    """Neither Linux, Windows, nor macOS → the unsupported backend."""
    import jellytoast.platform_compat as pc

    monkeypatch.setattr(pc, "IS_LINUX", False)
    monkeypatch.setattr(pc, "IS_WINDOWS", False)
    monkeypatch.setattr(pc, "IS_MACOS", False)


@pytest.fixture(autouse=True)
def _no_real_portal(monkeypatch, tmp_path):
    """Keep every test off the real session bus and out of the real
    ~/.config.

    Default state: `jeepney` is unimportable, so the Background portal is
    unavailable and the Linux backend takes the .desktop path — which is
    what the pre-portal tests below assert. The portal tests install a fake
    jeepney over this to exercise the portal path."""
    monkeypatch.setitem(sys.modules, "jeepney", None)
    from jellytoast.autostart import _portal

    monkeypatch.setattr(
        _portal, "_STATE_FILE", tmp_path / "portal-state" / "autostart-portal"
    )
    monkeypatch.delenv("FLATPAK_ID", raising=False)
    yield


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
# macOS backend — a per-user LaunchAgent .plist. Pure stdlib (plistlib), so
# the plist content + write/remove logic is fully testable off a Mac.
# ---------------------------------------------------------------------------


def test_macos_backend_selected_when_macos(monkeypatch):
    _force_macos(monkeypatch)
    autostart = _reload_autostart()
    assert autostart._backend.__name__ == "jellytoast.autostart._macos"


def test_macos_is_supported(monkeypatch):
    _force_macos(monkeypatch)
    autostart = _reload_autostart()
    assert autostart.is_supported() is True


def test_macos_plist_is_valid_and_has_required_keys(monkeypatch):
    import plistlib

    _force_macos(monkeypatch)
    _reload_autostart()
    from jellytoast.autostart import _macos

    data = plistlib.loads(_macos._plist_bytes())
    assert data["Label"] == "io.github.wolfgangwarehaus.jellytoast"
    assert data["RunAtLoad"] is True
    assert isinstance(data["ProgramArguments"], list)
    assert data["ProgramArguments"]  # non-empty


def test_macos_program_arguments_source_run(monkeypatch):
    _force_macos(monkeypatch)
    _reload_autostart()
    from jellytoast.autostart import _macos

    monkeypatch.setattr(sys, "frozen", False, raising=False)
    args = _macos._program_arguments()
    assert args[-2:] == ["-m", "jellytoast"]


def test_macos_program_arguments_frozen_app(monkeypatch):
    _force_macos(monkeypatch)
    _reload_autostart()
    from jellytoast.autostart import _macos

    exe = "/Applications/jellytoast.app/Contents/MacOS/jellytoast"
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", exe)
    assert _macos._program_arguments() == [exe]


def test_macos_enable_writes_plist(monkeypatch, tmp_path):
    import plistlib

    _force_macos(monkeypatch)
    autostart = _reload_autostart()
    from jellytoast.autostart import _macos

    agents = tmp_path / "LaunchAgents"
    plist = agents / "io.github.wolfgangwarehaus.jellytoast.plist"
    monkeypatch.setattr(_macos, "_AGENTS_DIR", agents)
    monkeypatch.setattr(_macos, "_PLIST", plist)

    assert autostart.enable() is True
    assert plist.exists()
    data = plistlib.loads(plist.read_bytes())
    assert data["Label"] == "io.github.wolfgangwarehaus.jellytoast"
    assert autostart.is_enabled() is True


def test_macos_disable_removes_plist(monkeypatch, tmp_path):
    _force_macos(monkeypatch)
    autostart = _reload_autostart()
    from jellytoast.autostart import _macos

    plist = tmp_path / "io.github.wolfgangwarehaus.jellytoast.plist"
    plist.write_bytes(b"<plist/>")
    monkeypatch.setattr(_macos, "_PLIST", plist)

    assert autostart.disable() is True
    assert not plist.exists()


def test_macos_disable_returns_false_when_nothing_to_remove(monkeypatch, tmp_path):
    _force_macos(monkeypatch)
    autostart = _reload_autostart()
    from jellytoast.autostart import _macos

    monkeypatch.setattr(_macos, "_PLIST", tmp_path / "missing.plist")
    assert autostart.disable() is False


def test_macos_enable_returns_false_when_filesystem_hostile(monkeypatch):
    _force_macos(monkeypatch)
    autostart = _reload_autostart()
    from jellytoast.autostart import _macos

    class BadPath:
        def mkdir(self, *a, **k):
            raise PermissionError("read-only home")

    monkeypatch.setattr(_macos, "_AGENTS_DIR", BadPath())
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
        "jellytoast.autostart._macos",
        "jellytoast.autostart._unsupported",
    ):
        sys.modules.pop(mod_name, None)


class TestFlatpakAutostart:
    """0.2.0 Steam Deck QA finding #3: inside a flatpak the autostart entry
    must be the HOST-runnable `flatpak run <app-id>` form — the old copy/
    synth pointed at sandbox paths (/app/…, the sandbox python) that don't
    exist for the host session executing ~/.config/autostart, so the toggle
    silently launched nothing."""

    def test_synth_uses_flatpak_run_inside_sandbox(self, monkeypatch):
        from jellytoast.autostart import _linux

        monkeypatch.setenv("FLATPAK_ID", "io.github.wolfgangwarehaus.jellytoast")
        entry = _linux._synth_desktop_entry()
        assert "Exec=flatpak run io.github.wolfgangwarehaus.jellytoast\n" in entry
        assert "Path=" not in entry  # /app/... is unresolvable on the host
        assert "/app/" not in entry

    def test_synth_uses_interpreter_outside_sandbox(self, monkeypatch):
        from jellytoast.autostart import _linux

        monkeypatch.delenv("FLATPAK_ID", raising=False)
        entry = _linux._synth_desktop_entry()
        assert "-m jellytoast" in entry
        assert "flatpak run" not in entry

    def test_enable_prefers_synth_in_flatpak_even_with_source_desktop(
        self, monkeypatch, tmp_path
    ):
        # Even when a copied desktop entry EXISTS, the flatpak branch must
        # synthesize — the copy's Exec is sandbox-internal too.
        from jellytoast.autostart import _linux

        monkeypatch.setenv("FLATPAK_ID", "io.github.wolfgangwarehaus.jellytoast")
        src = tmp_path / "jellytoast.desktop"
        src.write_text("[Desktop Entry]\nExec=/app/bin/whatever\n")
        monkeypatch.setattr(_linux, "_SOURCE_DESKTOP", src)
        monkeypatch.setattr(_linux, "_AUTOSTART_DIR", tmp_path / "autostart")
        monkeypatch.setattr(
            _linux, "_AUTOSTART_FILE", tmp_path / "autostart" / "jellytoast.desktop"
        )
        assert _linux.enable() is True
        written = (tmp_path / "autostart" / "jellytoast.desktop").read_text()
        assert "Exec=flatpak run io.github.wolfgangwarehaus.jellytoast" in written
        assert "/app/" not in written


# ---------------------------------------------------------------------------
# XDG Background portal — org.freedesktop.portal.Background.RequestBackground
# with autostart=true. The sanctioned, permission-free route: Flathub's linter
# hard-rejects --filesystem=~/.config/autostart:create, so the .desktop writer
# alone left launch-at-login a silent no-op in the Flathub build.
#
# The whole D-Bus stack is faked (a stand-in `jeepney` in sys.modules) — no
# test here ever touches a real session bus or a real portal.
# ---------------------------------------------------------------------------

_GRANTED = (0, {"autostart": ("b", True)})
_DENIED = (1, {})  # portal Response code 1 == cancelled/denied
_SILENTLY_REFUSED = (0, {"autostart": ("b", False)})
_TURNED_OFF = (0, {"autostart": ("b", False)})


class _FakeMsg:
    """Stands in for both an outgoing jeepney message and an incoming reply."""

    def __init__(self, member, body=()):
        self.member = member
        self.body = body


class _FakeMessageBus:
    """jeepney.bus_messages.message_bus."""

    @staticmethod
    def NameHasOwner(name):
        return _FakeMsg("NameHasOwner", (name,))

    @staticmethod
    def AddMatch(rule):
        return _FakeMsg("AddMatch", (rule,))


class _FakeBus:
    """A scripted session bus. Records every method call the portal code
    makes and replays a canned Request/Response exchange."""

    def __init__(self, *, has_owner=True, response=_GRANTED, call_error=None,
                 never_answers=False):
        self.has_owner = has_owner
        self.response = response
        self.call_error = call_error
        self.never_answers = never_answers
        self.calls = []       # member names, in order
        self.options = None   # the a{sv} handed to RequestBackground
        self.closed = False

    def send_and_get_reply(self, msg):
        self.calls.append(msg.member)
        if msg.member == "NameHasOwner":
            return _FakeMsg("reply", (self.has_owner,))
        if msg.member == "RequestBackground":
            if self.call_error is not None:
                raise self.call_error
            self.options = msg.body[1]
            return _FakeMsg("reply", ("/org/freedesktop/portal/desktop/request/1",))
        return _FakeMsg("reply", ())

    def filter(self, rule):
        class _Queue:
            def __enter__(inner):
                return "queue"

            def __exit__(inner, *exc):
                return False

        return _Queue()

    def recv_until_filtered(self, queue, timeout=None):
        if self.never_answers:
            raise TimeoutError("portal never sent a Response")
        return _FakeMsg("Response", self.response)

    def close(self):
        self.closed = True


def _install_fake_jeepney(monkeypatch, fake_bus):
    """Put a stand-in `jeepney` in sys.modules so `_portal`'s lazy imports
    resolve to the fake bus instead of the real one."""
    import types

    jeepney = types.ModuleType("jeepney")
    jeepney.DBusAddress = lambda path, bus_name=None, interface=None: (
        path,
        bus_name,
        interface,
    )
    jeepney.MatchRule = lambda **kw: kw
    jeepney.new_method_call = lambda addr, member, sig, body: _FakeMsg(member, body)

    io_mod = types.ModuleType("jeepney.io")
    blocking = types.ModuleType("jeepney.io.blocking")
    blocking.open_dbus_connection = lambda **kw: fake_bus
    io_mod.blocking = blocking
    jeepney.io = io_mod

    bus_messages = types.ModuleType("jeepney.bus_messages")
    bus_messages.message_bus = _FakeMessageBus()
    jeepney.bus_messages = bus_messages

    for name, mod in (
        ("jeepney", jeepney),
        ("jeepney.io", io_mod),
        ("jeepney.io.blocking", blocking),
        ("jeepney.bus_messages", bus_messages),
    ):
        monkeypatch.setitem(sys.modules, name, mod)
    return fake_bus


def _portal_backend(monkeypatch, tmp_path, fake_bus=None):
    """Linux backend with tmp-scoped desktop-file paths and (optionally) a
    fake portal bus wired in. Returns (autostart, _linux, _portal, bus)."""
    _force_linux(monkeypatch)
    autostart = _reload_autostart()
    from jellytoast.autostart import _linux, _portal

    monkeypatch.setattr(_linux, "_AUTOSTART_DIR", tmp_path / "autostart")
    monkeypatch.setattr(
        _linux, "_AUTOSTART_FILE", tmp_path / "autostart" / "jellytoast.desktop"
    )
    monkeypatch.setattr(_linux, "_SOURCE_DESKTOP", tmp_path / "share" / "nope.desktop")
    if fake_bus is not None:
        _install_fake_jeepney(monkeypatch, fake_bus)
    return autostart, _linux, _portal, fake_bus


def _desktop_written(tmp_path) -> bool:
    return (tmp_path / "autostart" / "jellytoast.desktop").exists()


class TestBackgroundPortal:
    def test_enable_goes_through_the_portal_when_available(self, monkeypatch, tmp_path):
        autostart, _, _, bus = _portal_backend(monkeypatch, tmp_path, _FakeBus())

        assert autostart.enable() is True
        assert "RequestBackground" in bus.calls
        # The portal writes the host-side entry — we must NOT also drop a
        # .desktop file (that's the grant Flathub rejects).
        assert not _desktop_written(tmp_path)
        assert autostart.is_enabled() is True
        assert bus.closed is True

    def test_request_options_match_the_portal_contract(self, monkeypatch, tmp_path):
        _, _, _, bus = _portal_backend(monkeypatch, tmp_path, _FakeBus())
        from jellytoast.autostart import _linux

        _linux.enable()
        opts = bus.options
        assert opts["autostart"] == ("b", True)
        assert opts["dbus-activatable"] == ("b", False)
        sig, cmdline = opts["commandline"]
        assert sig == "as"
        assert isinstance(cmdline, list) and cmdline
        reason_sig, reason = opts["reason"]
        assert reason_sig == "s"
        assert isinstance(reason, str) and reason  # localized, non-empty

    def test_inside_flatpak_skips_the_name_probe(self, monkeypatch, tmp_path):
        monkeypatch.setenv("FLATPAK_ID", "io.github.wolfgangwarehaus.jellytoast")
        # has_owner=False would fail the probe — inside a sandbox a portal is
        # always present, so we shouldn't be asking.
        _, _, _, bus = _portal_backend(
            monkeypatch, tmp_path, _FakeBus(has_owner=False)
        )
        from jellytoast.autostart import _linux

        assert _linux.enable() is True
        assert "NameHasOwner" not in bus.calls
        assert bus.options["commandline"] == ("as", ["jellytoast"])

    def test_denial_is_reported_honestly(self, monkeypatch, tmp_path):
        """The user said no. enable() returns False, is_enabled() agrees, and
        we do NOT sneak a .desktop file in behind their back."""
        autostart, _, _, _ = _portal_backend(
            monkeypatch, tmp_path, _FakeBus(response=_DENIED)
        )

        assert autostart.enable() is False
        assert autostart.is_enabled() is False
        assert not _desktop_written(tmp_path)

    def test_success_code_with_autostart_false_is_a_denial(self, monkeypatch, tmp_path):
        """Some portals answer code 0 but hand back autostart=false. That's a
        refusal, not a grant."""
        autostart, _, _, _ = _portal_backend(
            monkeypatch, tmp_path, _FakeBus(response=_SILENTLY_REFUSED)
        )

        assert autostart.enable() is False
        assert autostart.is_enabled() is False

    def test_falls_back_to_desktop_file_when_no_portal_on_the_bus(
        self, monkeypatch, tmp_path
    ):
        autostart, _, _, bus = _portal_backend(
            monkeypatch, tmp_path, _FakeBus(has_owner=False)
        )

        assert autostart.enable() is True
        assert "RequestBackground" not in bus.calls
        assert _desktop_written(tmp_path)
        assert autostart.is_enabled() is True

    def test_falls_back_to_desktop_file_when_the_portal_errors(
        self, monkeypatch, tmp_path
    ):
        autostart, _, _, bus = _portal_backend(
            monkeypatch, tmp_path, _FakeBus(call_error=RuntimeError("bus exploded"))
        )

        assert autostart.enable() is True  # no exception escaped
        assert "RequestBackground" in bus.calls
        assert _desktop_written(tmp_path)

    def test_falls_back_when_the_portal_never_answers(self, monkeypatch, tmp_path):
        autostart, _, _, _ = _portal_backend(
            monkeypatch, tmp_path, _FakeBus(never_answers=True)
        )

        assert autostart.enable() is True
        assert _desktop_written(tmp_path)

    def test_falls_back_when_jeepney_is_missing(self, monkeypatch, tmp_path):
        """No fake bus installed: the autouse fixture leaves `jeepney`
        unimportable, which is the no-jeepney install."""
        autostart, _, _, _ = _portal_backend(monkeypatch, tmp_path)

        assert autostart.enable() is True
        assert _desktop_written(tmp_path)

    def test_disable_asks_the_portal_and_forgets_the_grant(self, monkeypatch, tmp_path):
        autostart, _, _portal, bus = _portal_backend(monkeypatch, tmp_path, _FakeBus())
        assert autostart.enable() is True

        bus.response = _TURNED_OFF
        assert autostart.disable() is True
        assert bus.calls.count("RequestBackground") == 2
        assert bus.options["autostart"] == ("b", False)
        assert autostart.is_enabled() is False
        assert _portal.autostart_granted() is False

    def test_disable_keeps_the_grant_when_the_portal_refuses(
        self, monkeypatch, tmp_path
    ):
        """If the portal won't turn it off, don't pretend we did — the
        checkbox must keep showing the real (still-on) state."""
        autostart, _, _, bus = _portal_backend(monkeypatch, tmp_path, _FakeBus())
        assert autostart.enable() is True

        bus.response = _DENIED
        assert autostart.disable() is False
        assert autostart.is_enabled() is True

    def test_disable_drops_a_stale_grant_when_the_portal_vanishes(
        self, monkeypatch, tmp_path
    ):
        """Grant recorded, portal gone: the record is unverifiable, so drop
        it rather than pin the checkbox on forever."""
        autostart, _, _portal, _ = _portal_backend(monkeypatch, tmp_path, _FakeBus())
        assert autostart.enable() is True

        # Rip jeepney back out — the portal is now unreachable.
        monkeypatch.setitem(sys.modules, "jeepney", None)
        assert autostart.disable() is True
        assert autostart.is_enabled() is False

    def test_disable_still_removes_a_legacy_desktop_file(self, monkeypatch, tmp_path):
        """Upgraders may carry a .desktop entry from before the portal
        existed; disable() must clear that too."""
        autostart, _linux, _, _ = _portal_backend(monkeypatch, tmp_path, _FakeBus())
        _linux._AUTOSTART_DIR.mkdir(parents=True, exist_ok=True)
        _linux._AUTOSTART_FILE.write_text("[Desktop Entry]\nName=jellytoast\n")

        assert autostart.disable() is True
        assert not _desktop_written(tmp_path)

    def test_is_enabled_prefers_the_portal_grant_over_disk(self, monkeypatch, tmp_path):
        """Inside a sandbox there is no autostart file to look at — the
        recorded grant is the only truth."""
        autostart, _, _portal, _ = _portal_backend(monkeypatch, tmp_path)

        assert autostart.is_enabled() is False
        _portal.mark_granted()
        assert autostart.is_enabled() is True
        _portal.clear_granted()
        assert autostart.is_enabled() is False

    def test_nothing_raises_when_everything_is_broken(self, monkeypatch, tmp_path):
        """Hostile filesystem AND an exploding bus: the public API must still
        answer with plain bools — the settings checkbox depends on it."""
        autostart, _linux, _portal, _ = _portal_backend(
            monkeypatch, tmp_path, _FakeBus(call_error=RuntimeError("bus exploded"))
        )

        class BadPath:
            def mkdir(self, *a, **k):
                raise PermissionError("read-only home")

        monkeypatch.setattr(_linux, "_AUTOSTART_DIR", BadPath())
        monkeypatch.setattr(_portal, "_STATE_FILE", _BrokenPath())

        assert autostart.enable() is False
        assert autostart.is_enabled() is False
        assert autostart.disable() is False
        assert autostart.is_supported() is True

    def test_state_marker_write_failure_is_swallowed(self, monkeypatch, tmp_path):
        _, _, _portal, _ = _portal_backend(monkeypatch, tmp_path)
        monkeypatch.setattr(_portal, "_STATE_FILE", _BrokenPath())

        _portal.mark_granted()  # must not raise
        _portal.clear_granted()  # must not raise
        assert _portal.autostart_granted() is False


class _BrokenPath:
    """Every filesystem touch fails — models a read-only / missing config dir."""

    @property
    def parent(self):
        return self

    def mkdir(self, *a, **k):
        raise PermissionError("read-only config dir")

    def write_text(self, *a, **k):
        raise PermissionError("read-only config dir")

    def unlink(self, *a, **k):
        raise PermissionError("read-only config dir")

    def exists(self):
        raise OSError("unreadable config dir")


class TestPortalResultParsing:
    def test_variant_unwrapping(self):
        from jellytoast.autostart import _portal

        assert _portal._variant_value(("b", True)) is True
        assert _portal._variant_value(True) is True
        assert _portal._variant_value(("as", ["x"])) == ["x"]

    def test_granted_matches_requested_state(self):
        from jellytoast.autostart import _portal

        assert _portal._autostart_granted({"autostart": ("b", True)}, True) is True
        assert _portal._autostart_granted({"autostart": ("b", False)}, True) is False
        assert _portal._autostart_granted({"autostart": ("b", False)}, False) is True
        # No echo of the field: trust the success code.
        assert _portal._autostart_granted({}, True) is True

    def test_commandline_outside_a_sandbox_is_runnable(self, monkeypatch):
        from jellytoast.autostart import _portal

        monkeypatch.delenv("FLATPAK_ID", raising=False)
        monkeypatch.setattr("shutil.which", lambda name: None)
        assert _portal._commandline()[-2:] == ["-m", "jellytoast"]
