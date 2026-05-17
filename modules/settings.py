"""
Persistent settings: server, credentials, volume, queue state, preferences.
Uses QSettings (XDG-compliant on Linux: ~/.config/jellytoast/jellytoast.conf)
for non-secret state and a *dual-store* design for the access token:

  Primary:    OS secret store (KDE Wallet / GNOME Keyring / SecretService)
              via python-keyring. Encrypted at rest, OS-managed access.
  Resilience: AES-GCM-encrypted blob in the QSettings config file. The
              symmetric key is derived from /etc/machine-id + $USER via
              PBKDF2-SHA256, so the encrypted blob is only decryptable
              on the same machine as the same user. Config file is
              chmod 600 (owner-only) on top of that.

Dual-store eliminates the boot-time hang we used to see when kwalletd6
hadn't come up yet (token returned None for 8-15 seconds; LoginView
appeared even though the user was actually signed in). It also keeps
the app working on systems without any keyring backend at all — the
encrypted file is the floor.

The QSettings copy is *never* plaintext on disk after a fresh write
under v1+. Pre-v1 plaintext tokens are detected and re-encrypted on
the first read.
"""

import json
import os
import sys
from typing import Optional, TYPE_CHECKING
from PySide6.QtCore import QSettings, QStandardPaths
from pathlib import Path

if TYPE_CHECKING:
    # Queue lives in player_state which imports settings (transitively)
    # — TYPE_CHECKING-only import keeps the runtime cycle broken while
    # still letting type-checkers resolve the forward reference.
    from modules.player_state import Queue


# python-keyring identifies entries by (service, username). One token per
# install, so a fixed username is fine. Pre-2026-05-15 the service name
# was "JellyToast"; ``_migrate_legacy_org_name`` copies entries forward
# on first launch under the new name.
_KEYRING_SERVICE = "jellytoast"
_KEYRING_USERNAME = "access_token"
_LEGACY_KEYRING_SERVICE = "JellyToast"

# QSettings org/app names. Pre-2026-05-15 these were "JellyToast" /
# "JellyToast"; the migration helper copies keys forward on first
# launch and sets a marker so it doesn't re-run.
_QSETTINGS_ORG = "jellytoast"
_QSETTINGS_APP = "jellytoast"
_LEGACY_QSETTINGS_ORG = "JellyToast"
_LEGACY_QSETTINGS_APP = "JellyToast"
_MIGRATION_MARKER = "_migrated_to_lowercase"
# Separate marker for the nested-AppDataLocation recovery (2026-05-16).
# The original lowercase migration only renamed the OUTER org directory,
# leaving the INNER app subdir (Qt's two-level org/app layout) at its
# legacy name — so the post-migration app read from an empty path while
# the real data sat one folder over. This recovery runs ONCE regardless
# of whether the legacy migration already fired, so installs that landed
# in the broken intermediate state heal themselves on next launch.
_NESTED_RECOVERY_MARKER = "_nested_appdata_recovered"

# Version prefix on the QSettings token blob. Anything that doesn't
# start with this is a legacy plaintext value (pre-2026-05-08); we
# detect and re-encrypt on first read so existing installs upgrade
# silently. Bumping the prefix is the migration knob if we ever
# rotate the KDF or cipher.
_ENC_PREFIX = "v1:"


def _machine_key() -> bytes:
    """Derive a 32-byte AES key from a per-machine id + username. Stable
    across reboots; specific to this user on this machine. The key
    isn't stored anywhere — it's recomputed on each encrypt/decrypt
    so a stolen QSettings file alone (without the machine-id and
    matching username) can't be decrypted.

    PBKDF2 with a fixed salt — the salt isn't a secret here, just a
    domain separator so this key isn't reusable for anything else
    if someone composes the same machine-id+user input differently."""
    import getpass
    import hashlib
    import socket
    mid = ""
    # Linux: /etc/machine-id is the canonical stable per-install id.
    for path in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
        try:
            with open(path, encoding="utf-8") as f:
                mid = f.read().strip()
                break
        except OSError:
            continue
    if not mid and os.name == "nt":
        # Windows: HKLM\SOFTWARE\Microsoft\Cryptography\MachineGuid is
        # the stable equivalent. Reading via winreg avoids a pywin32
        # dep. Falls through to hostname-based on access denial.
        try:
            import winreg  # type: ignore[import-not-found]
            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"SOFTWARE\Microsoft\Cryptography",
            ) as k:
                mid, _ = winreg.QueryValueEx(k, "MachineGuid")
        except Exception:
            pass
    if not mid:
        # Containers / minimal installs / other OSes — fall back to
        # hostname + username. Weaker (hostname is shareable) but
        # still deterministic on a given machine and prevents leaking
        # plaintext into the config file.
        mid = f"{socket.gethostname()}:{getpass.getuser()}"
    try:
        user = getpass.getuser()
    except Exception:
        user = os.environ.get("USER") or os.environ.get("LOGNAME") or "unknown"
    salt = b"jellytoast/access_token/v1"
    return hashlib.pbkdf2_hmac(
        "sha256", (mid + ":" + user).encode("utf-8"), salt, 100_000,
    )


def _encrypt_token(plaintext: str) -> str:
    """AES-GCM encrypt with the machine-derived key. Returns
    ``v1:<base64(nonce||ciphertext||tag)>``. Empty input → empty
    string. Encryption failure → empty string (rather than falling
    through to plaintext, which would defeat the whole point)."""
    if not plaintext:
        return ""
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        import base64
        key = _machine_key()
        aes = AESGCM(key)
        nonce = os.urandom(12)  # AES-GCM standard nonce size
        ct = aes.encrypt(nonce, plaintext.encode("utf-8"), None)
        return _ENC_PREFIX + base64.b64encode(nonce + ct).decode("ascii")
    except Exception as e:
        print(f"[jellytoast] token encryption failed: {e}", flush=True)
        return ""


def _decrypt_token(value: str) -> str:
    """Decrypt a stored token blob. Returns the plaintext, or '' on
    failure. Values that don't start with the version prefix are
    treated as legacy plaintext and returned as-is — the caller
    should re-encrypt forward."""
    if not value:
        return ""
    if not value.startswith(_ENC_PREFIX):
        return value  # legacy plaintext, will be re-encrypted on next write
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        import base64
        blob = base64.b64decode(value[len(_ENC_PREFIX):].encode("ascii"))
        nonce, ct = blob[:12], blob[12:]
        key = _machine_key()
        aes = AESGCM(key)
        return aes.decrypt(nonce, ct, None).decode("utf-8")
    except Exception as e:
        print(f"[jellytoast] token decryption failed: {e}", flush=True)
        return ""


def warm_keyring_async() -> None:
    """Fire a throwaway keyring read on a background thread so the
    OS secret service starts coming online while the rest of the app
    constructs. KDE Wayland's kwalletd6 in particular can take 8-10
    seconds to register on the bus after Plasma start, and during
    that window every `keyring.get_password` returns None. By kicking
    a no-op read at module-import time we shift that warm-up onto
    the boot timeline rather than blocking the deferred auth check.

    Idempotent — only fires once per process. Daemon thread so a
    hung secret-service can't keep the app from exiting."""
    if getattr(warm_keyring_async, "_started", False):
        return
    warm_keyring_async._started = True  # type: ignore[attr-defined]
    import threading

    def _warm():
        try:
            import keyring
            keyring.get_password(_KEYRING_SERVICE, _KEYRING_USERNAME)
        except Exception:
            pass

    threading.Thread(target=_warm, daemon=True).start()


