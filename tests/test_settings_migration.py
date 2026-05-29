"""Tests for ``modules.settings._migrate_legacy_org_name``.

The helper is a one-shot brand migration ("JellyToast" → "jellytoast")
that touches three different surfaces:

  1. QSettings keys under the old org/app are copied forward.
  2. ``~/.local/share/JellyToast/`` and ``~/.cache/JellyToast/`` are
     filesystem-moved to their lowercase equivalents. Files in
     ``~/.config/JellyToast/`` (except the legacy ``.conf`` itself)
     are moved into ``~/.config/jellytoast/``.
  3. The keyring entry under service "JellyToast" is copied to
     service "jellytoast".

Plus an idempotency marker so it only runs once per install.

How we fake the world:

* HOME is monkeypatched to ``tmp_path`` so ``Path.home()`` lands in
  the sandbox.
* QSettings is redirected with ``QSettings.setPath(...)`` to
  ``tmp_path/.config`` per-test. This matters because Qt caches the
  resolved native-scope path globally on first ``QSettings()``
  construction in a process — bare HOME mutation isn't enough,
  every test would otherwise see the first test's tmp dir.
* Pointing QSettings's user scope at ``tmp/.config`` makes the
  ``.conf`` file live at exactly the path the migration helper
  checks (``~/.config/<org>/<app>.conf``) — production-shaped, just
  rooted in a sandbox.
* ``keyring`` is replaced in ``sys.modules`` with a tiny in-memory
  stub. The helper imports keyring lazily (inside the function body),
  so injecting it before the call is enough.
* ``shutil.move`` is left real — it's just operating inside ``tmp_path``.

The migration also short-circuits on non-Linux (it sets the marker and
returns). All tests below patch ``sys.platform`` to ``"linux"`` so the
behaviour is exercised regardless of CI host.
"""

import sys
import types

import pytest

# ── fake keyring backend ─────────────────────────────────────────────


class _FakeKeyring:
    """Minimal stand-in for python-keyring's module-level functions.

    Records every call so tests can assert what the helper did (or
    didn't) touch. Behaves like a real keyring for the small surface
    the migration uses: ``get_password`` / ``set_password`` /
    ``delete_password``."""

    def __init__(self):
        self.store: dict[tuple[str, str], str] = {}
        self.calls: list[tuple] = []

    def get_password(self, service, username):
        self.calls.append(("get", service, username))
        return self.store.get((service, username))

    def set_password(self, service, username, value):
        self.calls.append(("set", service, username, value))
        self.store[(service, username)] = value

    def delete_password(self, service, username):
        self.calls.append(("delete", service, username))
        self.store.pop((service, username), None)


@pytest.fixture
def fake_keyring(monkeypatch):
    fake = _FakeKeyring()
    # The helper does ``import keyring`` inside the function body, so
    # we shim the module entry rather than patching a binding.
    mod = types.ModuleType("keyring")
    mod.get_password = fake.get_password  # type: ignore[attr-defined]
    mod.set_password = fake.set_password  # type: ignore[attr-defined]
    mod.delete_password = fake.delete_password  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "keyring", mod)
    return fake


@pytest.fixture
def sandbox_home(tmp_path, monkeypatch):
    """Redirect HOME and QSettings's user-scope path to ``tmp_path``.

    Both ``Path.home()`` (via HOME) and ``QSettings()`` (via
    ``setPath``) land under ``tmp_path``. The QSettings ``.conf``
    file ends up at the canonical ``~/.config/<org>/<app>.conf``
    layout, just rooted in the sandbox — the same shape the
    migration helper expects.

    Also forces ``sys.platform`` to ``"linux"`` inside ``modules.settings``
    so the helper takes the migration path on every CI runner (the
    helper short-circuits on macOS/Windows).
    """
    from PySide6.QtCore import QSettings

    monkeypatch.setenv("HOME", str(tmp_path))

    # Redirect QSettings's UserScope path. Both Native and Ini formats
    # are pointed at the sandbox so any QSettings constructor lands
    # under it regardless of which the code chose.
    cfg_root = tmp_path / ".config"
    cfg_root.mkdir(exist_ok=True)
    QSettings.setPath(
        QSettings.Format.NativeFormat,
        QSettings.Scope.UserScope,
        str(cfg_root),
    )
    QSettings.setPath(
        QSettings.Format.IniFormat,
        QSettings.Scope.UserScope,
        str(cfg_root),
    )

    import modules.settings as smod

    monkeypatch.setattr(smod.sys, "platform", "linux")

    return tmp_path


def _legacy_qs(sandbox_home):
    """Build a QSettings handle pointing at the LEGACY (CamelCase) org/app
    and verify it actually writes under the sandboxed HOME."""
    from PySide6.QtCore import QSettings

    qs = QSettings("JellyToast", "JellyToast")
    assert str(sandbox_home) in qs.fileName(), (
        f"legacy QSettings landed outside sandbox: {qs.fileName()}"
    )
    return qs


