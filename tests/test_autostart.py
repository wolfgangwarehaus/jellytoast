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
        "jellytoast.autostart._flatpak",
        "jellytoast.autostart._unsupported",
    ):
        sys.modules.pop(mod_name, None)
    return importlib.import_module("jellytoast.autostart")


def _force_linux(monkeypatch):
    import jellytoast.platform_compat as pc

    monkeypatch.setattr(pc, "IS_LINUX", True)
    monkeypatch.setattr(pc, "IS_FLATPAK", False)


def _force_non_linux(monkeypatch):
    import jellytoast.platform_compat as pc

    monkeypatch.setattr(pc, "IS_LINUX", False)
    monkeypatch.setattr(pc, "IS_FLATPAK", False)


def _force_flatpak(monkeypatch):
    import jellytoast.platform_compat as pc

    monkeypatch.setattr(pc, "IS_LINUX", True)
    monkeypatch.setattr(pc, "IS_FLATPAK", True)


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


@pytest.fixture(autouse=True)
def _restore_autostart_module():
    """Leave a fresh import after each test so the next test gets a
    clean module + backend reference."""
    yield
    for mod_name in (
        "jellytoast.autostart",
        "jellytoast.autostart._linux",
        "jellytoast.autostart._unsupported",
    ):
        sys.modules.pop(mod_name, None)


# ── Flatpak backend (Background portal) ───────────────────────────────


def test_flatpak_backend_selected_inside_sandbox(monkeypatch):
    _force_flatpak(monkeypatch)
    autostart = _reload_autostart()
    assert autostart._backend.__name__ == "jellytoast.autostart._flatpak"


def test_flatpak_build_options_shape():
    """RequestBackground a{sv} options carry jeepney (signature, value)
    variants and pass the requested autostart state through."""
    from jellytoast.autostart import _flatpak

    on = _flatpak.build_options(True)
    off = _flatpak.build_options(False)
    assert on["autostart"] == ("b", True)
    assert off["autostart"] == ("b", False)
    assert on["commandline"] == ("as", ["jellytoast"])
    assert on["reason"][0] == "s"
    assert on["dbus-activatable"] == ("b", False)


def test_flatpak_is_enabled_reads_persisted_intent(isolated_settings):
    """The portal has no read-back API — is_enabled() reports the
    persisted settings flag."""
    from jellytoast.autostart import _flatpak

    isolated_settings.autostart = True
    assert _flatpak.is_enabled() is True
    isolated_settings.autostart = False
    assert _flatpak.is_enabled() is False


def test_flatpak_denied_response_flips_persisted_intent(isolated_settings):
    from jellytoast.autostart import _flatpak

    isolated_settings.autostart = True
    _flatpak._on_response(requested=True, granted=False)
    assert isolated_settings.autostart is False


def test_flatpak_timeout_response_keeps_intent(isolated_settings):
    from jellytoast.autostart import _flatpak

    isolated_settings.autostart = True
    _flatpak._on_response(requested=True, granted=None)
    assert isolated_settings.autostart is True


def test_flatpak_enable_dispatches_request(monkeypatch):
    """enable()/disable() are dispatch-and-return: the worker carries the
    portal round-trip, the call site gets True for 'request sent'."""
    from jellytoast.autostart import _flatpak

    calls = {}

    def _fake_run_async(fn, on_result=None, on_error=None):
        calls["result"] = fn()
        if on_result:
            on_result(calls["result"])

    import jellytoast.async_io as aio

    monkeypatch.setattr(aio, "run_async", _fake_run_async)
    monkeypatch.setattr(_flatpak, "_request_background", lambda autostart: True)
    monkeypatch.setattr(_flatpak, "is_supported", lambda: True)
    assert _flatpak.enable() is True
    assert calls["result"] is True


def test_kwin_backends_self_disable_inside_flatpak(monkeypatch):
    """drag_repaint + keep_above must hard-no-op in the sandbox: their
    kwinrc/kwinrulesrc writes would land in the private app config and
    org.kde.KWin isn't on the filtered bus."""
    import jellytoast.platform_compat as pc

    monkeypatch.setattr(pc, "IS_FLATPAK", True)
    from jellytoast.drag_repaint import _kwin as drag_kwin
    from jellytoast.keep_above import _kwin as above_kwin

    assert drag_kwin.is_supported() is False
    assert above_kwin.is_supported() is False