def _keyring_get_token(max_attempts: int = 5,
                       interval_s: float = 0.1) -> Optional[str]:
    """Read the access token from the desktop secret store. Returns None
    if keyring isn't installed, no backend is available, or the entry
    doesn't exist yet.

    KDE Wayland's secret service can race app launch — a get_password
    call moments after process start can return None even when the
    entry is present, because the backend hasn't finished registering
    yet. We retry several times with short sleeps before giving up.
    Worst case is ``max_attempts * interval_s`` of added latency on a
    launch where the entry is genuinely absent.

    Defaults: 5 × 100ms = 500ms worst-case stall on the calling
    thread. Acceptable in practice because the dual-store design
    means a keyring miss falls through to the encrypted-file copy
    immediately — the user-visible cost is bounded by this read,
    not by a long retry budget."""
    try:
        import keyring  # lazy: avoids a hard dependency at import time
    except Exception:
        return None
    import time
    last_error = None
    for attempt in range(max_attempts):
        try:
            v = keyring.get_password(_KEYRING_SERVICE, _KEYRING_USERNAME)
        except Exception as e:
            last_error = e
            v = None
        if v:
            if attempt > 0:
                print(
                    f"[jellytoast] keyring read succeeded on attempt "
                    f"{attempt + 1} (~{attempt * interval_s:.1f}s wait)",
                    flush=True,
                )
            return v
        if attempt < max_attempts - 1:
            time.sleep(interval_s)
    # Real backend exceptions (e.g. wallet locked, D-Bus disconnect)
    # are worth surfacing so the user can act on them. A simple "no
    # value" return after exhausting the retry budget is *expected*
    # under the dual-store design — the encrypted-file fallback
    # absorbs it — so logging that case just adds noise to every
    # boot when keyring is sleepy. Stay quiet on the silent-empty
    # path.
    if last_error is not None:
        print(f"[jellytoast] keyring read failed: {last_error}", flush=True)
    return None


def _keyring_set_token(value: str) -> bool:
    """Write or clear the access token in the desktop secret store.
    Returns True on success, False if keyring isn't usable — in which
    case the caller should fall back to QSettings so a missing wallet
    doesn't lock the user out of the app.

    Sign-out path also clears the legacy ``"JellyToast"`` service name
    (pre-rename installs) so the user doesn't end up with two copies
    of the credential after the org migration."""
    try:
        import keyring
    except Exception:
        return False
    try:
        if value:
            keyring.set_password(_KEYRING_SERVICE, _KEYRING_USERNAME, value)
        else:
            for svc in (_KEYRING_SERVICE, _LEGACY_KEYRING_SERVICE):
                try:
                    keyring.delete_password(svc, _KEYRING_USERNAME)
                except Exception:
                    pass  # entry already absent
        return True
    except Exception as e:
        print(f"[jellytoast] keyring write failed: {e}", flush=True)
        return False


def _pick_richer_downloads_db(legacy_db: Path, new_db: Path) -> Path:
    """Return whichever ``downloads.db`` has more rows in the ``nodes``
    table. On a tie or an unreadable file, prefer the legacy one — it's
    the copy the user has been operating on longest, and an unreadable
    new file (most likely a half-initialised SQLite header from the
    "fresh start" the broken app did) shouldn't get to win.

    Counts -1 for unreadable; legacy ≥ new comparison means -1 only
    wins for the new file when the legacy is also unreadable, in which
    case we still return legacy (safe default)."""
    import sqlite3

    def _count(p: Path) -> int:
        try:
            with sqlite3.connect(str(p)) as c:
                row = c.execute(
                    "SELECT COUNT(*) FROM nodes").fetchone()
                return int(row[0]) if row else -1
        except Exception:
            return -1

    legacy_n = _count(legacy_db)
    new_n = _count(new_db)
    if legacy_n >= new_n:
        return legacy_db
    return new_db


def _rename_inner_app_subdir(new_root: Path) -> None:
    """Inside ``new_root`` (a lowercase org dir like
    ``~/.local/share/jellytoast/``), rename the legacy inner app subdir
    (``JellyToast/``) to its lowercase form (``jellytoast/``). If only
    the legacy exists, this is a plain move. If both exist, merge:
    moving any entry from legacy that isn't already at the destination,
    with a downloads.db tiebreaker that prefers whichever DB has more
    rows in ``nodes`` (the broken-state app may have created a near-
    empty DB at the new path while the rich one sat at the legacy)."""
    if not new_root.exists():
        return
    legacy_inner = new_root / _LEGACY_QSETTINGS_APP
    new_inner = new_root / _QSETTINGS_APP
    if not legacy_inner.exists() or not legacy_inner.is_dir():
        return
    # Same-dir on case-insensitive filesystems (macOS HFS+ default,
    # Windows). Skip — there's nothing to rename and a move would error.
    try:
        if new_inner.exists() and legacy_inner.samefile(new_inner):
            return
    except OSError:
        pass

    import shutil
    if not new_inner.exists():
        try:
            shutil.move(str(legacy_inner), str(new_inner))
            print(
                f"[jellytoast] inner-app rename {legacy_inner} → "
                f"{new_inner}", flush=True,
            )
        except OSError as e:
            print(
                f"[jellytoast] inner-app rename {legacy_inner} → "
                f"{new_inner} failed: {e}", flush=True,
            )
        return

    # Both exist: merge. First settle the downloads.db tiebreaker so
    # the richer copy ends up at the new path before the generic
    # "skip if exists" loop runs.
    legacy_db = legacy_inner / "downloads.db"
    new_db = new_inner / "downloads.db"
    if legacy_db.exists() and new_db.exists():
        keep = _pick_richer_downloads_db(legacy_db, new_db)
        if keep == legacy_db:
            try:
                new_db.unlink()
                shutil.move(str(legacy_db), str(new_db))
                print(
                    "[jellytoast] kept richer downloads.db from legacy "
                    "inner app dir", flush=True,
                )
            except OSError as e:
                print(
                    f"[jellytoast] downloads.db merge failed: {e}",
                    flush=True,
                )

    for entry in list(legacy_inner.iterdir()):
        dst = new_inner / entry.name
        if dst.exists():
            continue
        try:
            shutil.move(str(entry), str(dst))
        except OSError as e:
            print(
                f"[jellytoast] inner-merge {entry} → {dst} failed: {e}",
                flush=True,
            )
    # Try cleaning up the legacy inner dir if it's now empty. Leaving
    # a non-empty husk is fine — the user can inspect manually.
    try:
        legacy_inner.rmdir()
    except OSError:
        pass


def _recover_nested_appdata():
    """Run-once recovery for users stuck on the nested-AppDataLocation
    bug. The original lowercase migration left the inner app subdir
    (Qt's two-level ``<org>/<app>/`` layout) at its legacy name, so
    after the org rename the app read from
    ``~/.local/share/jellytoast/jellytoast/`` (empty) while the real
    data sat at ``~/.local/share/jellytoast/JellyToast/``.

    Runs INDEPENDENTLY of ``_MIGRATION_MARKER`` — users who already
    "migrated" but hit this bug need recovery anyway. Sets its own
    marker so it's idempotent."""
    new_qs = QSettings(_QSETTINGS_ORG, _QSETTINGS_APP)
    if new_qs.value(_NESTED_RECOVERY_MARKER, False, type=bool):
        return
    if sys.platform != "linux":
        new_qs.setValue(_NESTED_RECOVERY_MARKER, True)
        new_qs.sync()
        return

    home = Path.home()
    new_config_dir = home / ".config" / _QSETTINGS_ORG
    new_data_dir = home / ".local" / "share" / _QSETTINGS_ORG
    new_cache_dir = home / ".cache" / _QSETTINGS_ORG

    any_legacy_inner = any(
        (root / _LEGACY_QSETTINGS_APP).exists()
        for root in (new_data_dir, new_cache_dir, new_config_dir)
    )
    if any_legacy_inner:
        for root in (new_data_dir, new_cache_dir, new_config_dir):
            _rename_inner_app_subdir(root)
        print(
            "[jellytoast] nested-AppDataLocation recovery complete",
            flush=True,
        )

    new_qs.setValue(_NESTED_RECOVERY_MARKER, True)
    new_qs.sync()