def _new_qs(sandbox_home):
    from PySide6.QtCore import QSettings

    qs = QSettings("jellytoast", "jellytoast")
    assert str(sandbox_home) in qs.fileName(), (
        f"new QSettings landed outside sandbox: {qs.fileName()}"
    )
    return qs


# ── test cases ────────────────────────────────────────────────────────


def test_no_legacy_data_is_noop_but_marks(sandbox_home, fake_keyring):
    """With nothing under the legacy name — no QSettings file, no
    config/data/cache dirs — the helper should still set the marker
    (so we don't re-check on every launch) but make zero keyring or
    filesystem mutations."""
    from modules.settings import (
        _MIGRATION_MARKER,
        _migrate_legacy_org_name,
    )

    _migrate_legacy_org_name()

    new = _new_qs(sandbox_home)
    assert new.value(_MIGRATION_MARKER, False, type=bool) is True
    # No legacy data → no keyring touch at all.
    assert fake_keyring.calls == []
    # And no new directories were conjured.
    assert not (sandbox_home / ".local" / "share" / "jellytoast").exists()
    assert not (sandbox_home / ".cache" / "jellytoast").exists()


def test_full_migration_first_call(sandbox_home, fake_keyring):
    """Legacy QSettings keys + ``~/.config/JellyToast/`` + data dir +
    cache dir + keyring entry are all present. After the call:

    * new QSettings has every legacy key
    * the data/cache dirs are MOVED (not copied) to the lowercase paths
    * extra config files (``queue.json``, ``scrobble_queue.json``) are
      moved; the legacy ``JellyToast.conf`` itself is left in place as
      a rollback safety net
    * the keyring entry is copied under the new service name
    * the marker key is set
    """
    # Plant legacy QSettings keys.
    old = _legacy_qs(sandbox_home)
    old.setValue("server/url", "http://old.example")
    old.setValue("volume/level", 73)
    old.sync()

    # Plant legacy filesystem layout. The QSettings sync above
    # already created ``~/.config/JellyToast/JellyToast.conf`` (we
    # redirected QSettings's UserScope into ``tmp/.config``), so this
    # dir exists.
    (sandbox_home / ".config" / "JellyToast").mkdir(parents=True, exist_ok=True)
    legacy_conf = sandbox_home / ".config" / "JellyToast" / "JellyToast.conf"
    assert legacy_conf.exists(), "QSettings sync should have created the .conf"
    (sandbox_home / ".config" / "JellyToast" / "queue.json").write_text("{}")
    (sandbox_home / ".local" / "share" / "JellyToast").mkdir(parents=True)
    (sandbox_home / ".local" / "share" / "JellyToast" / "downloads.db").write_text("blob")
    (sandbox_home / ".cache" / "JellyToast").mkdir(parents=True)
    (sandbox_home / ".cache" / "JellyToast" / "covers").mkdir()

    # Plant legacy keyring entry.
    fake_keyring.store[("JellyToast", "access_token")] = "secret-token"

    from modules.settings import (
        _MIGRATION_MARKER,
        _migrate_legacy_org_name,
    )

    _migrate_legacy_org_name()

    new = _new_qs(sandbox_home)
    # 1. QSettings keys copied.
    assert new.value("server/url", "") == "http://old.example"
    assert new.value("volume/level", 0, type=int) == 73
    # 2. Marker set.
    assert new.value(_MIGRATION_MARKER, False, type=bool) is True
    # 3. Data + cache dirs moved (not copied) — legacy gone, new present.
    assert not (sandbox_home / ".local" / "share" / "JellyToast").exists()
    assert (sandbox_home / ".local" / "share" / "jellytoast" / "downloads.db").exists()
    assert not (sandbox_home / ".cache" / "JellyToast").exists()
    assert (sandbox_home / ".cache" / "jellytoast" / "covers").exists()
    # 4. queue.json moved into lowercase config dir.
    assert (sandbox_home / ".config" / "jellytoast" / "queue.json").exists()
    assert not (sandbox_home / ".config" / "JellyToast" / "queue.json").exists()
    # 5. Legacy .conf preserved as rollback safety net.
    assert legacy_conf.exists()
    # 6. Keyring entry copied; legacy entry left in place (non-destructive).
    assert fake_keyring.store[("jellytoast", "access_token")] == "secret-token"
    assert fake_keyring.store[("JellyToast", "access_token")] == "secret-token"


