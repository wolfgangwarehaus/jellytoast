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
import logging
import os
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from PySide6.QtCore import QStandardPaths

from jellytoast.credentials import (
    _ENC_PREFIX,  # noqa: F401  (re-exported for airplay2 + tests)
    _decrypt_token,
    _encrypt_token,
    _keyring_get_token,
    _keyring_set_token,
    _machine_key,  # noqa: F401  (re-exported)
    warm_keyring_async,  # noqa: F401  (re-exported for jellytoast boot)
)
from jellytoast.settings_migration import (
    _QSETTINGS_APP,  # noqa: F401  (re-exported)
    _QSETTINGS_ORG,  # noqa: F401  (re-exported)
    _migrate_legacy_org_name,
    _migrate_theme_axes,
    _recover_nested_appdata,
    open_qsettings,
)

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    # Queue lives in player_state which imports settings (transitively)
    # — TYPE_CHECKING-only import keeps the runtime cycle broken while
    # still letting type-checkers resolve the forward reference.
    from jellytoast.player_state import Queue


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
        self._s = open_qsettings()
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
        # Split the old single ui/theme_mode into the orthogonal theme axes
        # (theme_mode ∈ auto/dark/light · frosted · theme_family). Run-once,
        # marker-guarded — a no-op after the first launch on 0.1.7.
        _migrate_theme_axes(self._s)

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
        # sync() is what first CREATES the ini on a fresh install (QSettings
        # doesn't touch disk at setValue() time), and it lands with the umask
        # default (0644, world-readable). The post-authenticate / sign-out
        # flushes run right after the token write, so tightening here closes
        # the fresh-install window where the credential blob + plaintext
        # username/server_url would otherwise sit world-readable until the
        # NEXT launch re-ran the __init__ chmod. No-op on the Windows registry
        # backend (path isn't a regular file).
        self._chmod_config_owner_only()

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
    def subsonic_auth_mode_plain(self) -> bool:
        """True when the Subsonic server rejected token+salt auth and the
        account had to fall back to plain-password (``p=``) auth — the
        LDAP-backed case (Subsonic error 41). Persisted so the provider
        rebuilt on next launch keeps using plain auth instead of reverting
        to token auth (which the server keeps rejecting → login loop)."""
        return self._s.value("server/subsonic_auth_mode_plain", False, type=bool)

    @subsonic_auth_mode_plain.setter
    def subsonic_auth_mode_plain(self, v: bool):
        self._s.setValue("server/subsonic_auth_mode_plain", bool(v))

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
        for entry in v or []:
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
    def selected_library_ids(self) -> list:
        """The subset of the server's music libraries the user has chosen
        to load into jellytoast (the top-bar library dropdown). Stored as
        a JSON list of library-id strings.

        Empty list (the default) means **all libraries** — no filter is
        applied and the server returns the whole collection, exactly as
        before this feature existed. A non-empty list scopes every browse
        surface (Albums / Artists / Songs / Genres / Suggestions / Search)
        to the union of the chosen libraries.

        The value is intentionally NOT per-server-namespaced in the key:
        it is cleared on sign-out and on a server change (see
        ``jellytoast.library_selection.reset_after_server_change``) so a
        stale selection from a previous server can't leak in. Ids that no
        longer exist on the current server are filtered out by the
        selection state layer at load time, so a server that dropped a
        library degrades to 'all' rather than showing an empty grid."""
        raw = self._s.value("server/selected_library_ids", "", type=str)
        if not raw:
            return []
        try:
            v = json.loads(raw)
        except Exception:
            return []
        if not isinstance(v, list):
            return []
        # Coerce to clean, de-duplicated, order-preserving id strings.
        out: list = []
        seen: set = set()
        for entry in v:
            sid = str(entry or "").strip()
            if sid and sid not in seen:
                seen.add(sid)
                out.append(sid)
        return out

    @selected_library_ids.setter
    def selected_library_ids(self, v: list):
        out: list = []
        seen: set = set()
        for entry in v or []:
            sid = str(entry or "").strip()
            if sid and sid not in seen:
                seen.add(sid)
                out.append(sid)
        self._s.setValue("server/selected_library_ids", json.dumps(out))

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
            logger.warning(
                "dual-store divergence — keyring stale, rewriting from encrypted blob"
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

    @property
    def np_left_pane_mode(self) -> str:
        """NowPlayingPage left-pane mode — tri-state: ``"cover"`` (no
        lyrics, no visualizer; just the artwork + meta), ``"lyrics"``
        (cover + scrolling lyrics, the default that preserves prior
        behaviour), or ``"visualizer"`` (cover + spectrum-bar
        visualizer in place of the lyrics scroll).

        Persisted so the user's chosen mode survives a restart.
        Unknown values fall back to ``"lyrics"`` so a future setting
        rename can't softlock the page into an empty state."""
        v = self._s.value("ui/np_left_pane_mode", "lyrics", type=str)
        return v if v in ("cover", "lyrics", "visualizer") else "lyrics"

    @np_left_pane_mode.setter
    def np_left_pane_mode(self, v: str):
        self._s.setValue(
            "ui/np_left_pane_mode",
            v if v in ("cover", "lyrics", "visualizer") else "lyrics",
        )

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
        return self._s.value("playback/download_quality", "original", type=str)

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
        self._s.setValue(
            "playback/cast_stream_routing", v if v in ("auto", "proxy", "direct") else "auto"
        )

    # ── Cast: per-type discovery toggles + timing ─────────────────────────
    # Each protocol gate lets a user enable a cast type they own, so
    # discovery only runs for it once turned on (faster scans, less mDNS
    # noise). All four are opt-in (off by default).

    @property
    def cast_discovery_timing(self) -> str:
        """When cast device discovery runs:
        'startup'   — scan a few seconds after boot, before the user
                      opens the cast menu (results are pre-warmed).
        'on_demand' — scan only when the cast menu opens. Default;
                      avoids the boot-time mDNS chatter for users who
                      rarely cast."""
        v = self._s.value("cast/discovery_timing", "on_demand", type=str)
        return v if v in ("startup", "on_demand") else "on_demand"

    @cast_discovery_timing.setter
    def cast_discovery_timing(self, v: str):
        v = (v or "on_demand").lower()
        self._s.setValue(
            "cast/discovery_timing",
            v if v in ("startup", "on_demand") else "on_demand",
        )

    # Per-protocol cast toggles — OFF by default (opt-in). Nothing
    # discovers or scans the network for a cast type until the user
    # enables it in Settings → Casting; the cast button routes there when
    # none are on. The discovery side reads the same keys with the same
    # False default (cast_manager._common._type_enabled,
    # cast.dlna._settings._settings_enabled) — keep them in sync.
    @property
    def cast_chromecast_enabled(self) -> bool:
        return self._s.value("cast/chromecast_enabled", False, type=bool)

    @cast_chromecast_enabled.setter
    def cast_chromecast_enabled(self, v: bool):
        self._s.setValue("cast/chromecast_enabled", bool(v))

    @property
    def cast_airplay_enabled(self) -> bool:
        return self._s.value("cast/airplay_enabled", False, type=bool)

    @cast_airplay_enabled.setter
    def cast_airplay_enabled(self, v: bool):
        self._s.setValue("cast/airplay_enabled", bool(v))

    @property
    def cast_dlna_enabled(self) -> bool:
        return self._s.value("cast/dlna_enabled", False, type=bool)

    @cast_dlna_enabled.setter
    def cast_dlna_enabled(self, v: bool):
        self._s.setValue("cast/dlna_enabled", bool(v))

    @property
    def cast_sonos_enabled(self) -> bool:
        return self._s.value("cast/sonos_enabled", False, type=bool)

    @cast_sonos_enabled.setter
    def cast_sonos_enabled(self, v: bool):
        self._s.setValue("cast/sonos_enabled", bool(v))

    @property
    def any_cast_type_enabled(self) -> bool:
        """True iff at least one cast protocol is enabled. Drives the cast
        button: with nothing on, the picker would be permanently empty
        (nothing discovers), so the button opens Settings → Casting
        instead."""
        return (
            self.cast_chromecast_enabled
            or self.cast_airplay_enabled
            or self.cast_dlna_enabled
            or self.cast_sonos_enabled
        )

    @property
    def media_integration_enabled(self) -> bool:
        """Whether to register jellytoast with the OS media controls —
        MPRIS (Linux: Plasma/GNOME media widget + media keys) and SMTC
        (Windows: volume-flyout transport + hardware media keys). Default
        on; gating the service start happens at launch, so a change takes
        effect on the next start."""
        return self._s.value("playback/media_integration_enabled", True, type=bool)

    @media_integration_enabled.setter
    def media_integration_enabled(self, v: bool):
        self._s.setValue("playback/media_integration_enabled", bool(v))

    @property
    def favorite_cast_devices(self) -> list:
        """Cast devices the user has hearted in the picker — pinned to
        the top of the list, and surfaced in the cast button's right-
        click menu. Stored as a JSON array of {uuid, name, type} dicts
        so the right-click menu can label each entry without waiting on
        a live discovery scan."""
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
                # Snapcast was removed (0.1.5) — silently drop any stale
                # snapcast favorite so it stops showing as a dead entry.
                if str(entry.get("type") or "") == "snapcast":
                    continue
                out.append(
                    {
                        "uuid": str(entry["uuid"]),
                        "name": str(entry.get("name") or entry["uuid"]),
                        "type": str(entry.get("type") or ""),
                    }
                )
        return out

    @favorite_cast_devices.setter
    def favorite_cast_devices(self, v):
        from enum import Enum

        def _type_str(t) -> str:
            # ``type`` may arrive as a ``CastType`` (a ``str``-backed Enum).
            # ``str(CastType.X)`` is ``"CastType.X"`` on Python 3.11+, which
            # would corrupt the persisted value — take the underlying string
            # value so the JSON stays byte-identical to the bare lowercase
            # literal ("chromecast" / "airplay" / …).
            if isinstance(t, Enum):
                return str(t.value)
            return str(t or "")

        cleaned = []
        for entry in v or []:
            if isinstance(entry, dict) and entry.get("uuid"):
                cleaned.append(
                    {
                        "uuid": str(entry["uuid"]),
                        "name": str(entry.get("name") or entry["uuid"]),
                        "type": _type_str(entry.get("type")),
                    }
                )
        self._s.setValue("playback/favorite_cast_devices", json.dumps(cleaned))

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

    # ── Sonos cast settings (see docs/research/casting_sonos.md §8) ─────────

    @property
    def sonos_enabled(self) -> bool:
        """Master Sonos discovery toggle. Off = skip M-SEARCH at startup
        and never show Sonos zones in the cast picker. Alias of
        ``cast_sonos_enabled`` — same ``cast/sonos_enabled`` key."""
        return self.cast_sonos_enabled

    @sonos_enabled.setter
    def sonos_enabled(self, v: bool):
        self.cast_sonos_enabled = bool(v)

    @property
    def sonos_preferred_zone(self) -> str:
        """Last-used Sonos coordinator UUID. If set and still present
        on the LAN, the cast dialog pre-selects it."""
        return self._s.value("cast/sonos_preferred_zone", "", type=str) or ""

    @sonos_preferred_zone.setter
    def sonos_preferred_zone(self, v: str):
        self._s.setValue("cast/sonos_preferred_zone", str(v or ""))

    @property
    def sonos_group_with_master(self) -> bool:
        """When casting to zone B while jellytoast already streams to
        zone A, join B to A instead of fragmenting playback. Default
        False so a fresh cast lands on the explicit target only."""
        return self._s.value("cast/sonos_group_with_master", False, type=bool)

    @sonos_group_with_master.setter
    def sonos_group_with_master(self, v: bool):
        self._s.setValue("cast/sonos_group_with_master", bool(v))

    @property
    def sonos_event_port(self) -> int:
        """UPnP NOTIFY listener port for soco events. ``0`` = ephemeral.
        Setting a fixed port matters only behind a tight egress firewall."""
        v = self._s.value("cast/sonos_event_port", 0, type=int)
        try:
            return max(0, min(65535, int(v)))
        except (TypeError, ValueError):
            return 0

    @sonos_event_port.setter
    def sonos_event_port(self, v: int):
        try:
            iv = max(0, min(65535, int(v)))
        except (TypeError, ValueError):
            iv = 0
        self._s.setValue("cast/sonos_event_port", iv)

    @property
    def sonos_volume_floor(self) -> int:
        """First push to a zone bumps volume to ``max(current, floor)``
        — protects against an inherited 100% volume from the Sonos app."""
        v = self._s.value("cast/sonos_volume_floor", 15, type=int)
        try:
            return max(0, min(50, int(v)))
        except (TypeError, ValueError):
            return 15

    @sonos_volume_floor.setter
    def sonos_volume_floor(self, v: int):
        try:
            iv = max(0, min(50, int(v)))
        except (TypeError, ValueError):
            iv = 15
        self._s.setValue("cast/sonos_volume_floor", iv)

    @property
    def prefer_server_when_online(self) -> bool:
        """When a track is downloaded *and* the server is reachable,
        whether to still stream from the server instead of playing the
        local copy. Default False — the local copy is faster and uses
        no bandwidth, so prefer it whenever it exists. Offline mode and
        an unreachable server always force the local copy regardless."""
        return self._s.value("playback/prefer_server_when_online", False, type=bool)

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
        return max(self._SLEEP_FADE_MIN_MS, min(self._SLEEP_FADE_MAX_MS, int(v)))

    @sleep_fade_duration_ms.setter
    def sleep_fade_duration_ms(self, v: int):
        clamped = max(self._SLEEP_FADE_MIN_MS, min(self._SLEEP_FADE_MAX_MS, int(v)))
        self._s.setValue("playback/sleep_fade_duration_ms", clamped)

    @property
    def downloads_paused(self) -> bool:
        """Persisted queue-paused flag for the download manager. A paused
        queue stays paused across a restart — the user's intent survives
        the process. Flipped by ``jellytoast.offline.manager.pause`` /
        ``resume``; not exposed in the UI as a free-standing setting."""
        return self._s.value("downloads/paused", False, type=bool)

    @downloads_paused.setter
    def downloads_paused(self, v: bool):
        self._s.setValue("downloads/paused", bool(v))

    @property
    def downloads_wifi_only(self) -> bool:
        """Persisted "only download on Wi-Fi" gate. Survives restart so
        the user's choice doesn't reset every launch. The metered-state
        side of the gate is transient (set by a future auto-detect
        layer); this flag is the user-controlled "do I care?"."""
        return self._s.value("downloads/wifi_only", False, type=bool)

    @downloads_wifi_only.setter
    def downloads_wifi_only(self, v: bool):
        self._s.setValue("downloads/wifi_only", bool(v))

    @property
    def notify_on_download_complete(self) -> bool:
        """Show a desktop notification when the download queue drains
        with at least one job dispatched this session. Honoured by
        ``jellytoast.offline.manager._emit_drain_complete``. Default True —
        the "kicked off an artist download, walked away" case is exactly
        what notifications exist for; opting out is the rarer choice."""
        return self._s.value("downloads/notify_on_complete", True, type=bool)

    @notify_on_download_complete.setter
    def notify_on_download_complete(self, v: bool):
        self._s.setValue("downloads/notify_on_complete", bool(v))

    @property
    def notify_on_track_change(self) -> bool:
        """Post a desktop notification each time playback moves to a new
        track. Off by default — the now-playing surface (and on Windows
        the SMTC flyout) already shows the track, so a toast every song
        is opt-in. Honoured by ``jellytoast.notifications.nowplaying``."""
        return self._s.value("notifications/on_track_change", False, type=bool)

    @notify_on_track_change.setter
    def notify_on_track_change(self, v: bool):
        self._s.setValue("notifications/on_track_change", bool(v))

    @property
    def library_download_in_progress(self) -> bool:
        """Sticky flag set when the user kicks off a "Download entire
        library" walk and cleared on the drain-edge. Persists across
        app restarts so a paused mid-flight bulk download can be
        recognised on next launch — the pause button rebrands to
        "Resume library download" instead of the generic "Resume
        downloads"."""
        return self._s.value("downloads/library_download_in_progress", False, type=bool)

    @library_download_in_progress.setter
    def library_download_in_progress(self, v: bool):
        self._s.setValue("downloads/library_download_in_progress", bool(v))

    @property
    def library_download_expected_total(self) -> int:
        """Track-count total captured at the start of a "Download
        entire library" walk. Persisted so the aggregate display can
        keep showing "Downloading X of Y" with a stable Y across an
        app restart in the middle of a paused bulk download. Cleared
        on the drain-edge alongside ``library_download_in_progress``.

        Slightly stale if the user added albums to the server while
        paused, which is fine — the off-by-a-few-tracks beats a
        boot-time API recount."""
        return int(self._s.value("downloads/library_download_expected_total", 0, type=int))

    @library_download_expected_total.setter
    def library_download_expected_total(self, v: int):
        self._s.setValue("downloads/library_download_expected_total", int(v))

    @property
    def library_sync_enabled(self) -> bool:
        """Toggle the "Keep library in sync" mode. When True, the
        offline package starts a 6-hour timer that re-walks the
        provider's album list and enqueues any not-yet-downloaded
        albums. Default False — bulk download is opt-in."""
        return self._s.value("downloads/library_sync_enabled", False, type=bool)

    @library_sync_enabled.setter
    def library_sync_enabled(self, v: bool):
        self._s.setValue("downloads/library_sync_enabled", bool(v))

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

    @property
    def bit_perfect_mode(self) -> bool:
        """Master "no DSP, no resample, no attenuation" lock for the
        audio path. When True, the application-side corners of the
        bit-perfect contract (volume < 100, EQ on, Normalization on,
        Crossfade on) are force-disabled — see
        ``docs/research/bit_perfect_playback.md`` §7 (T2). PipeWire
        rate-matching is documented in ``docs/bit_perfect.md`` and
        remains the user's responsibility.

        Off by default — bit-perfect is an explicit opt-in, not the
        product's default contract."""
        return self._s.value("playback/bit_perfect_mode", False, type=bool)

    @bit_perfect_mode.setter
    def bit_perfect_mode(self, v: bool):
        self._s.setValue("playback/bit_perfect_mode", bool(v))

    @property
    def audio_exclusive(self) -> bool:
        """Exclusive-output sub-toggle of Bit-perfect mode. When True,
        mpv opens its audio output with ``audio-exclusive=yes``:
        WASAPI Exclusive on Windows, ``kAudioDevicePropertyHogMode`` on
        macOS, sink-cork-other-streams on PipeWire. Bypasses the OS
        mixer so the DAC sees the file's PCM unaltered.

        UX cost is real — other apps go silent during playback, system
        sounds die, and the open can fail on some Windows DACs (mpv
        issues #11600 / #11733). ``_make_mpv_handle`` retries without
        the flag on construction failure so the player still launches.

        Tier 3 of ``docs/research/bit_perfect_playback.md`` §7; only
        meaningful when ``bit_perfect_mode`` is also True (the toggle is
        sub-gated in the UI)."""
        return self._s.value("playback/audio_exclusive", False, type=bool)

    @audio_exclusive.setter
    def audio_exclusive(self, v: bool):
        self._s.setValue("playback/audio_exclusive", bool(v))

    @property
    def audio_output_device(self) -> str:
        """mpv ``--audio-device`` name routing playback to a specific
        output, or ``"auto"`` (the default) for mpv's own pick. Values
        come from mpv's ``audio-device-list`` enumeration (e.g.
        ``pipewire/...``, ``pulse/...``, ``alsa/hw:CARD=DAC``,
        ``wasapi/{guid}``), surfaced in Settings → Playback → Audio
        output. Raw ALSA ``alsa/`` devices bypass PipeWire/the mixer
        entirely (the audiophile direct path —
        ``docs/research/audio_output_routing.md``): exclusive by
        nature, so the crossfade sibling is suppressed and the
        visualizer's monitor tap has nothing to read while one is
        selected. Applies when mpv (re)opens its audio output — the
        next track, no interruption."""
        v = self._s.value("playback/audio_output_device", "auto", type=str)
        return v.strip() or "auto"

    @audio_output_device.setter
    def audio_output_device(self, v: str):
        self._s.setValue(
            "playback/audio_output_device", (v or "auto").strip() or "auto"
        )

    # ── Crossfade ──────────────────────────────────────────────────────────
    # Two-instance ping-pong crossfade. Gated on the ``crossfade_enabled``
    # setting, exposed via the Settings → Playback crossfade section
    # (checkbox + smart-album toggle + duration slider). See
    # `docs/research/crossfade.md`. Range bounds match the research
    # doc's slider (§5).
    _CROSSFADE_MIN_MS = 1000
    _CROSSFADE_MAX_MS = 10000

    @property
    def crossfade_enabled(self) -> bool:
        return self._s.value("playback/crossfade_enabled", False, type=bool)

    @crossfade_enabled.setter
    def crossfade_enabled(self, v: bool):
        self._s.setValue("playback/crossfade_enabled", bool(v))

    @property
    def crossfade_duration_ms(self) -> int:
        v = self._s.value("playback/crossfade_duration_ms", 4000, type=int)
        return max(self._CROSSFADE_MIN_MS, min(self._CROSSFADE_MAX_MS, int(v)))

    @crossfade_duration_ms.setter
    def crossfade_duration_ms(self, v: int):
        clamped = max(self._CROSSFADE_MIN_MS, min(self._CROSSFADE_MAX_MS, int(v)))
        self._s.setValue("playback/crossfade_duration_ms", clamped)

    @property
    def crossfade_smart_album_continuity(self) -> bool:
        """When True (default), adjacent tracks on the same album skip the
        crossfade and route through gapless instead — preserves album
        playthroughs (e.g. Dark Side of the Moon's Money → Us and Them)."""
        return self._s.value("playback/crossfade_smart_album_continuity", True, type=bool)

    @crossfade_smart_album_continuity.setter
    def crossfade_smart_album_continuity(self, v: bool):
        self._s.setValue("playback/crossfade_smart_album_continuity", bool(v))

    # ── Equalizer ──────────────────────────────────────────────────────────
    # Scaffold for the 10-band graphic EQ. See `docs/research/eq_dsp.md`
    # and `jellytoast/eq_presets.py` for the band layout. UI lands in a
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
    def eq_enabled_pre_bit_perfect(self) -> bool:
        """Snapshot of ``eq_enabled`` taken the moment the user enabled
        Bit-perfect mode (which then force-disables EQ). Restored back
        into ``eq_enabled`` when the user turns Bit-perfect off, so a
        user who lives with their curve doesn't have to remember to
        re-tick the box every time they flip the master toggle.

        Persisted (not just in-memory) so the restore survives an app
        restart while Bit-perfect is on. Defaults False — first-launch
        users have no prior state to remember."""
        return self._s.value("playback/eq_enabled_pre_bit_perfect", False, type=bool)

    @eq_enabled_pre_bit_perfect.setter
    def eq_enabled_pre_bit_perfect(self, v: bool):
        self._s.setValue("playback/eq_enabled_pre_bit_perfect", bool(v))

    @property
    def replaygain_pre_bit_perfect(self) -> str:
        """Snapshot of ``replaygain`` mode ("no" / "track" / "album")
        taken on bit-perfect enable. Restored on bit-perfect disable.
        See ``eq_enabled_pre_bit_perfect`` for the rationale."""
        return self._s.value("playback/replaygain_pre_bit_perfect", "no", type=str)

    @replaygain_pre_bit_perfect.setter
    def replaygain_pre_bit_perfect(self, v: str):
        if v not in ("no", "track", "album"):
            v = "no"
        self._s.setValue("playback/replaygain_pre_bit_perfect", v)

    @property
    def crossfade_enabled_pre_bit_perfect(self) -> bool:
        """Snapshot of ``crossfade_enabled`` taken on bit-perfect enable.
        Restored on bit-perfect disable. See ``eq_enabled_pre_bit_perfect``
        for the rationale."""
        return self._s.value("playback/crossfade_enabled_pre_bit_perfect", False, type=bool)

    @crossfade_enabled_pre_bit_perfect.setter
    def crossfade_enabled_pre_bit_perfect(self, v: bool):
        self._s.setValue("playback/crossfade_enabled_pre_bit_perfect", bool(v))

    @property
    def volume_pre_bit_perfect(self) -> int:
        """Snapshot of ``volume`` taken on bit-perfect enable. Restored
        on bit-perfect disable so a user at vol=50 doesn't return from
        bit-perfect mode to vol=100 (surprise loudness). Defaults 100
        — first-launch users with bit-perfect off have nothing to
        remember. ``-1`` sentinel means "no snapshot active"."""
        return self._s.value("playback/volume_pre_bit_perfect", -1, type=int)

    @volume_pre_bit_perfect.setter
    def volume_pre_bit_perfect(self, v: int):
        try:
            iv = int(v)
        except (TypeError, ValueError):
            iv = -1
        if iv != -1:
            iv = max(0, min(100, iv))
        self._s.setValue("playback/volume_pre_bit_perfect", iv)

    # ── Streaming-info badge visibility ────────────────────────────────────
    # Per-segment toggles for the line that sits above the transport row
    # (``Streaming · Bit Perfect · FLAC · 1411 kbps`` etc.). The prefix
    # ("Streaming" / "Local playback" / "Casting to X") is always shown —
    # it's the line's anchor. Defaults: Bit Perfect / EQ / codec / bitrate
    # ON, ReplayGain and Crossfade OFF (the average listener doesn't think
    # in DSP terms; the ones who do can opt in via Display settings).

    @property
    def streaming_info_show_bit_perfect(self) -> bool:
        return self._s.value("display/streaming_info_show_bit_perfect", True, type=bool)

    @streaming_info_show_bit_perfect.setter
    def streaming_info_show_bit_perfect(self, v: bool):
        self._s.setValue("display/streaming_info_show_bit_perfect", bool(v))

    @property
    def streaming_info_show_eq(self) -> bool:
        return self._s.value("display/streaming_info_show_eq", True, type=bool)

    @streaming_info_show_eq.setter
    def streaming_info_show_eq(self, v: bool):
        self._s.setValue("display/streaming_info_show_eq", bool(v))

    @property
    def streaming_info_show_replaygain(self) -> bool:
        return self._s.value("display/streaming_info_show_replaygain", False, type=bool)

    @streaming_info_show_replaygain.setter
    def streaming_info_show_replaygain(self, v: bool):
        self._s.setValue("display/streaming_info_show_replaygain", bool(v))

    @property
    def streaming_info_show_crossfade(self) -> bool:
        return self._s.value("display/streaming_info_show_crossfade", False, type=bool)

    @streaming_info_show_crossfade.setter
    def streaming_info_show_crossfade(self, v: bool):
        self._s.setValue("display/streaming_info_show_crossfade", bool(v))

    @property
    def streaming_info_show_codec(self) -> bool:
        return self._s.value("display/streaming_info_show_codec", True, type=bool)

    @streaming_info_show_codec.setter
    def streaming_info_show_codec(self, v: bool):
        self._s.setValue("display/streaming_info_show_codec", bool(v))

    @property
    def streaming_info_show_bitrate(self) -> bool:
        return self._s.value("display/streaming_info_show_bitrate", True, type=bool)

    @streaming_info_show_bitrate.setter
    def streaming_info_show_bitrate(self, v: bool):
        self._s.setValue("display/streaming_info_show_bitrate", bool(v))

    @property
    def audio_quality_pre_bit_perfect(self) -> str:
        """Snapshot of ``audio_quality`` taken on bit-perfect enable.
        The bit-perfect contract requires ``audio_quality=="original"``,
        so the Settings dialog greys the quality combo while the mode
        is on. We don't auto-force "original" on enable (that would
        change the URL build for the next stream — potentially a
        large FLAC over a slow connection), but if the user does pick
        "original" themselves while the gate is greyed, the OFF leg
        restores whatever they had before so they're not stuck at
        original forever. Empty string sentinel means "no snapshot"."""
        return self._s.value("playback/audio_quality_pre_bit_perfect", "", type=str)

    @audio_quality_pre_bit_perfect.setter
    def audio_quality_pre_bit_perfect(self, v: str):
        self._s.setValue("playback/audio_quality_pre_bit_perfect", (v or "").strip())

    @property
    def eq_linear_phase(self) -> bool:
        """EQ T2 — linear-phase FIR mode. When True, ``apply_eq`` uses
        ffmpeg's ``firequalizer`` (FFT-based, zero-phase) instead of
        ``anequalizer`` (IIR Butterworth). Linear phase preserves
        transient response through the EQ — audible on drums, plucked
        strings, percussive material — at the cost of ~20 ms internal
        latency and ~3× CPU (still well under one core for 48 k
        stereo). See ``docs/research/eq_dsp_v2.md`` §6 (T2).

        Off by default — bit-perfect-by-default stance argues against
        enabling additional DSP users didn't ask for; this is opt-in
        even after the user has turned the master EQ on."""
        return self._s.value("playback/eq_linear_phase", False, type=bool)

    @eq_linear_phase.setter
    def eq_linear_phase(self, v: bool):
        self._s.setValue("playback/eq_linear_phase", bool(v))

    @property
    def eq_view_advanced(self) -> bool:
        """EQ T3b — whether the Settings → Playback → Equalizer surface
        shows the parametric curve editor (True) or the 10-band slider
        strip (False, default). Persisted so the user's preferred view
        comes back across sessions.

        The data underneath is the same in both views — the toggle is a
        purely cosmetic swap of the editor widget for the slider grid."""
        return self._s.value("playback/eq_view_advanced", False, type=bool)

    @eq_view_advanced.setter
    def eq_view_advanced(self, v: bool):
        self._s.setValue("playback/eq_view_advanced", bool(v))

    @property
    def eq_autoeq_profile_json(self) -> str:
        """EQ T3a — serialised AutoEQ headphone-correction profile.

        Empty string = no profile loaded; the graphic 10-band EQ is the
        active path. Non-empty = JSON-encoded
        ``{preamp_db, name, bands, skipped}`` produced by
        ``jellytoast.eq_presets.parse_autoeq_profile`` and saved by the
        Settings → Playback → Equalizer → Import dialog. apply_eq picks
        the parametric formatter and replaces the graphic-band gains
        with the profile's bands when this is populated.

        Stored as a string (not a parsed object) because QSettings'
        JSON support is brittle and we already have the parsed-form
        helpers next door. The Settings dialog parses on read,
        serialises on write."""
        return self._s.value("playback/eq_autoeq_profile_json", "", type=str)

    @eq_autoeq_profile_json.setter
    def eq_autoeq_profile_json(self, v: str):
        self._s.setValue("playback/eq_autoeq_profile_json", str(v or ""))

    @property
    def eq_preset(self) -> str:
        """Last-selected preset name. ``Custom`` once the user drags
        any band; the UI follow-up owns that transition. Default
        ``Flat`` so the first read on a fresh install picks a valid
        entry from ``jellytoast.eq_presets.PRESETS``."""
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
        from jellytoast.eq_presets import BAND_COUNT

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
        from jellytoast.eq_presets import BAND_COUNT

        cleaned: list = []
        for entry in v or []:
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
    def eq_preamp(self) -> float:
        """Master pre-amp in dB, applied before the band filter via a
        ``volume=<dB>`` mpv filter prepended to the chain. Default 0.0;
        clamped to the documented ±12 dB envelope on read so a hand-
        edited settings.ini can't shred speakers. Pre-amp is split out
        from the band list so dragging it doesn't rebuild the band
        filter string."""
        from jellytoast.eq_presets import GAIN_LIMIT_DB

        try:
            raw = float(self._s.value("playback/eq_preamp", 0.0, type=float))
        except (TypeError, ValueError):
            return 0.0
        if raw > GAIN_LIMIT_DB:
            return GAIN_LIMIT_DB
        if raw < -GAIN_LIMIT_DB:
            return -GAIN_LIMIT_DB
        return raw

    @eq_preamp.setter
    def eq_preamp(self, v):
        from jellytoast.eq_presets import GAIN_LIMIT_DB

        try:
            x = float(v)
        except (TypeError, ValueError):
            x = 0.0
        if x > GAIN_LIMIT_DB:
            x = GAIN_LIMIT_DB
        elif x < -GAIN_LIMIT_DB:
            x = -GAIN_LIMIT_DB
        self._s.setValue("playback/eq_preamp", x)

    @property
    def eq_user_presets(self) -> dict:
        """User-saved EQ presets keyed by name. Shape:
        ``{name: {"preamp": float, "bands": [10 floats]}}``. Stored as
        a JSON string (same pattern as ``favorite_cast_devices``).
        Returns ``{}`` on missing / malformed input rather than raising,
        so a corrupted settings file degrades gracefully — the user
        loses their custom presets but the app boots and the built-in
        presets still work."""
        from jellytoast.eq_presets import BAND_COUNT

        raw = self._s.value("playback/eq_user_presets", "", type=str)
        if not raw:
            return {}
        try:
            v = json.loads(raw)
        except Exception:
            return {}
        if not isinstance(v, dict):
            return {}
        out: dict = {}
        for name, entry in v.items():
            if not isinstance(entry, dict):
                continue
            try:
                preamp = float(entry.get("preamp", 0.0))
            except (TypeError, ValueError):
                preamp = 0.0
            bands_raw = entry.get("bands") or []
            if not isinstance(bands_raw, list) or len(bands_raw) != BAND_COUNT:
                continue
            bands: list = []
            ok = True
            for b in bands_raw:
                try:
                    bands.append(float(b))
                except (TypeError, ValueError):
                    ok = False
                    break
            if not ok:
                continue
            out[str(name)] = {"preamp": preamp, "bands": bands}
        return out

    @eq_user_presets.setter
    def eq_user_presets(self, v: dict):
        from jellytoast.eq_presets import BAND_COUNT

        cleaned: dict = {}
        if isinstance(v, dict):
            for name, entry in v.items():
                if not isinstance(entry, dict):
                    continue
                try:
                    preamp = float(entry.get("preamp", 0.0))
                except (TypeError, ValueError):
                    preamp = 0.0
                bands_in = entry.get("bands") or []
                if not isinstance(bands_in, list):
                    continue
                bands_out: list = []
                for b in bands_in:
                    try:
                        bands_out.append(float(b))
                    except (TypeError, ValueError):
                        bands_out.append(0.0)
                if len(bands_out) < BAND_COUNT:
                    bands_out.extend([0.0] * (BAND_COUNT - len(bands_out)))
                elif len(bands_out) > BAND_COUNT:
                    bands_out = bands_out[:BAND_COUNT]
                cleaned[str(name)] = {"preamp": preamp, "bands": bands_out}
        self._s.setValue("playback/eq_user_presets", json.dumps(cleaned))

    # Current smart-playlist entry schema version. Bumped when the
    # *entry* shape (not the rule schema) changes; persisted on every
    # saved entry so a future migration can tell old data apart.
    SMART_PLAYLIST_SCHEMA_VERSION = 1

    @staticmethod
    def _coerce_schema_version(raw: Any) -> int:
        """Read an entry's ``schema_version``, defaulting to 1.

        Entries written before versioning landed have no
        ``schema_version`` key — those are treated as v1 (the only
        version that ever shipped). A malformed value also falls back
        to 1 rather than crashing the load."""
        if raw is None:
            return 1
        try:
            v = int(raw)
        except (TypeError, ValueError):
            return 1
        return v if v >= 1 else 1

    @property
    def smart_playlists(self) -> list:
        """User-defined smart playlists. Each entry::

            {
                "name": str,
                "rules": <dict matching jellytoast.providers.smart_rule_schema>,
                "created_at": <ISO 8601 string>,
                "schema_version": int,   # entry-shape version, >= 1
            }

        Stored as a JSON list (same pattern as ``eq_user_presets``).
        Returns ``[]`` on missing / malformed input; entries that fail
        ``smart_rule_schema.validate_rules`` are dropped so a corrupted
        settings file doesn't take the app down — the user loses the
        broken playlists but the rest still load.

        ``schema_version`` is a defensive future-proof: pre-versioning
        entries (no key) load cleanly as v1 via ``_coerce_schema_version``."""
        from jellytoast.providers.smart_rule_schema import validate_rules

        raw = self._s.value("library/smart_playlists", "", type=str)
        if not raw:
            return []
        try:
            v = json.loads(raw)
        except Exception:
            return []
        if not isinstance(v, list):
            return []
        out: list = []
        for entry in v:
            if not isinstance(entry, dict):
                continue
            name = entry.get("name")
            rules = entry.get("rules")
            if not isinstance(name, str) or not name.strip():
                continue
            if validate_rules(rules):
                continue
            out.append(
                {
                    "name": name.strip(),
                    "rules": rules,
                    "created_at": str(entry.get("created_at") or ""),
                    "schema_version": self._coerce_schema_version(
                        entry.get("schema_version")
                    ),
                }
            )
        return out

    @smart_playlists.setter
    def smart_playlists(self, v: list) -> None:
        from jellytoast.providers.smart_rule_schema import validate_rules

        cleaned: list = []
        if isinstance(v, list):
            for entry in v:
                if not isinstance(entry, dict):
                    continue
                name = entry.get("name")
                rules = entry.get("rules")
                if not isinstance(name, str) or not name.strip():
                    continue
                if validate_rules(rules):
                    continue
                # Preserve an explicit version if the caller supplied
                # one; otherwise stamp the current entry-schema version.
                version = entry.get("schema_version")
                if version is None:
                    version = self.SMART_PLAYLIST_SCHEMA_VERSION
                else:
                    version = self._coerce_schema_version(version)
                cleaned.append(
                    {
                        "name": name.strip(),
                        "rules": rules,
                        "created_at": str(entry.get("created_at") or ""),
                        "schema_version": version,
                    }
                )
        self._s.setValue("library/smart_playlists", json.dumps(cleaned))

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
                "scrobble/listenbrainz_token",
                _encrypt_token(decrypted),
            )
        return decrypted

    @listenbrainz_token.setter
    def listenbrainz_token(self, v: str):
        self._s.setValue(
            "scrobble/listenbrainz_token",
            _encrypt_token(v or ""),
        )

    @property
    def listenbrainz_url(self) -> str:
        """Base URL for the ListenBrainz API. Defaults to the canonical
        instance; users on a Maloja or self-hosted ListenBrainz point
        this elsewhere — same knob Navidrome's own scrobbler exposes."""
        return self._s.value(
            "scrobble/listenbrainz_url",
            "https://api.listenbrainz.org",
            type=str,
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
                "scrobble/lastfm_session_key",
                _encrypt_token(decrypted),
            )
        return decrypted

    @lastfm_session_key.setter
    def lastfm_session_key(self, v: str):
        self._s.setValue(
            "scrobble/lastfm_session_key",
            _encrypt_token(v or ""),
        )

    @property
    def lastfm_username(self) -> str:
        """Display-only username returned by auth.getSession."""
        return self._s.value("scrobble/lastfm_username", "", type=str)

    @lastfm_username.setter
    def lastfm_username(self, v: str):
        self._s.setValue("scrobble/lastfm_username", v or "")

    # Server-side scrobbling detection (set by
    # jellytoast.scrobble.refresh_server_scrobble_flags — LB submission_client
    # inspection of recent listens). The Settings → Scrobbling
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
        return self._s.value("scrobble/server_scrobbles_lastfm", False, type=bool)

    @server_scrobbles_lastfm.setter
    def server_scrobbles_lastfm(self, v: bool):
        self._s.setValue("scrobble/server_scrobbles_lastfm", bool(v))

    @property
    def server_scrobbles_listenbrainz(self) -> bool:
        return self._s.value("scrobble/server_scrobbles_listenbrainz", False, type=bool)

    @server_scrobbles_listenbrainz.setter
    def server_scrobbles_listenbrainz(self, v: bool):
        self._s.setValue("scrobble/server_scrobbles_listenbrainz", bool(v))

    @property
    def server_scrobble_check_done(self) -> bool:
        """True once we've successfully read the Navidrome user record
        at least once. The settings UI uses this to distinguish "we
        couldn't tell" (banner says: leave off if you've enabled it
        there) from "we know" (banner says: server is scrobbling for
        you, in-app off)."""
        return self._s.value("scrobble/server_scrobble_check_done", False, type=bool)

    @server_scrobble_check_done.setter
    def server_scrobble_check_done(self, v: bool):
        self._s.setValue("scrobble/server_scrobble_check_done", bool(v))

    @property
    def scrobble_in_app_anyway(self) -> bool:
        """Override: scrobble to ListenBrainz from jellytoast even when a
        second scrobbler (the server, detected via the LB submission_client
        of recent listens) is covering this account. Default False = defer to
        the server to avoid duplicates. The user flips this on if the
        detected 'other' scrobbler is actually a different app, not their
        server, and they want jellytoast to scrobble too."""
        return self._s.value("scrobble/in_app_anyway", False, type=bool)

    @scrobble_in_app_anyway.setter
    def scrobble_in_app_anyway(self, v: bool):
        self._s.setValue("scrobble/in_app_anyway", bool(v))

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
        # jellytoast.autostart for the actual filesystem state. This
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
    def native_window_border(self) -> bool:
        # When True the main window keeps KDE's native server-side
        # decoration (titlebar + border). When False (default) jellytoast
        # installs a KWin `noborder` rule and draws its own blended top
        # bar — the borderless / all-frosted look. KDE Wayland only;
        # a no-op elsewhere. Read at startup; takes effect next launch.
        return self._s.value("ui/native_window_border", False, type=bool)

    @native_window_border.setter
    def native_window_border(self, v: bool):
        self._s.setValue("ui/native_window_border", v)

    @property
    def square_corners(self) -> bool:
        # When True every rounded corner in the UI — windows, album art,
        # tiles, dialogs, players, buttons, popups — is squared off; genuinely
        # circular controls (round icon buttons, the slider handle) stay round.
        # Baked into design_tokens at module import, so it takes effect on the
        # next launch (the Display page shows the restart notice).
        return self._s.value("ui/square_corners", False, type=bool)

    @square_corners.setter
    def square_corners(self, v: bool):
        self._s.setValue("ui/square_corners", v)

    # NB: no ``opaque_mode`` setting. The "Opaque background" toggle was removed
    # — a frosted theme that can't get blur already falls back to a near-opaque
    # body automatically, and the toggle broke the window's rounded corners by
    # dropping translucency. The JT_OPAQUE=1 env switch remains as a dev-only
    # diagnostic (see jellytoast.blur.opaque_mode_active).

    @property
    def theme_mode(self) -> str:
        """Luminance intent — ``"auto"`` (follow the OS light/dark), ``"dark"``,
        or ``"light"``. Orthogonal to :attr:`frosted` (Frosted/Opaque) and
        :attr:`theme_family`; ``get_active_theme()`` composes the built-in theme
        from all three. The old single-key 4-name scheme (frosted_dark / dark /
        frosted_light / light / transparent*) is split into these axes at boot by
        ``settings_migration._migrate_theme_axes``; this getter self-heals any
        stray 4-name to its luminance as a belt-and-braces fallback."""
        v = self._s.value("ui/theme_mode", "dark", type=str)
        if v not in ("auto", "dark", "light"):
            v = "light" if "light" in v else "dark"
            self._s.setValue("ui/theme_mode", v)
        return v

    @theme_mode.setter
    def theme_mode(self, v: str):
        self._s.setValue("ui/theme_mode", v if v in ("auto", "dark", "light") else "dark")

    @property
    def frosted(self) -> bool:
        """Frosted glass (translucent, blur-riding) vs Opaque body — the
        Frosted/Opaque switch, orthogonal to :attr:`theme_mode` +
        :attr:`theme_family`. Default on (the frosted aesthetic ships)."""
        return self._s.value("ui/frosted", True, type=bool)

    @frosted.setter
    def frosted(self, v: bool):
        self._s.setValue("ui/frosted", bool(v))

    @property
    def preset_glass_alpha(self) -> int:
        """User override for a PRESET family's frosted body alpha (0 = use the
        built-in per-luminance default). Higher = deeper, truer to the scheme's
        background; lower = airier. Surfaced as the "Glass opacity" slider that
        appears when Frosted is on for a preset. Ignored for the jellytoast
        family (its glass is fixed) and env JT_PRESET_GLASS_ALPHA."""
        return self._s.value("ui/preset_glass_alpha", 0, type=int)

    @preset_glass_alpha.setter
    def preset_glass_alpha(self, v: int):
        try:
            self._s.setValue("ui/preset_glass_alpha", int(v))
        except (TypeError, ValueError):
            self._s.setValue("ui/preset_glass_alpha", 0)

    @property
    def jellytoast_glass_alpha(self) -> int:
        """User override for the built-in jellytoast theme's frosted body alpha
        (0 = its own airier default, 172 dark / 140 light). Same "Glass opacity"
        slider as presets, but tracked separately so each family keeps its own
        default + tuning. Ignored on the Opaque (solid) variant."""
        return self._s.value("ui/jellytoast_glass_alpha", 0, type=int)

    @jellytoast_glass_alpha.setter
    def jellytoast_glass_alpha(self, v: int):
        try:
            self._s.setValue("ui/jellytoast_glass_alpha", int(v))
        except (TypeError, ValueError):
            self._s.setValue("ui/jellytoast_glass_alpha", 0)

    @property
    def theme_family(self) -> str:
        """Active theme family — ``""`` / ``"jellytoast"`` (built-in), a preset
        family key (``theme_presets.THEME_FAMILIES``), or ``"imported"``. Drives
        which palette applies and whether the Display page shows the accent row
        (jellytoast) or the base16 preview (presets)."""
        return self._s.value("ui/theme_family", "", type=str)

    @theme_family.setter
    def theme_family(self, v: str):
        self._s.setValue("ui/theme_family", (v or "").strip())

    @property
    def imported_scheme_json(self) -> str:
        """Raw JSON of the last user-imported base16 scheme (name / variant /
        accent_slot / base16), so its preview grid + body tint survive a restart.
        Empty when nothing has been imported."""
        return self._s.value("ui/imported_scheme_json", "", type=str)

    @imported_scheme_json.setter
    def imported_scheme_json(self, v: str):
        self._s.setValue("ui/imported_scheme_json", v or "")

    # ── Update check (jellytoast/updates.py) ──────────────────────────
    @property
    def check_for_updates_enabled(self) -> bool:
        """Whether the in-app update check runs. Default on; it only ever
        nags MANUAL install channels (the auto-updating Store / Mac App
        Store / AUR builds suppress it regardless). See updates.should_check."""
        return self._s.value("updates/check_enabled", True, type=bool)

    @check_for_updates_enabled.setter
    def check_for_updates_enabled(self, v: bool):
        self._s.setValue("updates/check_enabled", bool(v))

    @property
    def update_last_check_time(self) -> int:
        """Unix timestamp of the last update check (0 = never). Throttles the
        check to once per day."""
        return self._s.value("updates/last_check_time", 0, type=int)

    @update_last_check_time.setter
    def update_last_check_time(self, v: int):
        self._s.setValue("updates/last_check_time", int(v))

    @property
    def update_dismissed_version(self) -> str:
        """The release version the user dismissed (e.g. "0.1.6"), so the update
        chip doesn't re-nag for it. An even newer release clears the nag."""
        return self._s.value("updates/dismissed_version", "", type=str)

    @update_dismissed_version.setter
    def update_dismissed_version(self, v: str):
        self._s.setValue("updates/dismissed_version", str(v))

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
            "#a78bfa": "#967de1",  # Purple
            "#00a4dc": "#0093c6",  # Blue
            "#22c5be": "#1eb1ab",  # Teal
            "#34d399": "#2fbe8a",  # Green
            "#f472b6": "#dc66a4",  # Pink
            "#fb923c": "#e28336",  # Orange
            "#ef4444": "#d73d3d",  # Red
        }
        if v in _LEGACY_TO_SUBDUED:
            v = _LEGACY_TO_SUBDUED[v]
            self._s.setValue("ui/accent_color", v)
        return v

    @accent_color.setter
    def accent_color(self, v: str):
        self._s.setValue("ui/accent_color", (v or "").strip())

    @property
    def last_preset_name(self) -> str:
        """Name of the last-applied curated color preset (theme_presets), so the
        Settings picker can show which swatch is selected. Empty = none / the
        colors were changed by hand or a different path."""
        return self._s.value("ui/last_preset_name", "", type=str)

    @last_preset_name.setter
    def last_preset_name(self, v: str):
        self._s.setValue("ui/last_preset_name", (v or "").strip())

    @property
    def follow_system_accent(self) -> bool:
        """When on, jellytoast adopts the desktop's accent colour (read from the
        XDG portal — see system_accent) on enable + at launch. Off by default."""
        return self._s.value("ui/follow_system_accent", False, type=bool)

    @follow_system_accent.setter
    def follow_system_accent(self, v: bool):
        self._s.setValue("ui/follow_system_accent", bool(v))

    @property
    def follow_pywal(self) -> bool:
        """When on, jellytoast follows the pywal / wallust palette: the theme
        watcher applies ``~/.cache/wal/colors.json`` at launch and live on every
        wallpaper change (see theme_watcher). Off by default."""
        return self._s.value("ui/follow_pywal", False, type=bool)

    @follow_pywal.setter
    def follow_pywal(self, v: bool):
        self._s.setValue("ui/follow_pywal", bool(v))

    @property
    def imported_scheme_path(self) -> str:
        """Source file of the active imported scheme when it came from the
        watched themes folder — the folder watcher re-applies it live when the
        file changes. Empty = a one-off import (or pywal), nothing to follow."""
        return self._s.value("ui/imported_scheme_path", "", type=str)

    @imported_scheme_path.setter
    def imported_scheme_path(self, v: str):
        self._s.setValue("ui/imported_scheme_path", v or "")

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
    def radio_stations(self) -> list:
        """Local internet-radio stations for the Jellyfin provider —
        Jellyfin has no native CRUD endpoint, so the JellyfinProvider
        backs its four ``*_internet_radio_station`` methods with this
        list. Stored as a JSON array of dicts
        ``{id, name, streamUrl, homePageUrl}`` — same key shape Subsonic
        returns from ``getInternetRadioStations`` so the UI never has
        to switch on provider kind. Empty list (default) means no local
        stations. Resilient against hand-edited configs: malformed JSON
        or rows missing the required keys yield an empty list with a
        warning rather than raising."""
        raw = self._s.value("radio/stations", "", type=str)
        if not raw:
            return []
        try:
            v = json.loads(raw)
        except Exception:
            logger.warning("radio/stations: malformed JSON, returning empty list")
            return []
        if not isinstance(v, list):
            logger.warning("radio/stations: not a list, returning empty list")
            return []
        out = []
        for entry in v:
            if (
                isinstance(entry, dict)
                and "id" in entry
                and "name" in entry
                and "streamUrl" in entry
            ):
                out.append(
                    {
                        "id": str(entry["id"]),
                        "name": str(entry["name"]),
                        "streamUrl": str(entry["streamUrl"]),
                        "homePageUrl": str(entry.get("homePageUrl") or ""),
                    }
                )
            else:
                logger.warning(
                    "radio/stations: dropping malformed entry "
                    "(missing id/name/streamUrl)"
                )
        return out

    @radio_stations.setter
    def radio_stations(self, v: list):
        cleaned = []
        for entry in v or []:
            if not isinstance(entry, dict):
                continue
            if "id" not in entry or "name" not in entry or "streamUrl" not in entry:
                continue
            cleaned.append(
                {
                    "id": str(entry["id"]),
                    "name": str(entry["name"]),
                    "streamUrl": str(entry["streamUrl"]),
                    "homePageUrl": str(entry.get("homePageUrl") or ""),
                }
            )
        self._s.setValue("radio/stations", json.dumps(cleaned))

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
        # `jellytoast.design_tokens`; restart required to take effect
        # because the tokens are baked into class-level constants and
        # then splattered into QSS strings across every widget at
        # construction.
        return self._s.value("ui/font_scale", "default", type=str)

    @font_scale.setter
    def font_scale(self, v: str):
        self._s.setValue("ui/font_scale", v)

    @property
    def font_family(self) -> str:
        # User-chosen UI text font family. "" means the built-in Inter stack.
        # Applied app-wide via the global QSS font-family rule + app.setFont;
        # icons are SVG so they're never affected. LIVE-applied (no restart) via
        # ui_helpers.apply_font_settings_live, and also read at boot.
        return self._s.value("ui/font_family", "", type=str)

    @font_family.setter
    def font_family(self, v: str):
        self._s.setValue("ui/font_family", (v or "").strip())

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
        except Exception as e:
            # A silent miss here means the user's queue quietly fails to
            # restore next launch with nothing in the log to explain it.
            logger.warning("queue persistence failed: %s", e)
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
        from jellytoast.player_state import Queue, QueueContext, QueueKind

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


# Module-level singleton. Constructed under a lock: get_settings is called
# from pool workers (offline.library_sync et al.), and an unlocked
# check-then-create racing the GUI thread would build two Settings stores
# (double-checked pattern, same as offline/db.py).
_settings: Optional[Settings] = None
_settings_lock = threading.Lock()


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        with _settings_lock:
            if _settings is None:
                _settings = Settings()
    return _settings