def _migrate_legacy_org_name():
    """One-shot migration from the legacy CamelCase brand ("JellyToast")
    to the lowercase form ("jellytoast"). Runs once per install at
    Settings construction time and sets ``_MIGRATION_MARKER`` so it
    never repeats.

    Three things move forward:

    1. **QSettings keys** — every key under the old org/app is copied
       to the new org/app (when the new path doesn't already have it).
       The legacy ``JellyToast.conf`` file is left in place as a
       belt-and-braces backup.
    2. **Data + cache directories** — ``~/.local/share/JellyToast/``
       and ``~/.cache/JellyToast/`` are filesystem-moved to their
       lowercase equivalents. Filesystem move (not copy) so a large
       downloads tree doesn't double on disk.
    3. **Keyring entry** — the access token under
       ``keyring.get_password("JellyToast", "access_token")`` is
       copied to the new service name. The old entry is left alone
       so a rollback is non-destructive.

    Linux-only for now — the user base on macOS / Windows is
    effectively zero (the rename pre-dates first ship there). Other
    platforms get a fresh-install experience under the new name."""
    new_qs = QSettings(_QSETTINGS_ORG, _QSETTINGS_APP)
    if new_qs.value(_MIGRATION_MARKER, False, type=bool):
        return
    if sys.platform != "linux":
        # Mark migrated so we don't keep checking on platforms with no
        # legacy footprint. New installs land under the lowercase
        # name from the start.
        new_qs.setValue(_MIGRATION_MARKER, True)
        new_qs.sync()
        return

    old_qs = QSettings(_LEGACY_QSETTINGS_ORG, _LEGACY_QSETTINGS_APP)
    legacy_keys = old_qs.allKeys()

    home = Path.home()
    old_config_dir = home / ".config" / _LEGACY_QSETTINGS_ORG
    new_config_dir = home / ".config" / _QSETTINGS_ORG
    old_data_dir = home / ".local" / "share" / _LEGACY_QSETTINGS_ORG
    new_data_dir = home / ".local" / "share" / _QSETTINGS_ORG
    old_cache_dir = home / ".cache" / _LEGACY_QSETTINGS_ORG
    new_cache_dir = home / ".cache" / _QSETTINGS_ORG

    has_legacy_anything = bool(legacy_keys) or any(
        p.exists() for p in (old_config_dir, old_data_dir, old_cache_dir)
    )
    if not has_legacy_anything:
        new_qs.setValue(_MIGRATION_MARKER, True)
        new_qs.sync()
        return

    # 1. Copy QSettings keys. Don't clobber anything the new path
    #    already has — the user might have started a fresh install
    #    on the new code before noticing the old config was around.
    for k in legacy_keys:
        if not new_qs.contains(k):
            new_qs.setValue(k, old_qs.value(k))

    # 2. Filesystem-move the data + cache trees. Move (not copy) so a
    #    multi-GB downloads/ tree doesn't double on disk.
    import shutil
    for src, dst in (
        (old_data_dir, new_data_dir),
        (old_cache_dir, new_cache_dir),
    ):
        if src.exists() and not dst.exists():
            try:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), str(dst))
            except OSError as e:
                print(
                    f"[jellytoast] migrate {src} → {dst} failed: {e}",
                    flush=True,
                )

    # 2b. Rename the nested inner app subdir. Qt's AppDataLocation is
    #     ~/.local/share/<org>/<app>/ — a TWO-level layout — so after
    #     moving the outer org dir above, the user's data is still under
    #     a "JellyToast/" subfolder inside the new lowercase root. Rename
    #     that inner subdir too, or the app reads from an empty path.
    for new_root in (new_data_dir, new_cache_dir):
        _rename_inner_app_subdir(new_root)

    # 3. Move other config files (queue.json, scrobble_queue.json, …).
    #    The legacy .conf file stays put as a rollback safety net —
    #    its keys are already in new_qs from step 1.
    if old_config_dir.exists():
        try:
            new_config_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        for entry in old_config_dir.iterdir():
            if entry.name == f"{_LEGACY_QSETTINGS_APP}.conf":
                continue
            dst = new_config_dir / entry.name
            if not dst.exists():
                try:
                    shutil.move(str(entry), str(dst))
                except OSError as e:
                    print(
                        f"[jellytoast] migrate {entry} → {dst} failed: {e}",
                        flush=True,
                    )

    # 4. Keyring — copy the access token under the new service name.
    try:
        import keyring
        old_token = keyring.get_password(
            _LEGACY_KEYRING_SERVICE, _KEYRING_USERNAME)
        if old_token:
            existing = keyring.get_password(
                _KEYRING_SERVICE, _KEYRING_USERNAME)
            if not existing:
                keyring.set_password(
                    _KEYRING_SERVICE, _KEYRING_USERNAME, old_token)
    except Exception as e:
        print(
            f"[jellytoast] keyring migration failed: {e}", flush=True)

    new_qs.setValue(_MIGRATION_MARKER, True)
    new_qs.sync()
    print(
        "[jellytoast] org-name migration complete "
        "(JellyToast → jellytoast)", flush=True,
    )