def test_second_call_is_noop(sandbox_home, fake_keyring):
    """Once the marker is set, the helper must return immediately —
    no QSettings reads on the legacy side, no keyring touches, no
    filesystem walks. We assert by planting legacy data AFTER setting
    the marker; if anything migrated, the new side would pick it up."""
    from modules.settings import (
        _MIGRATION_MARKER,
        _migrate_legacy_org_name,
    )

    # Pre-mark migration as complete.
    new = _new_qs(sandbox_home)
    new.setValue(_MIGRATION_MARKER, True)
    new.sync()

    # Plant legacy state that would normally trigger a migration.
    old = _legacy_qs(sandbox_home)
    old.setValue("server/url", "http://should-not-migrate")
    old.sync()
    (sandbox_home / ".local" / "share" / "JellyToast").mkdir(parents=True)
    (sandbox_home / ".local" / "share" / "JellyToast" / "downloads.db").write_text("x")
    fake_keyring.store[("JellyToast", "access_token")] = "should-not-copy"

    _migrate_legacy_org_name()

    # New QSettings should NOT have the legacy key.
    new = _new_qs(sandbox_home)
    assert new.value("server/url", "") == ""
    # Filesystem untouched.
    assert (sandbox_home / ".local" / "share" / "JellyToast" / "downloads.db").exists()
    assert not (sandbox_home / ".local" / "share" / "jellytoast").exists()
    # Keyring untouched.
    assert ("jellytoast", "access_token") not in fake_keyring.store
    assert fake_keyring.calls == []


def test_missing_legacy_keyring_does_not_crash_or_write_none(
    sandbox_home,
    fake_keyring,
):
    """Legacy filesystem is present (so we *enter* the migration body)
    but the legacy keyring entry is absent — ``get_password`` returns
    None. The helper must not call ``set_password`` with None (which
    real keyring backends reject) and must not crash."""
    # Plant a minimal legacy footprint so the helper doesn't early-out.
    (sandbox_home / ".local" / "share" / "JellyToast").mkdir(parents=True)
    (sandbox_home / ".local" / "share" / "JellyToast" / "downloads.db").write_text("x")
    # Do NOT plant a legacy keyring entry — get_password returns None.

    from modules.settings import _migrate_legacy_org_name

    _migrate_legacy_org_name()  # must not raise

    # No set_password was ever called.
    set_calls = [c for c in fake_keyring.calls if c[0] == "set"]
    assert set_calls == []
    # And no None was stashed under the new service name.
    assert ("jellytoast", "access_token") not in fake_keyring.store


def test_partial_migration_merges_without_clobbering(
    sandbox_home,
    fake_keyring,
):
    """Simulate an aborted previous run: marker NOT set, but new-side
    state already exists alongside leftover legacy state.

    Observed behaviour (documented here as the contract this test
    pins): the helper

    * copies legacy QSettings keys that the new side doesn't already
      have, but leaves existing new-side keys alone;
    * leaves the legacy data/cache dirs in place if the new dir
      already exists (filesystem move is gated on ``not dst.exists()``);
    * leaves the new-side keyring entry untouched if it already has a
      value, even if the legacy entry also exists.

    In other words: idempotent-merge, never overwrite. Re-running a
    half-finished migration is safe.
    """
    # Legacy QSettings has both a key the new side has and one it doesn't.
    old = _legacy_qs(sandbox_home)
    old.setValue("server/url", "http://old.example")
    old.setValue("keep_existing", "OLD")
    old.sync()

    # New QSettings already has its own value for keep_existing.
    new = _new_qs(sandbox_home)
    new.setValue("keep_existing", "NEW")
    new.sync()

    # Both data dirs exist (interrupted move).
    (sandbox_home / ".local" / "share" / "JellyToast").mkdir(parents=True)
    (sandbox_home / ".local" / "share" / "JellyToast" / "leftover.db").write_text("old")
    (sandbox_home / ".local" / "share" / "jellytoast").mkdir(parents=True)
    (sandbox_home / ".local" / "share" / "jellytoast" / "existing.db").write_text("new")

    # Both keyring entries exist (interrupted copy).
    fake_keyring.store[("JellyToast", "access_token")] = "legacy-secret"
    fake_keyring.store[("jellytoast", "access_token")] = "already-here-token"

    from modules.settings import (
        _MIGRATION_MARKER,
        _migrate_legacy_org_name,
    )

    _migrate_legacy_org_name()

    new = _new_qs(sandbox_home)
    # Net-new key was forwarded.
    assert new.value("server/url", "") == "http://old.example"
    # Existing new-side key was NOT clobbered.
    assert new.value("keep_existing", "") == "NEW"
    # Marker set (migration is now complete).
    assert new.value(_MIGRATION_MARKER, False, type=bool) is True
    # Filesystem: legacy dir remains because lowercase dir already
    # existed — we don't clobber on partial state.
    assert (sandbox_home / ".local" / "share" / "JellyToast" / "leftover.db").exists()
    assert (sandbox_home / ".local" / "share" / "jellytoast" / "existing.db").exists()
    # Keyring: new-side value preserved, not overwritten by legacy.
    assert fake_keyring.store[("jellytoast", "access_token")] == "already-here-token"
