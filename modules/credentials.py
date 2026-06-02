"""Access-token credential crypto + dual-store keyring layer.

Extracted from ``settings.py`` (2026-06-02) so the security-critical
crypto reads in isolation. Two stores back the access token (see
``settings`` module docstring for the rationale):

  Primary:    OS secret store via python-keyring (``_keyring_get_token`` /
              ``_keyring_set_token``), encrypted at rest, OS-managed.
  Resilience: an AES-GCM blob in the QSettings file (``_encrypt_token`` /
              ``_decrypt_token``), keyed by ``_machine_key`` (PBKDF2 over
              /etc/machine-id + $USER) so a stolen config file alone can't
              be decrypted.

``settings`` re-imports every name here, so existing callers
(``modules.airplay2``, the boot ``warm_keyring_async`` in ``jellytoast``,
and the access-token / airplay-credential tests that monkeypatch
``settings._keyring_get_token`` / ``_keyring_set_token``) keep importing
them from ``modules.settings`` unchanged.
"""

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)


# python-keyring identifies entries by (service, username). One token per
# install, so a fixed username is fine. Pre-2026-05-15 the service name
# was "JellyToast"; ``_migrate_legacy_org_name`` copies entries forward
# on first launch under the new name.
_KEYRING_SERVICE = "jellytoast"
_KEYRING_USERNAME = "access_token"
_LEGACY_KEYRING_SERVICE = "JellyToast"

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
        "sha256",
        (mid + ":" + user).encode("utf-8"),
        salt,
        100_000,
    )


def _encrypt_token(plaintext: str) -> str:
    """AES-GCM encrypt with the machine-derived key. Returns
    ``v1:<base64(nonce||ciphertext||tag)>``. Empty input → empty
    string. Encryption failure → empty string (rather than falling
    through to plaintext, which would defeat the whole point)."""
    if not plaintext:
        return ""
    try:
        import base64

        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        key = _machine_key()
        aes = AESGCM(key)
        nonce = os.urandom(12)  # AES-GCM standard nonce size
        ct = aes.encrypt(nonce, plaintext.encode("utf-8"), None)
        return _ENC_PREFIX + base64.b64encode(nonce + ct).decode("ascii")
    except Exception as e:
        logger.warning("token encryption failed: %s", e)
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
        import base64

        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        blob = base64.b64decode(value[len(_ENC_PREFIX) :].encode("ascii"))
        nonce, ct = blob[:12], blob[12:]
        key = _machine_key()
        aes = AESGCM(key)
        return aes.decrypt(nonce, ct, None).decode("utf-8")
    except Exception as e:
        logger.warning("token decryption failed: %s", e)
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


def _keyring_get_token(max_attempts: int = 5, interval_s: float = 0.1) -> Optional[str]:
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
                logger.info(
                    "keyring read succeeded on attempt %s (~%.1fs wait)",
                    attempt + 1,
                    attempt * interval_s,
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
        logger.warning("keyring read failed: %s", last_error)
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
        logger.warning("keyring write failed: %s", e)
        return False
