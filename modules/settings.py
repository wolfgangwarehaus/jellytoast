"""
Persistent settings: server, credentials, volume, queue state, preferences.
Uses QSettings (XDG-compliant on Linux: ~/.config/JellyToast/JellyToast.conf)
for everything *except* the auth token, which lives in the desktop's
secure secret store (KDE Wallet / GNOME Keyring / SecretService) via
python-keyring. Falls back to QSettings on systems without a working
keyring backend so the app still launches.
"""

import json
from typing import Optional, List, Dict, Any
from PySide6.QtCore import QSettings, QStandardPaths
from pathlib import Path


# python-keyring identifies entries by (service, username). One token per
# install, so a fixed username is fine.
_KEYRING_SERVICE = "JellyToast"
_KEYRING_USERNAME = "access_token"


def _keyring_get_token() -> Optional[str]:
    """Read the access token from the desktop secret store. Returns None
    if keyring isn't installed, no backend is available, or the entry
    doesn't exist yet."""
    try:
        import keyring  # lazy: avoids a hard dependency at import time
    except Exception:
        return None
    try:
        return keyring.get_password(_KEYRING_SERVICE, _KEYRING_USERNAME)
    except Exception as e:
        print(f"[JellyToast] keyring read failed: {e}", flush=True)
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

    # ── Server / credentials ────────────────────────────────────────────────
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
        # Preferred path: SecretService / KWallet / GNOME Keyring via
        # python-keyring. Legacy plaintext fallback covers installs that
        # predate this migration plus environments without a working
        # keyring backend.
        kr = _keyring_get_token()
        if kr:
            return kr
        legacy = self._s.value("server/token", "", type=str)
        if legacy:
            # Migrate forward: if the keyring works now, move the token
            # there and purge the plaintext copy from QSettings so it
            # doesn't sit on disk indefinitely. If keyring still isn't
            # usable, leave the plaintext value alone — the app still
            # works, just less securely, and the next launch retries.
            if _keyring_set_token(legacy):
                self._s.remove("server/token")
            return legacy
        return ""

    @access_token.setter
    def access_token(self, v: str):
        if _keyring_set_token(v):
            # Belt-and-suspenders: always purge the legacy plaintext copy
            # so a half-migrated install can't shadow the keyring entry.
            self._s.remove("server/token")
            return
        # Keyring unusable (no backend, denied prompt, …) — degrade to
        # the QSettings path so the app stays functional. Failure was
        # already logged by `_keyring_set_token`.
        self._s.setValue("server/token", v)

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
    def start_destination(self) -> str:
        # Where to land on launch: "home" | "music" | "movies" | "tvshows"
        return self._s.value("ui/start_destination", "music", type=str)

    @start_destination.setter
    def start_destination(self, v: str):
        self._s.setValue("ui/start_destination", v)

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
