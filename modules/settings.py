"""
Persistent settings: server, credentials, volume, queue state, preferences.
Uses QSettings (XDG-compliant on Linux: ~/.config/JellyToast/JellyToast.conf)
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
from typing import Optional, TYPE_CHECKING
from PySide6.QtCore import QSettings, QStandardPaths
from pathlib import Path

if TYPE_CHECKING:
    # Queue lives in player_state which imports settings (transitively)
    # — TYPE_CHECKING-only import keeps the runtime cycle broken while
    # still letting type-checkers resolve the forward reference.
    from modules.player_state import Queue


# python-keyring identifies entries by (service, username). One token per
# install, so a fixed username is fine.
_KEYRING_SERVICE = "JellyToast"
_KEYRING_USERNAME = "access_token"

# Version prefix on the QSettings token blob. Anything that doesn't
# start with this is a legacy plaintext value (pre-2026-05-08); we
# detect and re-encrypt on first read so existing installs upgrade
# silently. Bumping the prefix is the migration knob if we ever
# rotate the KDF or cipher.
_ENC_PREFIX = "v1:"


def _machine_key() -> bytes:
    """Derive a 32-byte AES key from /etc/machine-id + $USER. Stable
    across reboots; specific to this user on this machine. The key
    isn't stored anywhere — it's recomputed on each encrypt/decrypt
    so a stolen QSettings file alone (without the machine-id and
    matching username) can't be decrypted.

    PBKDF2 with a fixed salt — the salt isn't a secret here, just a
    domain separator so this key isn't reusable for anything else
    if someone composes the same machine-id+user input differently."""
    import hashlib
    mid = ""
    for path in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
        try:
            with open(path) as f:
                mid = f.read().strip()
                break
        except OSError:
            continue
    if not mid:
        # Containers / minimal installs may have neither file — fall
        # back to hostname + UID. Weaker (hostname is shareable) but
        # still deterministic on a given machine and prevents leaking
        # plaintext into the config file.
        import socket
        mid = f"{socket.gethostname()}:{os.getuid()}"
    user = os.environ.get("USER") or os.environ.get("LOGNAME") or str(os.getuid())
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
        print(f"[JellyToast] token encryption failed: {e}", flush=True)
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
        print(f"[JellyToast] token decryption failed: {e}", flush=True)
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
    launch where the entry is genuinely absent (post-logout / first
    run), which goes to the LoginView anyway, so it's not user-visible.
    On successful warm reads the first attempt returns immediately.

    The 5 × 100ms default keeps __init__ paths cheap (a missing-token
    boot still resolves in ~500ms). The boot auth check passes a
    much higher budget (~7.5s) explicitly so a cold KDE Wayland
    session that takes seconds to expose the secret service still
    sees the token before falling back to the login view."""
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
                    f"[JellyToast] keyring read succeeded on attempt "
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
        print(f"[JellyToast] keyring read failed: {last_error}", flush=True)
    return None


def _keyring_set_token(value: str) -> bool:
    """Write or clear the access token in the desktop secret store.
    Returns True on success, False if keyring isn't usable — in which
    case the caller should fall back to QSettings so a missing wallet
    doesn't lock the user out of the app."""
    try:
        import keyring
    except Exception:
        return False
    try:
        if value:
            keyring.set_password(_KEYRING_SERVICE, _KEYRING_USERNAME, value)
        else:
            try:
                keyring.delete_password(_KEYRING_SERVICE, _KEYRING_USERNAME)
            except Exception:
                pass  # entry already absent
        return True
    except Exception as e:
        print(f"[JellyToast] keyring write failed: {e}", flush=True)
        return False


class Settings:
    """Wrapper around QSettings with typed accessors."""

    def __init__(self):
        self._s = QSettings("JellyToast", "JellyToast")
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
    def username(self) -> str:
        return self._s.value("server/username", "", type=str)

    @username.setter
    def username(self, v: str):
        self._s.setValue("server/username", v)

    @property
    def access_token(self) -> str:
        # Dual-store read: keyring first (OS-managed, encrypted at
        # rest), QSettings second as the resilience floor. The
        # QSettings copy is itself AES-GCM encrypted with a key
        # derived from machine-id + $USER, so even a config-file
        # leak doesn't expose the credential.
        kr = _keyring_get_token()
        if kr:
            # Keep the encrypted QSettings copy in sync. Re-encrypt
            # if the stored blob is empty (existing install whose
            # plaintext copy was wiped by the prior migrate-and-remove
            # path) or legacy plaintext (transparently upgrade).
            stored = self._s.value("server/token", "", type=str)
            if (not stored) or (not stored.startswith(_ENC_PREFIX)):
                self._s.setValue("server/token", _encrypt_token(kr))
                self._chmod_config_owner_only()
            return kr
        # Keyring miss — fall back to the encrypted QSettings copy.
        stored = self._s.value("server/token", "", type=str)
        if not stored:
            return ""
        decrypted = _decrypt_token(stored)
        # Legacy plaintext upgrade: the value didn't start with our
        # version prefix, so `_decrypt_token` returned it verbatim.
        # Re-encrypt forward so the next read sees a proper blob.
        if decrypted and not stored.startswith(_ENC_PREFIX):
            self._s.setValue("server/token", _encrypt_token(decrypted))
            self._chmod_config_owner_only()
        return decrypted

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
    def gapless(self) -> bool:
        return self._s.value("playback/gapless", True, type=bool)

    @gapless.setter
    def gapless(self, v: bool):
        self._s.setValue("playback/gapless", v)

    @property
    def replaygain(self) -> str:
        # 'no' | 'track' | 'album'
        return self._s.value("playback/replaygain", "track", type=str)

    @replaygain.setter
    def replaygain(self, v: str):
        self._s.setValue("playback/replaygain", v)

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
    def autostart(self) -> bool:
        # Whether JellyToast launches on login. Backed by an XDG
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
        # Wayland-only knob: when true, JellyToast installs a KWin
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
        # "ascending" | "descending" — the JellyToast top-bar string,
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

    # ── Queue persistence ───────────────────────────────────────────────────
    def save_queue(self, queue: "Queue"):
        """Persist the full Queue (context + original items + play_order +
        current index + manual overlay). The on-disk schema is bumped to
        v2; v1 (`{queue, index}` only) is read transparently in `load_queue`.
        """
        path = self._config_dir / "queue.json"
        try:
            payload = {"version": 2, **queue.to_dict()}
            with open(path, "w") as f:
                json.dump(payload, f)
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
            with open(path) as f:
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
