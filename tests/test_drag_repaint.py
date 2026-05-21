"""Tests for modules.drag_repaint — the KWin scripted-effect that kills
the stale-blur drag artifact.

Covers the bundled effect asset (well-formedness), the `_kwin` backend
(install / uninstall / is_supported, with subprocess + data-dir mocked),
the `_unsupported` no-op backend, and the facade's `sync()` env gate.

The `_kwin` backend is imported directly — `modules.drag_repaint`'s
`__init__` picks a backend at import time via `is_kde_wayland()`, which
is False under the test environment, so the package facade resolves to
`_unsupported`. Importing `_kwin` directly exercises the real logic.
"""

from __future__ import annotations

import json

import pytest

from modules.drag_repaint import _kwin, _unsupported


# ── The bundled effect asset ──────────────────────────────────────────


class TestBundledEffect:
    def test_source_dir_exists(self):
        assert _kwin._SOURCE_DIR.is_dir()

    def test_metadata_is_valid_json(self):
        meta = json.loads((_kwin._SOURCE_DIR / "metadata.json").read_text())
        assert isinstance(meta, dict)

    def test_metadata_id_matches_effect_id(self):
        meta = json.loads((_kwin._SOURCE_DIR / "metadata.json").read_text())
        # The KPlugin Id is also the kwinrc key stem — they must agree.
        assert meta["KPlugin"]["Id"] == _kwin._EFFECT_ID

    def test_metadata_declares_javascript_api(self):
        meta = json.loads((_kwin._SOURCE_DIR / "metadata.json").read_text())
        assert meta["X-Plasma-API"] == "javascript"
        assert meta["KPackageStructure"] == "KWin/Effect"

    def test_main_js_present_and_nonempty(self):
        js = _kwin._SOURCE_DIR / "contents" / "code" / "main.js"
        assert js.is_file()
        assert js.stat().st_size > 0

    def test_main_js_hooks_move_signals(self):
        js = (_kwin._SOURCE_DIR / "contents" / "code" / "main.js").read_text()
        # The effect must wire both move signals or it does nothing.
        assert "windowStartUserMovedResized" in js
        assert "windowFinishUserMovedResized" in js

    def test_main_js_force_blurs_during_transform(self):
        js = (_kwin._SOURCE_DIR / "contents" / "code" / "main.js").read_text()
        # Without WindowForceBlurRole the blur drops while transformed.
        assert "WindowForceBlurRole" in js


# ── _kwin backend — fixtures ──────────────────────────────────────────


@pytest.fixture
def fake_kwin(monkeypatch, tmp_path):
    """Wire the `_kwin` backend for tests: tools resolve to fake paths,
    the data home points at a tmp dir, and subprocess is captured.
    Yields the list of subprocess argv lists."""
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        return None

    monkeypatch.setattr(_kwin, "_kwriteconfig_bin", lambda: "/usr/bin/kwriteconfig6")
    monkeypatch.setattr(_kwin, "_qdbus_bin", lambda: "/usr/bin/qdbus6")
    monkeypatch.setattr(_kwin, "_data_home", lambda: tmp_path)
    monkeypatch.setattr(_kwin.subprocess, "run", fake_run)
    return calls


# ── _kwin backend — is_supported ──────────────────────────────────────


class TestKWinIsSupported:
    def test_true_when_tools_and_source_present(self, fake_kwin):
        assert _kwin.is_supported() is True

    def test_false_without_kwriteconfig(self, fake_kwin, monkeypatch):
        monkeypatch.setattr(_kwin, "_kwriteconfig_bin", lambda: None)
        assert _kwin.is_supported() is False

    def test_false_without_qdbus(self, fake_kwin, monkeypatch):
        monkeypatch.setattr(_kwin, "_qdbus_bin", lambda: None)
        assert _kwin.is_supported() is False


# ── _kwin backend — install ───────────────────────────────────────────


class TestKWinInstall:
    def test_install_copies_effect_files(self, fake_kwin):
        assert _kwin.install() is True
        dest = _kwin._dest_dir()
        assert (dest / "metadata.json").is_file()
        assert (dest / "contents" / "code" / "main.js").is_file()

    def test_install_writes_enable_key(self, fake_kwin):
        _kwin.install()
        flat = [" ".join(c) for c in fake_kwin]
        assert any(
            "kwinrc" in c
            and "jellytoast_dragrepaintEnabled" in c
            and c.endswith(" true")
            for c in flat
        ), flat

    def test_install_loads_the_effect(self, fake_kwin):
        _kwin.install()
        flat = [" ".join(c) for c in fake_kwin]
        assert any("loadEffect jellytoast_dragrepaint" in c for c in flat), flat

    def test_install_overwrites_a_stale_copy(self, fake_kwin):
        dest = _kwin._dest_dir()
        dest.mkdir(parents=True)
        stale = dest / "stale.txt"
        stale.write_text("old")
        assert _kwin.install() is True
        # The stale file is gone, the real asset is in place.
        assert not stale.exists()
        assert (dest / "metadata.json").is_file()

    def test_install_is_idempotent(self, fake_kwin):
        assert _kwin.install() is True
        assert _kwin.install() is True
        assert (_kwin._dest_dir() / "metadata.json").is_file()

    def test_install_returns_false_when_unsupported(self, fake_kwin, monkeypatch):
        monkeypatch.setattr(_kwin, "_qdbus_bin", lambda: None)
        assert _kwin.install() is False
        assert not _kwin._dest_dir().exists()


