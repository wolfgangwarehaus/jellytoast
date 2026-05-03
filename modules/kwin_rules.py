"""
KWin window-rule helper. Lets us inject a "keep above" rule into KDE's
~/.config/kwinrulesrc and reload KWin so it takes effect immediately.

Why this exists: on Wayland, Qt.WindowStaysOnTopHint is a no-op for
regular xdg-shell windows — the protocol forbids apps from controlling
their own stacking. KDE's recommended workaround for "always on top" is
either (a) a layer-shell-qt overlay surface (no Python bindings yet) or
(b) a KWin window rule applied compositor-side. We do (b).

Scope of the rule: matches our app_id (wmclass = "jellytoast", set via
setDesktopFileName) plus the mini-player's specific window title so it
doesn't catch the main window.

The rule UUID is persisted in QSettings so toggling off cleanly removes
the same rule we wrote, even across runs.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import uuid as _uuid

from PySide6.QtCore import QSettings


# Title set on the FloatingMiniPlayer so this rule can scope-match it.
# Anyone editing one must update the other.
MINI_PLAYER_WINDOW_TITLE = "JellyToast Mini Player"

_DESCRIPTION = "JellyToast — keep mini player above (managed)"
_APP_ID = "jellytoast"
_SETTINGS_KEY = "kwin/mini_player_rule_uuid"


def is_kde_wayland() -> bool:
    """True iff the user is on KDE Plasma running on Wayland.
    Outside that combo, our rule is meaningless (X11 honours the Qt
    flag; non-KDE compositors don't read kwinrulesrc)."""
    if not os.environ.get("WAYLAND_DISPLAY"):
        return False
    desktop = os.environ.get("XDG_CURRENT_DESKTOP", "").upper()
    return "KDE" in desktop


def is_supported() -> bool:
    """KDE Wayland *and* the kwriteconfig/kreadconfig/qdbus tools are on
    PATH. Returns False on any non-KDE / X11 / minimal install."""
    return is_kde_wayland() and bool(
        _kwriteconfig_bin() and _kreadconfig_bin() and _qdbus_bin()
    )


def install_mini_player_rule() -> bool:
    """Idempotently install (or refresh) the keep-above rule.
    Returns True on success, False if the environment isn't supported."""
    if not is_supported():
        return False

    qs = QSettings("JellyToast", "JellyToast")
    rule_uuid = qs.value(_SETTINGS_KEY, "", type=str)
    if not rule_uuid:
        rule_uuid = str(_uuid.uuid4())
        qs.setValue(_SETTINGS_KEY, rule_uuid)

    _ensure_in_rules_list(rule_uuid)
    _write_rule_body(rule_uuid)
    _reconfigure_kwin()
    return True


def remove_mini_player_rule() -> bool:
    """Idempotently remove our rule from kwinrulesrc. Returns True if a
    rule was present and removed, False if there was nothing to do."""
    if not is_supported():
        return False

    qs = QSettings("JellyToast", "JellyToast")
    rule_uuid = qs.value(_SETTINGS_KEY, "", type=str)
    if not rule_uuid:
        return False

    _remove_from_rules_list(rule_uuid)
    _delete_rule_group(rule_uuid)
    qs.remove(_SETTINGS_KEY)
    _reconfigure_kwin()
    return True


# ── internals ─────────────────────────────────────────────────────────

def _kwriteconfig_bin() -> str | None:
    for cand in ("kwriteconfig6", "kwriteconfig5"):
        path = shutil.which(cand)
        if path:
            return path
    return None


def _kreadconfig_bin() -> str | None:
    for cand in ("kreadconfig6", "kreadconfig5"):
        path = shutil.which(cand)
        if path:
            return path
    return None


def _qdbus_bin() -> str | None:
    for cand in ("qdbus6", "qdbus-qt6", "qdbus"):
        path = shutil.which(cand)
        if path:
            return path
    return None


def _kreadconfig(group: str, key: str, default: str = "") -> str:
    bin_ = _kreadconfig_bin()
    if not bin_:
        return default
    try:
        out = subprocess.run(
            [bin_, "--file", "kwinrulesrc", "--group", group,
             "--key", key, "--default", default],
            check=False, capture_output=True, text=True, timeout=5,
        )
        return (out.stdout or "").strip()
    except Exception:
        return default


def _kwriteconfig(group: str, key: str, value: str) -> None:
    bin_ = _kwriteconfig_bin()
    if not bin_:
        return
    try:
        subprocess.run(
            [bin_, "--file", "kwinrulesrc", "--group", group,
             "--key", key, value],
            check=False, capture_output=True, timeout=5,
        )
    except Exception:
        pass


def _kdeleteconfig_key(group: str, key: str) -> None:
    bin_ = _kwriteconfig_bin()
    if not bin_:
        return
    try:
        subprocess.run(
            [bin_, "--file", "kwinrulesrc", "--group", group,
             "--key", key, "--delete"],
            check=False, capture_output=True, timeout=5,
        )
    except Exception:
        pass


def _ensure_in_rules_list(rule_uuid: str) -> None:
    """Append our UUID to [General].rules and bump count, unless already
    present. Both keys are kept in sync — KWin reads `rules` for the
    ordered list and `count` for length validation."""
    raw = _kreadconfig("General", "rules", "")
    rules = [r for r in raw.split(",") if r]
    if rule_uuid in rules:
        return
    rules.append(rule_uuid)
    _kwriteconfig("General", "rules", ",".join(rules))
    _kwriteconfig("General", "count", str(len(rules)))


def _remove_from_rules_list(rule_uuid: str) -> None:
    raw = _kreadconfig("General", "rules", "")
    rules = [r for r in raw.split(",") if r and r != rule_uuid]
    _kwriteconfig("General", "rules", ",".join(rules))
    _kwriteconfig("General", "count", str(len(rules)))


def _write_rule_body(rule_uuid: str) -> None:
    """Scope: wmclass contains 'jellytoast' AND title contains
    'JellyToast Mini Player'. Loose matchers (substring + complete=true)
    so we tolerate any X11/Wayland WM_CLASS quirks Qt or KWin might add.

    Action: above=true with aboverule=5 (Force) — KWin reapplies the
    value continuously and prevents user override. Apply (2) wasn't
    sticking on Plasma 6 in our testing; Force is the next-strongest
    SetRule value and is what System Settings → Window Rules generates
    when you pick "Force" for an attribute."""
    fields = {
        "Description":        _DESCRIPTION,
        "clientmachine":      "localhost",
        "clientmachinematch": "0",
        "wmclass":            _APP_ID,
        "wmclassmatch":       "2",   # 2 = substring
        "wmclasscomplete":    "true",
        "title":              MINI_PLAYER_WINDOW_TITLE,
        "titlematch":         "2",   # 2 = substring
        "above":              "true",
        "aboverule":          "5",   # 5 = Force
    }
    for key, val in fields.items():
        _kwriteconfig(rule_uuid, key, val)


def diagnose() -> dict:
    """Returns the runtime values KWin would match against, plus the
    rule fields we'd write. Handy for figuring out why a rule isn't
    sticking — call from a debug toggle and log the dict."""
    return {
        "is_kde_wayland":  is_kde_wayland(),
        "is_supported":    is_supported(),
        "kwriteconfig":    _kwriteconfig_bin(),
        "kreadconfig":     _kreadconfig_bin(),
        "qdbus":           _qdbus_bin(),
        "rule_app_id":     _APP_ID,
        "rule_title":      MINI_PLAYER_WINDOW_TITLE,
    }


def _delete_rule_group(rule_uuid: str) -> None:
    # kwriteconfig has no "delete group" — wipe each key we wrote.
    for key in (
        "Description", "clientmachine", "clientmachinematch",
        "wmclass", "wmclassmatch", "wmclasscomplete",
        "title", "titlematch",
        "above", "aboverule",
    ):
        _kdeleteconfig_key(rule_uuid, key)


def _reconfigure_kwin() -> None:
    """Tell KWin to reread kwinrulesrc. Cheap, no shell restart needed.
    Memory note: never kquitapp / killall plasmashell — reconfigure is
    the only refresh we ever issue."""
    bin_ = _qdbus_bin()
    if not bin_:
        return
    try:
        subprocess.run(
            [bin_, "org.kde.KWin", "/KWin", "reconfigure"],
            check=False, capture_output=True, timeout=5,
        )
    except Exception:
        pass