class Settings:
    """Wrapper around QSettings with typed accessors."""

    def __init__(self):
        # Run the legacy-org migration before any read/write so a
        # legacy install upgrades transparently on first launch under
        # the lowercase name. Idempotent — sets a marker and exits
        # immediately on subsequent constructions.
        _migrate_legacy_org_name()
        # Separate run-once recovery for the nested-AppDataLocation bug
        # (the original migration only renamed the outer org dir, not
        # the inner app subdir). Independent marker so users who
        # already "migrated" still get rescued.
        _recover_nested_appdata()
        self._s = QSettings(_QSETTINGS_ORG, _QSETTINGS_APP)
        self._config_dir = Path(
            QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppConfigLocation)
        )
        self._config_dir.mkdir(parents=True, exist_ok=True)
        # Lock the config file to owner-only on every Settings init.
        # Qt creates it with the umask default (typically 644 / world-
        # readable); the credential blob deserves 600 even though it's
        # encrypted, since defence-in-depth costs nothing. Idempotent
        # on subsequent runs.
        self._chmod_config_owner_only()

    def _chmod_config_owner_only(self):
        """chmod 600 the QSettings config file. No-op on platforms or
        edge cases where the file path isn't a regular file (Windows
        registry backend, etc.). Errors are swallowed silently —
        permission tightening is best-effort."""
        try:
            path = self._s.fileName()
            if path and os.path.isfile(path):
                os.chmod(path, 0o600)
        except OSError:
            pass

    def flush(self) -> None:
        """Force any pending QSettings writes to disk RIGHT NOW.

        QSettings batches writes in memory and flushes on its own
        cadence (periodic timer + destructor). The destructor path is
        unreliable on KDE Plasma when the app exits via the tray
        ``Quit`` action — the QCoreApplication teardown can skip
        QSettings destruction, leaving recent writes in memory only.
        Callers that absolutely must persist (the post-authenticate
        credential block, the sign-out path) should call this
        explicitly so a subsequent relaunch sees the new state."""
        self._s.sync()

    # ── Server / credentials ────────────────────────────────────────────────
    @property
    def provider_kind(self) -> str:
        """Backend identifier — ``"jellyfin"`` (default) or
        ``"subsonic"`` once that provider lands. Changing this
        without re-authenticating against the new server kind will
        leave the app pointed at incompatible credentials; the
        login flow guards against that by re-probing."""
        return self._s.value("server/provider_kind", "jellyfin", type=str)

    @provider_kind.setter
    def provider_kind(self, v: str):
        self._s.setValue("server/provider_kind", (v or "jellyfin").lower())

    @property
    def server_url(self) -> str:
        return self._s.value("server/url", "", type=str)

    @server_url.setter
    def server_url(self, v: str):
        self._s.setValue("server/url", v)

    @property
    def server_hostnames(self) -> list:
        """Alternate server URLs (Tailscale / LAN / public) layered on top
        of ``server_url``. Stored as a JSON list of dicts
        ``{label: str, url: str, priority: int}``. Empty list (default)
        means single-URL operation — the connectivity tracker behaves
        as it always has. When non-empty, the primary ``server_url`` is
        treated as priority 0 and these alternates are tried in
        ``priority`` order on unreachable transitions."""
        raw = self._s.value("server/hostnames", "", type=str)
        if not raw:
            return []
        try:
            v = json.loads(raw)
        except Exception:
            return []
        if not isinstance(v, list):
            return []
        cleaned: list = []
        for entry in v:
            if not isinstance(entry, dict):
                continue
            url = str(entry.get("url") or "").strip().rstrip("/")
            if not url:
                continue
            label = str(entry.get("label") or url)
            try:
                priority = int(entry.get("priority") or 0)
            except (TypeError, ValueError):
                priority = 0
            cleaned.append(
                {"label": label, "url": url, "priority": priority},
            )
        return cleaned

    @server_hostnames.setter
    def server_hostnames(self, v: list):
        cleaned: list = []
        for entry in (v or []):
            if not isinstance(entry, dict):
                continue
            url = str(entry.get("url") or "").strip().rstrip("/")
            if not url:
                continue
            label = str(entry.get("label") or url)
            try:
                priority = int(entry.get("priority") or 0)
            except (TypeError, ValueError):
                priority = 0
            cleaned.append(
                {"label": label, "url": url, "priority": priority},
            )
        self._s.setValue("server/hostnames", json.dumps(cleaned))

    @property
    def username(self) -> str:
        return self._s.value("server/username", "", type=str)

    @username.setter
    def username(self, v: str):
        self._s.setValue("server/username", v)

    @property
    def access_token(self) -> str:
        # Dual-store read: keyring (OS-managed) paired with an
        # AES-GCM-encrypted QSettings blob (resilience floor — keyring
        # can be sleepy on boot, see [[feedback-dual-store-credentials]]).
        # Both stores get written on every set, but the keyring write
        # can silently fail (D-Bus glitch, locked wallet, kwallet
        # restart) while the QSettings write succeeds. When that
        # happens, the keyring keeps a stale value and the blob has
        # the current one — naively trusting the keyring loads the
        # wrong password forever ("login devolves" symptom). Resolution:
        # when both stores have values AND they differ, the blob wins
        # (it's the one our flush() guarantees) and we rewrite the
        # keyring to match.
        kr = _keyring_get_token()
        stored = self._s.value("server/token", "", type=str)
        blob_decrypted = _decrypt_token(stored) if stored else ""
        if kr and blob_decrypted and kr != blob_decrypted:
            print(
                "[jellytoast] dual-store divergence — keyring stale, "
                "rewriting from encrypted blob",
                flush=True,
            )
            _keyring_set_token(blob_decrypted)
            # Refresh in case the keyring write succeeded — if it
            # failed again we still return the blob's value below.
            kr = blob_decrypted
        if kr:
            # Keep the encrypted QSettings copy in sync. Re-encrypt if
            # the blob is empty or legacy plaintext.
            if (not stored) or (not stored.startswith(_ENC_PREFIX)):
                self._s.setValue("server/token", _encrypt_token(kr))
                self._chmod_config_owner_only()
            return kr
        # Keyring miss — fall back to the blob.
        if not stored:
            return ""
        # Legacy plaintext upgrade: re-encrypt forward.
        if blob_decrypted and not stored.startswith(_ENC_PREFIX):
            self._s.setValue("server/token", _encrypt_token(blob_decrypted))
            self._chmod_config_owner_only()
        return blob_decrypted

    @access_token.setter
    def access_token(self, v: str):
        # Write to *both* stores on every set so they don't drift.
        # Keyring is best-effort — a missing backend is logged but
        # doesn't fail the write; the encrypted QSettings copy alone
        # is enough to keep the user signed in next launch.
        _keyring_set_token(v)
        if v:
            self._s.setValue("server/token", _encrypt_token(v))
            self._chmod_config_owner_only()
        else:
            self._s.remove("server/token")

    @property
    def user_id(self) -> str:
        return self._s.value("server/user_id", "", type=str)

    @user_id.setter
    def user_id(self, v: str):
        self._s.setValue("server/user_id", v)

    @property
    def device_id(self) -> str:
        existing = self._s.value("server/device_id", "", type=str)
        if not existing:
            import uuid
            existing = str(uuid.uuid4())
            self._s.setValue("server/device_id", existing)
        return existing

    # ── Window geometry ─────────────────────────────────────────────────────
    # Persisted as the QByteArray QMainWindow.saveGeometry() returns —
    # opaque to us but round-trippable through QMainWindow.restoreGeometry.
    # Empty => no saved geometry, caller picks default.
    @property
    def window_geometry(self) -> bytes:
        from PySide6.QtCore import QByteArray
        v = self._s.value("ui/window_geometry")
        if isinstance(v, QByteArray):
            return bytes(v)
        if isinstance(v, (bytes, bytearray)):
            return bytes(v)
        return b""

    @window_geometry.setter
    def window_geometry(self, v: bytes):
        from PySide6.QtCore import QByteArray
        self._s.setValue("ui/window_geometry", QByteArray(v or b""))

    # Mini player geometry — same QByteArray blob from saveGeometry.
    # Mode is tracked separately because setFixedSize in compact mode
    # would otherwise fight restoreGeometry's size; we switch the mode
    # first, then apply the geometry blob.
    @property
    def mini_player_geometry(self) -> bytes:
        from PySide6.QtCore import QByteArray
        v = self._s.value("ui/mini_player_geometry")
        if isinstance(v, QByteArray):
            return bytes(v)
        if isinstance(v, (bytes, bytearray)):
            return bytes(v)
        return b""

    @mini_player_geometry.setter
    def mini_player_geometry(self, v: bytes):
        from PySide6.QtCore import QByteArray
        self._s.setValue("ui/mini_player_geometry", QByteArray(v or b""))

    @property
    def mini_player_mode(self) -> str:
        v = self._s.value("ui/mini_player_mode", "compact", type=str)
        return v if v in ("compact", "expanded") else "compact"

    @mini_player_mode.setter
    def mini_player_mode(self, v: str):
        self._s.setValue(
            "ui/mini_player_mode",
            v if v in ("compact", "expanded") else "compact",
        )

    @property
    def mini_player_expanded_width(self) -> int:
        """Last user-set width of the expanded mini player. Persisted
        across sessions so a fresh launch re-opens at the size the
        user dragged it to. Tracked separately from saveGeometry()
        because that blob only captures the size of whichever mode
        was active at close time — without this, a session that ends
        in compact would forget the expanded width."""
        v = self._s.value("ui/mini_player_expanded_width", 300, type=int)
        try:
            return int(v)
        except (TypeError, ValueError):
            return 300

    @mini_player_expanded_width.setter
    def mini_player_expanded_width(self, v: int):
        self._s.setValue("ui/mini_player_expanded_width", int(v))

    # ── Playback ────────────────────────────────────────────────────────────
    @property
    def volume(self) -> int:
        return self._s.value("playback/volume", 80, type=int)

    @volume.setter
    def volume(self, v: int):
        self._s.setValue("playback/volume", max(0, min(100, v)))

    @property
    def repeat_mode(self) -> str:
        return self._s.value("playback/repeat", "off", type=str)

    @repeat_mode.setter
    def repeat_mode(self, v: str):
        self._s.setValue("playback/repeat", v)

    @property
    def shuffle(self) -> bool:
        return self._s.value("playback/shuffle", False, type=bool)

    @shuffle.setter
    def shuffle(self, v: bool):
        self._s.setValue("playback/shuffle", v)

    @property
    def audio_quality(self) -> str:
        # 'original' (direct play) or a bitrate string like '320', '192', '128'
        return self._s.value("playback/audio_quality", "original", type=str)

    @audio_quality.setter
    def audio_quality(self, v: str):
        self._s.setValue("playback/audio_quality", v)

    @property
    def download_quality(self) -> str:
        """Quality downloaded copies are fetched at — independent of the
        playback `audio_quality`. 'original' (default) keeps the source
        file bit-perfect; a kbps string ('320', '192', …) pulls a
        smaller server-transcoded copy to save disk."""
        return self._s.value("playback/download_quality", "original",
                             type=str)

    @download_quality.setter
    def download_quality(self, v: str):
        self._s.setValue("playback/download_quality", v)

    @property
    def cast_stream_routing(self) -> str:
        """How a cast device should reach the media stream:
          'auto'   — direct URL when the server looks LAN-reachable
                     (private IP), proxy through this machine otherwise
                     (Tailscale / public / hostname). Default.
          'proxy'  — always relay the stream through this machine's
                     local HTTP server (max compatibility).
          'direct' — never proxy; hand the cast device the server URL
                     verbatim (most efficient, multi-room friendly)."""
        v = self._s.value("playback/cast_stream_routing", "auto", type=str)
        return v if v in ("auto", "proxy", "direct") else "auto"

    @cast_stream_routing.setter
    def cast_stream_routing(self, v: str):
        v = (v or "auto").lower()
        self._s.setValue("playback/cast_stream_routing",
                         v if v in ("auto", "proxy", "direct") else "auto")

    @property
    def favorite_cast_devices(self) -> list:
        """Cast devices the user has hearted in the picker — pinned to
        the top of the list, and surfaced in the cast button's right-
        click menu. Stored as a JSON array of {uuid, name, type} dicts
        so the right-click menu can label each entry without waiting on
        a live discovery scan."""
        import json
        raw = self._s.value("playback/favorite_cast_devices", "", type=str)
        if not raw:
            return []
        try:
            v = json.loads(raw)
        except Exception:
            return []
        if not isinstance(v, list):
            return []
        out = []
        for entry in v:
            # Tolerate the legacy bare-uuid-string format from the
            # first cut of this feature.
            if isinstance(entry, str):
                out.append({"uuid": entry, "name": entry, "type": ""})
            elif isinstance(entry, dict) and entry.get("uuid"):
                out.append({
                    "uuid": str(entry["uuid"]),
                    "name": str(entry.get("name") or entry["uuid"]),
                    "type": str(entry.get("type") or ""),
                })
        return out

    @favorite_cast_devices.setter
    def favorite_cast_devices(self, v):
        import json
        cleaned = []
        for entry in (v or []):
            if isinstance(entry, dict) and entry.get("uuid"):
                cleaned.append({
                    "uuid": str(entry["uuid"]),
                    "name": str(entry.get("name") or entry["uuid"]),
                    "type": str(entry.get("type") or ""),
                })
        self._s.setValue("playback/favorite_cast_devices",
                         json.dumps(cleaned))

    @property
    def favorite_cast_device_ids(self) -> set:
        """Just the uuids from favorite_cast_devices — for the picker's
        is-this-device-hearted check."""
        return {d["uuid"] for d in self.favorite_cast_devices}

    @property
    def cast_member_volumes(self) -> dict:
        """Per-speaker volume balance for Chromecast groups, keyed by
        ``{group_uuid: {speaker_uuid: 0-100}}``. Persists the dialed-in
        balance across cast sessions so a kitchen-loud, living-room-quiet
        mix stays put after the speakers go to sleep."""
        import json
        raw = self._s.value("playback/cast_member_volumes", "", type=str)
        if not raw:
            return {}
        try:
            v = json.loads(raw)
        except Exception:
            return {}
        if not isinstance(v, dict):
            return {}
        out = {}
        for gid, members in v.items():
            if not isinstance(members, dict):
                continue
            cleaned = {}
            for sid, vol in members.items():
                try:
                    cleaned[str(sid)] = max(0, min(100, int(vol)))
                except (TypeError, ValueError):
                    continue
            if cleaned:
                out[str(gid)] = cleaned
        return out

    @cast_member_volumes.setter
    def cast_member_volumes(self, v: dict):
        import json
        cleaned: dict = {}
        for gid, members in (v or {}).items():
            if not isinstance(members, dict):
                continue
            inner: dict = {}
            for sid, vol in members.items():
                try:
                    inner[str(sid)] = max(0, min(100, int(vol)))
                except (TypeError, ValueError):
                    continue
            if inner:
                cleaned[str(gid)] = inner
        self._s.setValue("playback/cast_member_volumes", json.dumps(cleaned))

    @property
    def gapless(self) -> bool:
        return self._s.value("playback/gapless", True, type=bool)

    @gapless.setter
    def gapless(self, v: bool):
        self._s.setValue("playback/gapless", v)

    @property
    def prefer_server_when_online(self) -> bool:
        """When a track is downloaded *and* the server is reachable,
        whether to still stream from the server instead of playing the
        local copy. Default False — the local copy is faster and uses
        no bandwidth, so prefer it whenever it exists. Offline mode and
        an unreachable server always force the local copy regardless."""
        return self._s.value(
            "playback/prefer_server_when_online", False, type=bool)

    @prefer_server_when_online.setter
    def prefer_server_when_online(self, v: bool):
        self._s.setValue("playback/prefer_server_when_online", bool(v))

    # Sleep-timer fade duration: how long the linear volume ramp takes
    # when the user picks the "fade to stop" mode. 8s feels musical
    # (a long-enough breath that you notice the fade, short enough that
    # you don't think the player has crashed). Clamped to 1–60s.
    _SLEEP_FADE_MIN_MS = 1000
    _SLEEP_FADE_MAX_MS = 60000

    @property
    def sleep_fade_duration_ms(self) -> int:
        v = self._s.value("playback/sleep_fade_duration_ms", 8000, type=int)
        return max(self._SLEEP_FADE_MIN_MS,
                   min(self._SLEEP_FADE_MAX_MS, int(v)))

    @sleep_fade_duration_ms.setter
    def sleep_fade_duration_ms(self, v: int):
        clamped = max(self._SLEEP_FADE_MIN_MS,
                      min(self._SLEEP_FADE_MAX_MS, int(v)))
        self._s.setValue("playback/sleep_fade_duration_ms", clamped)

    @property
    def auto_offline_mode(self) -> bool:
        """When True, the connectivity tracker flips offline mode on
        automatically once a string of API failures declares the server
        unreachable, and flips it back off on the first successful call
        after reconnect. The user's explicit offline-mode toggle takes
        precedence — when they manually set it, auto won't undo their
        choice. Default True: failure handling should feel automatic
        rather than something the user has to opt into."""
        return self._s.value("offline/auto_offline_mode", True, type=bool)

    @auto_offline_mode.setter
    def auto_offline_mode(self, v: bool):
        self._s.setValue("offline/auto_offline_mode", bool(v))

    @property
    def downloads_paused(self) -> bool:
        """Persisted queue-paused flag for the download manager. A paused
        queue stays paused across a restart — the user's intent survives
        the process. Flipped by ``modules.offline.manager.pause`` /
        ``resume``; not exposed in the UI as a free-standing setting."""
        return self._s.value("downloads/paused", False, type=bool)

    @downloads_paused.setter
    def downloads_paused(self, v: bool):
        self._s.setValue("downloads/paused", bool(v))

    @property
    def offline_mode(self) -> bool:
        """Persisted offline-mode flag. Survives restart so a user who
        was offline on shutdown comes back in offline mode rather than
        watching the connectivity probe re-race their library load."""
        return self._s.value("offline/offline_mode", False, type=bool)

    @offline_mode.setter
    def offline_mode(self, v: bool):
        self._s.setValue("offline/offline_mode", bool(v))

    @property
    def replaygain(self) -> str:
        # 'no' | 'track' | 'album'
        return self._s.value("playback/replaygain", "track", type=str)

    @replaygain.setter
    def replaygain(self, v: str):
        self._s.setValue("playback/replaygain", v)

    # ── Equalizer ──────────────────────────────────────────────────────────
    # Scaffold for the 10-band graphic EQ. See `docs/research/eq_dsp.md`
    # and `modules/eq_presets.py` for the band layout. UI lands in a
    # follow-up — these properties exist so the backend can wire mpv's
    # `anequalizer` filter without the surface in place yet.

    @property
    def eq_enabled(self) -> bool:
        """Master EQ on/off. Off by default — EQ on means the audio is
        no longer bit-perfect, and the disclosure UI for that lands
        with the slider page."""
        return self._s.value("playback/eq_enabled", False, type=bool)

    @eq_enabled.setter
    def eq_enabled(self, v: bool):
        self._s.setValue("playback/eq_enabled", bool(v))

    @property
    def eq_preset(self) -> str:
        """Last-selected preset name. ``Custom`` once the user drags
        any band; the UI follow-up owns that transition. Default
        ``Flat`` so the first read on a fresh install picks a valid
        entry from ``modules.eq_presets.PRESETS``."""
        return self._s.value("playback/eq_preset", "Flat", type=str)

    @eq_preset.setter
    def eq_preset(self, v: str):
        self._s.setValue("playback/eq_preset", (v or "Flat").strip())

    @property
    def eq_bands(self) -> list:
        """Per-band gains in dB, ordered to match
        ``eq_presets.BAND_FREQUENCIES`` (31..16k). Stored as a JSON
        string so QSettings doesn't mangle the float list into a
        QStringList (same pattern as ``favorite_cast_devices``).

        Always returns a list of exactly 10 floats — short / long /
        non-numeric / unparseable values fall back to a zero list so
        the backend can call ``apply_eq`` blindly without each caller
        re-validating shape.
        """
        from modules.eq_presets import BAND_COUNT
        raw = self._s.value("playback/eq_bands", "", type=str)
        if not raw:
            return [0.0] * BAND_COUNT
        try:
            v = json.loads(raw)
        except Exception:
            return [0.0] * BAND_COUNT
        if not isinstance(v, list) or len(v) != BAND_COUNT:
            return [0.0] * BAND_COUNT
        out: list = []
        for entry in v:
            try:
                out.append(float(entry))
            except (TypeError, ValueError):
                out.append(0.0)
        return out

    @eq_bands.setter
    def eq_bands(self, v):
        from modules.eq_presets import BAND_COUNT
        cleaned: list = []
        for entry in (v or []):
            try:
                cleaned.append(float(entry))
            except (TypeError, ValueError):
                cleaned.append(0.0)
        # Pad or truncate so the stored value always matches the
        # band count — defends against future band-count changes
        # writing through a half-filled list.
        if len(cleaned) < BAND_COUNT:
            cleaned.extend([0.0] * (BAND_COUNT - len(cleaned)))
        elif len(cleaned) > BAND_COUNT:
            cleaned = cleaned[:BAND_COUNT]
        self._s.setValue("playback/eq_bands", json.dumps(cleaned))

    @property
    def media_controls_enabled(self) -> bool:
        """OS media-key + MPRIS integration. When False, the
        MediaControlsService is not started at boot, so system media
        keys and KDE/GNOME's media-control widget see nothing for
        jellytoast. Defaults to True — the integration is the expected
        behavior on Linux desktops."""
        return self._s.value("playback/media_controls_enabled", True, type=bool)

    @media_controls_enabled.setter
    def media_controls_enabled(self, v: bool):
        self._s.setValue("playback/media_controls_enabled", bool(v))

    @property
    def show_streaming_info(self) -> bool:
        """Show a small "Streaming {container} · {bitrate} kbps"
        label above the play button in the bottom transport bar.
        Off by default — extra information for users who want to
        verify they're getting the original-quality stream they
        expect (e.g., FLAC vs transcoded MP3)."""
        return self._s.value("playback/show_streaming_info", False, type=bool)

    @show_streaming_info.setter
    def show_streaming_info(self, v: bool):
        self._s.setValue("playback/show_streaming_info", bool(v))

    # ── Scrobbling (ListenBrainz + Last.fm) ────────────────────────────────
    # Tokens are AES-GCM encrypted at rest with the same machine-derived
    # key the Jellyfin/Subsonic access token uses (see _encrypt_token).
    # ListenBrainz is plain user-token; Last.fm is a permanent session
    # key obtained via the desktop browser-auth flow. Display names
    # ("…_username") are stored plaintext for the settings UI.

    @property
    def listenbrainz_enabled(self) -> bool:
        return self._s.value("scrobble/listenbrainz_enabled", False, type=bool)

    @listenbrainz_enabled.setter
    def listenbrainz_enabled(self, v: bool):
        self._s.setValue("scrobble/listenbrainz_enabled", bool(v))

    @property
    def listenbrainz_token(self) -> str:
        stored = self._s.value("scrobble/listenbrainz_token", "", type=str)
        if not stored:
            return ""
        decrypted = _decrypt_token(stored)
        # Forward-migrate legacy plaintext (no v1: prefix).
        if decrypted and not stored.startswith(_ENC_PREFIX):
            self._s.setValue(
                "scrobble/listenbrainz_token", _encrypt_token(decrypted),
            )
        return decrypted

    @listenbrainz_token.setter
    def listenbrainz_token(self, v: str):
        self._s.setValue(
            "scrobble/listenbrainz_token", _encrypt_token(v or ""),
        )

    @property
    def listenbrainz_url(self) -> str:
        """Base URL for the ListenBrainz API. Defaults to the canonical
        instance; users on a Maloja or self-hosted ListenBrainz point
        this elsewhere — same knob Navidrome's own scrobbler exposes."""
        return self._s.value(
            "scrobble/listenbrainz_url",
            "https://api.listenbrainz.org", type=str,
        )

    @listenbrainz_url.setter
    def listenbrainz_url(self, v: str):
        self._s.setValue("scrobble/listenbrainz_url", (v or "").strip())

    @property
    def listenbrainz_username(self) -> str:
        """Resolved username from validate-token. Display only — used by
        the settings page to show "Connected as <name>". Empty until the
        user has validated."""
        return self._s.value("scrobble/listenbrainz_username", "", type=str)

    @listenbrainz_username.setter
    def listenbrainz_username(self, v: str):
        self._s.setValue("scrobble/listenbrainz_username", v or "")

    @property
    def lastfm_enabled(self) -> bool:
        return self._s.value("scrobble/lastfm_enabled", False, type=bool)

    @lastfm_enabled.setter
    def lastfm_enabled(self, v: bool):
        self._s.setValue("scrobble/lastfm_enabled", bool(v))

    @property
    def lastfm_session_key(self) -> str:
        stored = self._s.value("scrobble/lastfm_session_key", "", type=str)
        if not stored:
            return ""
        decrypted = _decrypt_token(stored)
        if decrypted and not stored.startswith(_ENC_PREFIX):
            self._s.setValue(
                "scrobble/lastfm_session_key", _encrypt_token(decrypted),
            )
        return decrypted

    @lastfm_session_key.setter
    def lastfm_session_key(self, v: str):
        self._s.setValue(
            "scrobble/lastfm_session_key", _encrypt_token(v or ""),
        )

    @property
    def lastfm_username(self) -> str:
        """Display-only username returned by auth.getSession."""
        return self._s.value("scrobble/lastfm_username", "", type=str)

    @lastfm_username.setter
    def lastfm_username(self, v: str):
        self._s.setValue("scrobble/lastfm_username", v or "")

    # Server-side scrobbling detection (set on Navidrome login by
    # modules.scrobble.navidrome_detect). The Settings → Scrobbling
    # page reads these to surface "Your server is scrobbling for you"
    # banners and to disable the in-app enable checkboxes — preventing
    # the double-scrobble case automatically when we can prove it.
    # ``server_is_navidrome`` is True when ping reported a Navidrome
    # server, regardless of whether the native-API probe succeeded;
    # the warning banner uses it to strengthen its language.

    @property
    def server_is_navidrome(self) -> bool:
        return self._s.value("scrobble/server_is_navidrome", False, type=bool)

    @server_is_navidrome.setter
    def server_is_navidrome(self, v: bool):
        self._s.setValue("scrobble/server_is_navidrome", bool(v))

    @property
    def server_scrobbles_lastfm(self) -> bool:
        return self._s.value(
            "scrobble/server_scrobbles_lastfm", False, type=bool)

    @server_scrobbles_lastfm.setter
    def server_scrobbles_lastfm(self, v: bool):
        self._s.setValue("scrobble/server_scrobbles_lastfm", bool(v))

    @property
    def server_scrobbles_listenbrainz(self) -> bool:
        return self._s.value(
            "scrobble/server_scrobbles_listenbrainz", False, type=bool)

    @server_scrobbles_listenbrainz.setter
    def server_scrobbles_listenbrainz(self, v: bool):
        self._s.setValue(
            "scrobble/server_scrobbles_listenbrainz", bool(v))

    @property
    def server_scrobble_check_done(self) -> bool:
        """True once we've successfully read the Navidrome user record
        at least once. The settings UI uses this to distinguish "we
        couldn't tell" (banner says: leave off if you've enabled it
        there) from "we know" (banner says: server is scrobbling for
        you, in-app off)."""
        return self._s.value(
            "scrobble/server_scrobble_check_done", False, type=bool)

    @server_scrobble_check_done.setter
    def server_scrobble_check_done(self, v: bool):
        self._s.setValue(
            "scrobble/server_scrobble_check_done", bool(v))

    # ── Resume position ────────────────────────────────────────────────────
    # Stored as ms position + item_id pair so a relaunch can verify the
    # position belongs to the queue's current track. If the queue
    # advanced (or skipped) without a clean position-write between, the
    # id won't match and we ignore the stale position.

    @property
    def saved_position_ms(self) -> int:
        return self._s.value("playback/position_ms", 0, type=int)

    @saved_position_ms.setter
    def saved_position_ms(self, v: int):
        self._s.setValue("playback/position_ms", max(0, int(v)))

    @property
    def saved_position_item_id(self) -> str:
        return self._s.value("playback/position_item_id", "", type=str)

    @saved_position_item_id.setter
    def saved_position_item_id(self, v: str):
        self._s.setValue("playback/position_item_id", v or "")

    # ── UI ──────────────────────────────────────────────────────────────────
    @property
    def show_mini_on_start(self) -> bool:
        return self._s.value("ui/mini_on_start", False, type=bool)

    @show_mini_on_start.setter
    def show_mini_on_start(self, v: bool):
        self._s.setValue("ui/mini_on_start", v)

    @property
    def minimize_to_tray(self) -> bool:
        return self._s.value("ui/minimize_to_tray", True, type=bool)

    @minimize_to_tray.setter
    def minimize_to_tray(self, v: bool):
        self._s.setValue("ui/minimize_to_tray", v)

    @property
    def show_tooltips(self) -> bool:
        """Hover tooltips across the app. On by default — helpful for
        new users — but a global QApplication event filter swallows
        QEvent.ToolTip when this is off, so it applies live with no
        restart."""
        return self._s.value("ui/show_tooltips", True, type=bool)

    @show_tooltips.setter
    def show_tooltips(self, v: bool):
        self._s.setValue("ui/show_tooltips", bool(v))

    @property
    def autostart(self) -> bool:
        # Whether jellytoast launches on login. Backed by an XDG
        # autostart .desktop file, not just this flag — see
        # modules.autostart for the actual filesystem state. This
        # property mirrors the file's presence so the settings UI can
        # show the right initial check state without hitting the disk.
        return self._s.value("ui/autostart", False, type=bool)

    @autostart.setter
    def autostart(self, v: bool):
        self._s.setValue("ui/autostart", v)

    @property
    def home_destination(self) -> str:
        # Native music surface the top-bar Home button routes to:
        # "albums" | "playlists" | "artists" | "songs" | "genres" |
        # "suggestions". Defaults to "albums" — the canonical music
        # landing.
        return self._s.value("ui/home_destination", "albums", type=str)

    @home_destination.setter
    def home_destination(self, v: str):
        self._s.setValue("ui/home_destination", v)

    @property
    def mini_player_keep_above(self) -> bool:
        # Wayland-only knob: when true, jellytoast installs a KWin
        # window rule (~/.config/kwinrulesrc) that pins the mini player
        # above other windows. Off by default — the rule modifies a
        # user-global config file, so we want explicit opt-in.
        return self._s.value("ui/mini_player_keep_above", False, type=bool)

    @mini_player_keep_above.setter
    def mini_player_keep_above(self, v: bool):
        self._s.setValue("ui/mini_player_keep_above", v)

    @property
    def theme_mode(self) -> str:
        # "frosted_dark" (current default) | "dark" | "transparent" | "light"
        # Only frosted_dark is wired up; the rest are reserved.
        return self._s.value("ui/theme_mode", "frosted_dark", type=str)

    @theme_mode.setter
    def theme_mode(self, v: str):
        self._s.setValue("ui/theme_mode", v)

    @property
    def accent_color(self) -> str:
        """Hex string (``#rrggbb``) overriding the active theme's accent.
        Empty string means "use the theme default". Validated lazily by
        ``get_active_theme()`` — bad hex falls back to the theme."""
        v = self._s.value("ui/accent_color", "#967de1", type=str)
        # One-shot migration: users who stored a previous (brighter)
        # preset value get bumped to the new subdued tone for that
        # same color. Trade-off: anyone who happened to have hand-
        # picked one of the legacy values exactly gets migrated too,
        # but the new value is the same intent in the same family.
        _LEGACY_TO_SUBDUED = {
            "#a78bfa": "#967de1",   # Purple
            "#00a4dc": "#0093c6",   # Blue
            "#22c5be": "#1eb1ab",   # Teal
            "#34d399": "#2fbe8a",   # Green
            "#f472b6": "#dc66a4",   # Pink
            "#fb923c": "#e28336",   # Orange
            "#ef4444": "#d73d3d",   # Red
        }
        if v in _LEGACY_TO_SUBDUED:
            v = _LEGACY_TO_SUBDUED[v]
            self._s.setValue("ui/accent_color", v)
        return v

    @accent_color.setter
    def accent_color(self, v: str):
        self._s.setValue("ui/accent_color", (v or "").strip())

    @property
    def library_page_size(self) -> int:
        """Items per page when paginated. 0 means "load all in one
        fetch" — disables pagination. Default 200: small enough that
        cold-start paint is fast, large enough that most libraries
        fit in 1–2 pages. Read at LibraryGrid.load_items time, so a
        change applies on the next browse, not the current rendering."""
        return self._s.value("ui/library_page_size", 200, type=int)

    @library_page_size.setter
    def library_page_size(self, v: int):
        self._s.setValue("ui/library_page_size", max(0, int(v)))

    @property
    def shuffle_queue_size(self) -> int:
        """Number of tracks pulled into the queue by "Shuffle library".
        Default 100 — keeps the queue render snappy on commit (1 mutation
        in a 500-track queue caused a multi-frame redraw). Capped at
        1000 so a user can crank it up for marathon listening, but the
        UI stays responsive."""
        return self._s.value("ui/shuffle_queue_size", 100, type=int)

    @shuffle_queue_size.setter
    def shuffle_queue_size(self, v: int):
        self._s.setValue("ui/shuffle_queue_size", max(10, min(1000, int(v))))

    @property
    def library_cover_prefetch(self) -> bool:
        """When True (default), the LibraryGrid background-prefetches
        every tile's cover after the chunked render finishes so a
        later scroll doesn't trigger a fresh round-trip per row.
        Off for users on metered connections who want covers loaded
        only as tiles enter the viewport."""
        return self._s.value("ui/library_cover_prefetch", True, type=bool)

    @library_cover_prefetch.setter
    def library_cover_prefetch(self, v: bool):
        self._s.setValue("ui/library_cover_prefetch", bool(v))

    @property
    def library_view_mode(self) -> str:
        """Library grid render mode: "grid" (multi-column tile grid,
        default) or "list" (single-column row stack). Persisted so
        the toggle survives across launches."""
        v = self._s.value("ui/library_view_mode", "grid", type=str)
        return v if v in ("grid", "list") else "grid"

    @library_view_mode.setter
    def library_view_mode(self, v: str):
        self._s.setValue(
            "ui/library_view_mode",
            v if v in ("grid", "list") else "grid",
        )

    @property
    def library_tile_fade(self) -> bool:
        """When True (default), tiles fade in over 180ms as their
        cover lands. Off skips the QPropertyAnimation and reveals
        instantly — slightly cheaper on very slow systems and a
        matter of taste."""
        return self._s.value("ui/library_tile_fade", True, type=bool)

    @library_tile_fade.setter
    def library_tile_fade(self, v: bool):
        self._s.setValue("ui/library_tile_fade", bool(v))

    @property
    def library_sort_by(self) -> str:
        # Jellyfin SortBy parameter. Defaults to SortName so the first
        # launch lands on alphabetical-by-album-name (also JF Web's
        # default). User picks via the top-bar sort dropdown.
        return self._s.value("ui/library_sort_by", "SortName", type=str)

    @library_sort_by.setter
    def library_sort_by(self, v: str):
        self._s.setValue("ui/library_sort_by", v)

    @property
    def library_sort_order(self) -> str:
        # "ascending" | "descending" — the jellytoast top-bar string,
        # mapped to Jellyfin's SortOrder casing in AlbumLibraryGrid.
        return self._s.value("ui/library_sort_order", "ascending", type=str)

    @library_sort_order.setter
    def library_sort_order(self, v: str):
        self._s.setValue("ui/library_sort_order", v)

    @property
    def lyrics_font_size(self) -> str:
        # "small" | "default" | "large" | "largest" — controls the
        # active and inactive lyric line sizes + line padding on the
        # now-playing page. Default keeps the post-Phase-3 compact
        # sizing (active 18/600, inactive 13/400).
        return self._s.value("ui/lyrics_font_size", "default", type=str)

    @lyrics_font_size.setter
    def lyrics_font_size(self, v: str):
        self._s.setValue("ui/lyrics_font_size", v)

    @property
    def font_scale(self) -> str:
        # "small" | "default" | "large" | "largest" — multiplies every
        # design-token font size at module-import time. Consumed by
        # `modules.design_tokens`; restart required to take effect
        # because the tokens are baked into class-level constants and
        # then splattered into QSS strings across every widget at
        # construction.
        return self._s.value("ui/font_scale", "default", type=str)

    @font_scale.setter
    def font_scale(self, v: str):
        self._s.setValue("ui/font_scale", v)

    # ── Queue persistence ───────────────────────────────────────────────────
    def save_queue(self, queue: "Queue"):
        """Persist the full Queue (context + original items + play_order +
        current index + manual overlay). The on-disk schema is bumped to
        v2; v1 (`{queue, index}` only) is read transparently in `load_queue`.
        """
        path = self._config_dir / "queue.json"
        # tmp + os.replace so a crash mid-write leaves the previous good
        # queue.json intact (truncated json silently returns None on the
        # next launch — the user loses their queue).
        tmp = path.with_suffix(".json.tmp")
        try:
            payload = {"version": 2, **queue.to_dict()}
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f)
            os.replace(tmp, path)
        except Exception:
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass

    def load_queue(self) -> Optional["Queue"]:
        """Returns the persisted Queue or None if nothing's saved. Reads
        both the v2 schema (full Queue) and the legacy v1 schema (flat
        items + index, treated as a MANUAL context with sequential
        play_order)."""
        # Lazy import — settings.py is imported very early and player_state
        # imports settings indirectly via QueueManager, so deferring here
        # avoids the cycle. Reading queue.json doesn't happen at import.
        from modules.player_state import Queue, QueueContext, QueueKind
        path = self._config_dir / "queue.json"
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return None
        if isinstance(data, dict) and data.get("version") == 2:
            try:
                return Queue.from_dict(data)
            except Exception:
                return None
        # Legacy: {"queue": [...], "index": N}
        if isinstance(data, dict) and "queue" in data:
            items = data.get("queue") or []
            idx = data.get("index", -1)
            if not items:
                return None
            return Queue(
                context=QueueContext(kind=QueueKind.MANUAL),
                original_items=list(items),
                play_order=list(range(len(items))),
                current_index=idx if 0 <= idx < len(items) else -1,
            )
        return None

    def clear(self):
        self._s.clear()


# Module-level singleton
_settings: Optional[Settings] = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
