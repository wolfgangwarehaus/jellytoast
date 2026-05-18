"""Cross-platform keep-above (always-on-top) backend smoke tests.

The keep_above package picks its backend at import time via a runtime
call to `modules.platform_compat.is_kde_wayland()`. On KDE Wayland the
`_kwin` backend shells out to kwriteconfig6/kreadconfig6/qdbus6 to
manage a KWin window rule. Everywhere else the unsupported backend is a
silent no-op.

All shell-outs are mocked so the suite runs on any host.
"""

from __future__ import annotations

import importlib
import subprocess
import sys

import pytest


def _reload_keep_above():
    """Drop and re-import the package so its import-time `is_kde_wayland()`
    gate re-evaluates against current mocks."""
    for mod_name in (
        "modules.keep_above",
        "modules.keep_above._kwin",
        "modules.keep_above._unsupported",
    ):
        sys.modules.pop(mod_name, None)
    return importlib.import_module("modules.keep_above")


def _force_kde_wayland(monkeypatch, value: bool):
    import modules.platform_compat as pc

    monkeypatch.setattr(pc, "is_kde_wayland", lambda: value)


def test_imports_cleanly_on_kde_wayland(monkeypatch):
    _force_kde_wayland(monkeypatch, True)
    keep_above = _reload_keep_above()
    assert hasattr(keep_above, "install_mini_player_rule")
    assert hasattr(keep_above, "remove_mini_player_rule")
    assert hasattr(keep_above, "is_supported")
    assert hasattr(keep_above, "diagnose")
    assert keep_above.MINI_PLAYER_WINDOW_TITLE == "jellytoast Mini Player"


def test_imports_cleanly_off_kde_wayland(monkeypatch):
    _force_kde_wayland(monkeypatch, False)
    keep_above = _reload_keep_above()
    assert hasattr(keep_above, "install_mini_player_rule")
    assert hasattr(keep_above, "remove_mini_player_rule")
    assert hasattr(keep_above, "is_supported")
    assert hasattr(keep_above, "diagnose")


def test_kwin_backend_selected_on_kde_wayland(monkeypatch):
    _force_kde_wayland(monkeypatch, True)
    keep_above = _reload_keep_above()
    assert keep_above._backend.__name__ == "modules.keep_above._kwin"


def test_unsupported_backend_selected_off_kde_wayland(monkeypatch):
    _force_kde_wayland(monkeypatch, False)
    keep_above = _reload_keep_above()
    assert keep_above._backend.__name__ == "modules.keep_above._unsupported"


def test_unsupported_methods_are_silent_noops(monkeypatch):
    _force_kde_wayland(monkeypatch, False)
    keep_above = _reload_keep_above()
    assert keep_above.is_supported() is False
    assert keep_above.install_mini_player_rule() is False
    assert keep_above.remove_mini_player_rule() is False
    d = keep_above.diagnose()
    assert isinstance(d, dict)
    assert d["backend"] == "unsupported"
    assert d["is_supported"] is False


def test_is_supported_false_when_kde_tools_missing(monkeypatch):
    _force_kde_wayland(monkeypatch, True)
    keep_above = _reload_keep_above()

    from modules.keep_above import _kwin

    monkeypatch.setattr(_kwin.shutil, "which", lambda _: None)
    assert keep_above.is_supported() is False


def test_is_supported_true_when_kde_tools_present(monkeypatch):
    _force_kde_wayland(monkeypatch, True)
    keep_above = _reload_keep_above()

    from modules.keep_above import _kwin

    monkeypatch.setattr(
        _kwin.shutil,
        "which",
        lambda cmd: f"/usr/bin/{cmd}",
    )
    assert keep_above.is_supported() is True


def test_install_returns_false_when_tools_missing(monkeypatch):
    _force_kde_wayland(monkeypatch, True)
    keep_above = _reload_keep_above()

    from modules.keep_above import _kwin

    monkeypatch.setattr(_kwin.shutil, "which", lambda _: None)
    assert keep_above.install_mini_player_rule() is False


def test_install_shells_out_when_tools_present(monkeypatch):
    """Install must call kwriteconfig6 (or 5) for each rule field and
    fire a kwin reconfigure at the end. We don't care about exact
    invocation count; we just want subprocess.run to be triggered with
    the right binaries."""
    _force_kde_wayland(monkeypatch, True)
    keep_above = _reload_keep_above()

    from modules.keep_above import _kwin

    monkeypatch.setattr(
        _kwin.shutil,
        "which",
        lambda cmd: f"/usr/bin/{cmd}",
    )

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    monkeypatch.setattr(_kwin.subprocess, "run", fake_run)

    result = keep_above.install_mini_player_rule()
    assert result is True
    # We expect a healthy mix: kreadconfig probes + kwriteconfig writes
    # for each rule field + a qdbus reconfigure.
    assert any("kwriteconfig6" in c[0] or "kwriteconfig5" in c[0] for c in calls)
    assert any("qdbus" in c[0] for c in calls)


def test_remove_returns_false_when_no_stored_uuid(monkeypatch, tmp_path):
    """remove_mini_player_rule must short-circuit cleanly when there's
    nothing stored in QSettings (no UUID = nothing was ever installed)."""
    _force_kde_wayland(monkeypatch, True)
    keep_above = _reload_keep_above()

    from modules.keep_above import _kwin

    monkeypatch.setattr(
        _kwin.shutil,
        "which",
        lambda cmd: f"/usr/bin/{cmd}",
    )

    # Patch QSettings.value to return empty string (no stored UUID).
    class _FakeQSettings:
        def __init__(self, *args, **kwargs):
            pass

        def value(self, key, default="", type=str):
            return ""

        def setValue(self, key, val):
            pass

        def remove(self, key):
            pass

    monkeypatch.setattr(_kwin, "QSettings", _FakeQSettings)
    assert keep_above.remove_mini_player_rule() is False


def test_install_survives_subprocess_failure(monkeypatch):
    """A FileNotFoundError or generic Exception from subprocess.run
    inside _kwriteconfig must be swallowed — the helper has its own
    try/except. The public install API still reports True because
    is_supported gated past."""
    _force_kde_wayland(monkeypatch, True)
    keep_above = _reload_keep_above()

    from modules.keep_above import _kwin

    monkeypatch.setattr(
        _kwin.shutil,
        "which",
        lambda cmd: f"/usr/bin/{cmd}",
    )

    def raising_run(cmd, **kwargs):
        raise FileNotFoundError("tool vanished mid-call")

    monkeypatch.setattr(_kwin.subprocess, "run", raising_run)

    # Must not raise — internal try/except eats the failure.
    keep_above.install_mini_player_rule()


def test_diagnose_on_kwin_backend_reports_tools(monkeypatch):
    _force_kde_wayland(monkeypatch, True)
    keep_above = _reload_keep_above()

    from modules.keep_above import _kwin

    monkeypatch.setattr(
        _kwin.shutil,
        "which",
        lambda cmd: f"/usr/bin/{cmd}",
    )

    d = keep_above.diagnose()
    assert d["backend"] == "kwin"
    assert d["is_supported"] is True
    assert d["rule_app_id"] == "jellytoast"
    assert d["rule_title"] == "jellytoast Mini Player"


@pytest.fixture(autouse=True)
def _restore_keep_above_module():
    """Leave a fresh import after each test so the next test gets a
    clean module + backend reference."""
    yield
    for mod_name in (
        "modules.keep_above",
        "modules.keep_above._kwin",
        "modules.keep_above._unsupported",
    ):
        sys.modules.pop(mod_name, None)