# ── _kwin backend — uninstall ─────────────────────────────────────────


class TestKWinUninstall:
    def test_uninstall_removes_the_effect_dir(self, fake_kwin):
        _kwin.install()
        assert _kwin._dest_dir().exists()
        assert _kwin.uninstall() is True
        assert not _kwin._dest_dir().exists()

    def test_uninstall_writes_disable_key(self, fake_kwin):
        _kwin.install()
        fake_kwin.clear()
        _kwin.uninstall()
        flat = [" ".join(c) for c in fake_kwin]
        assert any(
            "jellytoast_dragrepaintEnabled" in c and c.endswith(" false")
            for c in flat
        ), flat

    def test_uninstall_unloads_the_effect(self, fake_kwin):
        _kwin.install()
        fake_kwin.clear()
        _kwin.uninstall()
        flat = [" ".join(c) for c in fake_kwin]
        assert any("unloadEffect jellytoast_dragrepaint" in c for c in flat), flat

    def test_uninstall_when_nothing_installed_is_safe(self, fake_kwin):
        # No prior install — uninstall must not raise and still returns True.
        assert _kwin.uninstall() is True

    def test_uninstall_returns_false_when_unsupported(self, fake_kwin, monkeypatch):
        monkeypatch.setattr(_kwin, "_kwriteconfig_bin", lambda: None)
        assert _kwin.uninstall() is False


# ── _kwin backend — path resolution ───────────────────────────────────


class TestKWinPaths:
    def test_data_home_honours_xdg(self, monkeypatch):
        monkeypatch.setenv("XDG_DATA_HOME", "/custom/data")
        assert str(_kwin._data_home()) == "/custom/data"

    def test_data_home_default(self, monkeypatch):
        monkeypatch.delenv("XDG_DATA_HOME", raising=False)
        assert _kwin._data_home().name == "share"

    def test_dest_dir_is_under_kwin_effects(self, monkeypatch, tmp_path):
        monkeypatch.setattr(_kwin, "_data_home", lambda: tmp_path)
        dest = _kwin._dest_dir()
        assert dest == tmp_path / "kwin" / "effects" / "jellytoast_dragrepaint"


# ── _unsupported backend ──────────────────────────────────────────────


class TestUnsupportedBackend:
    def test_is_supported_false(self):
        assert _unsupported.is_supported() is False

    def test_install_false(self):
        assert _unsupported.install() is False

    def test_uninstall_false(self):
        assert _unsupported.uninstall() is False

    def test_diagnose_reports_unsupported(self):
        d = _unsupported.diagnose()
        assert d["backend"] == "unsupported"
        assert d["is_supported"] is False


# ── Facade — sync() env gate ──────────────────────────────────────────


class TestFacadeSync:
    def test_sync_installs_by_default(self, monkeypatch):
        import modules.drag_repaint as dr

        monkeypatch.delenv("JT_NO_DRAG_REPAINT", raising=False)
        called = []
        monkeypatch.setattr(dr, "install", lambda: called.append("install") or True)
        monkeypatch.setattr(dr, "uninstall", lambda: called.append("uninstall") or True)
        dr.sync()
        assert called == ["install"]

    def test_sync_uninstalls_under_env_flag(self, monkeypatch):
        import modules.drag_repaint as dr

        monkeypatch.setenv("JT_NO_DRAG_REPAINT", "1")
        called = []
        monkeypatch.setattr(dr, "install", lambda: called.append("install") or True)
        monkeypatch.setattr(dr, "uninstall", lambda: called.append("uninstall") or True)
        dr.sync()
        assert called == ["uninstall"]

    def test_sync_env_flag_must_be_exactly_one(self, monkeypatch):
        import modules.drag_repaint as dr

        monkeypatch.setenv("JT_NO_DRAG_REPAINT", "0")
        called = []
        monkeypatch.setattr(dr, "install", lambda: called.append("install") or True)
        monkeypatch.setattr(dr, "uninstall", lambda: called.append("uninstall") or True)
        dr.sync()
        assert called == ["install"]
