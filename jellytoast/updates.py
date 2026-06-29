"""In-app update check — tells a user on a MANUAL install channel when a newer
release is out, and stays silent on auto-updating ones.

Once per day (throttled via a stored timestamp), on a background HTTP GET through
``async_io.get_qnam()`` (never a raw thread), the app asks GitHub for the latest
*published* release. If it's newer than ``version.__version__`` — and the user
hasn't dismissed that exact version — it fires ``PlayerBus.update_available`` so
the top-bar chip (``update_banner.py``) can offer Download + What's-new.

Channel-aware (the load-bearing rule): we only nag where the user updates BY HAND
(.dmg / .deb / AppImage / installer / portable / source). On the auto-updating
channels — Microsoft Store (MSIX), Mac App Store, AUR — the package manager owns
updates, so pointing the user at a manual installer would be wrong; those are
suppressed. Store / MAS are detected at runtime; the rest read the build stamp
(``_channel.CHANNEL``, default ``"source"`` → checks ON, the safe direction).

The check hits GitHub's public, unauthenticated API (no account, no PII) and can
be turned off in Settings.
"""

from __future__ import annotations

import json
import logging
import time

from PySide6.QtCore import QUrl
from PySide6.QtNetwork import QNetworkReply, QNetworkRequest

from jellytoast._channel import CHANNEL
from jellytoast.async_io import get_qnam
from jellytoast.platform_compat import is_macos_sandboxed, is_msix_packaged
from jellytoast.version import __version__

logger = logging.getLogger(__name__)

_RELEASES_API = (
    "https://api.github.com/repos/wolfgangwarehaus/jellytoast/releases/latest"
)
_RELEASES_PAGE = "https://github.com/wolfgangwarehaus/jellytoast/releases/latest"
_CHECK_INTERVAL_S = 24 * 60 * 60  # once per day

# Channels whose updates are handled by a store / package manager — never nag
# them (an in-app "download the installer" would install a conflicting copy).
_AUTO_CHANNELS = frozenset({"msix", "mas", "aur"})


def get_channel() -> str:
    """How this copy was installed. Runtime probes (Store / Mac App Store) win
    over the build stamp, since they're unambiguous; everything else falls back
    to ``_channel.CHANNEL`` (default ``"source"``). Never raises."""
    try:
        if is_msix_packaged():
            return "msix"
        if is_macos_sandboxed():
            return "mas"
    except Exception:
        pass
    return CHANNEL or "source"


def is_auto_update_channel() -> bool:
    """True on channels that update themselves (Store / MAS / AUR) — where the
    in-app update nag should stay silent."""
    return get_channel() in _AUTO_CHANNELS


def _version_tuple(v: str) -> tuple[int, ...]:
    """Parse a ``"0.1.5"`` / ``"v0.1.5"`` version into a comparable int tuple.
    Best-effort: a non-numeric chunk contributes its leading digits (or 0)."""
    parts = []
    for chunk in str(v).lstrip("vV").split("."):
        digits = ""
        for c in chunk:
            if c.isdigit():
                digits += c
            else:
                break
        parts.append(int(digits) if digits else 0)
    return tuple(parts) or (0,)


def is_newer(candidate: str, current: str = __version__) -> bool:
    """True iff ``candidate`` is a strictly newer version than ``current``."""
    return _version_tuple(candidate) > _version_tuple(current)


def should_check() -> bool:
    """Whether to run the check at all: the user setting is on AND this isn't an
    auto-updating channel."""
    from jellytoast.settings import get_settings

    try:
        if not get_settings().check_for_updates_enabled:
            return False
    except Exception:
        pass
    return not is_auto_update_channel()


def _pick_download_url(assets: list, channel: str, html_url: str) -> str:
    """Deep-link the release asset matching this OS/arch/channel; fall back to
    the releases page when there's no clean match (source / pip / unknown asset
    name) so the user can still pick."""
    import platform

    machine = platform.machine().lower()
    needle = None
    if channel == "dmg":
        needle = "arm64.dmg" if machine in ("arm64", "aarch64") else "x86_64.dmg"
    elif channel == "deb":
        needle = ".deb"
    elif channel == "appimage":
        needle = ".appimage"
    elif channel == "portable":
        needle = "portable"
    elif channel == "inno":
        needle = "setup.exe"
    if needle:
        for a in assets:
            name = (a.get("name") or "").lower()
            url = a.get("browser_download_url")
            if url and needle in name:
                return url
    return html_url


def maybe_check(force: bool = False) -> None:
    """Run the update check if due (gated by channel + the user setting, and
    throttled to once per day). ``force=True`` skips the gate/throttle for a
    manual "Check now". Fires ``PlayerBus.update_available(version, download_url,
    notes_url)`` on success. Background + best-effort; never raises.

    Must be called after the QApplication exists (uses the shared QNAM)."""
    from jellytoast.settings import get_settings

    # The channel gate ALWAYS applies — a self-updating build (Store / MAS / AUR)
    # is never checked, even on a manual "Check now".
    if is_auto_update_channel():
        return
    s = get_settings()
    if not force:
        # Automatic check: also respect the user toggle + the daily throttle.
        if not s.check_for_updates_enabled:
            return
        if (int(time.time()) - s.update_last_check_time) < _CHECK_INTERVAL_S:
            return
    s.update_last_check_time = int(time.time())
    try:
        req = QNetworkRequest(QUrl(_RELEASES_API))
        # GitHub's API rejects requests with no User-Agent (HTTP 403).
        req.setRawHeader(b"User-Agent", f"jellytoast/{__version__}".encode())
        req.setRawHeader(b"Accept", b"application/vnd.github+json")
        req.setTransferTimeout(10000)
        reply = get_qnam().get(req)
        reply.finished.connect(lambda r=reply: _on_finished(r, force))
    except Exception as e:
        logger.debug("update check failed to start: %s", e)


def _on_finished(reply: QNetworkReply, force: bool) -> None:
    """Parse the releases/latest response on the GUI thread; emit
    ``update_available`` if there's a newer, non-dismissed release."""
    try:
        if reply.error() != QNetworkReply.NetworkError.NoError:
            logger.debug("update check: %s", reply.errorString())
            return
        data = json.loads(bytes(reply.readAll()).decode("utf-8", "replace"))
        tag = (data.get("tag_name") or "").lstrip("vV").strip()
        if not tag or not is_newer(tag):
            return  # missing / up to date
        from jellytoast.settings import get_settings

        # A user "Check now" should surface even a previously-dismissed version.
        if not force and tag == get_settings().update_dismissed_version:
            return
        html_url = data.get("html_url") or _RELEASES_PAGE
        download_url = _pick_download_url(data.get("assets") or [], get_channel(), html_url)

        from jellytoast.player_state import PlayerBus

        PlayerBus.get().update_available.emit(tag, download_url, html_url)
        logger.info("update available: %s (running %s)", tag, __version__)
    except Exception as e:
        logger.debug("update check parse failed: %s", e)
    finally:
        try:
            reply.deleteLater()
        except Exception:
            pass
