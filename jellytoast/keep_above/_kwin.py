"""KWin window-rule backend. Lets us inject a "keep above" rule into
KDE's ~/.config/kwinrulesrc and reload KWin so it takes effect
immediately.

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

import shutil
import subprocess
import uuid as _uuid

from jellytoast.keep_above import (
    ABOUT_DIALOG_WINDOW_TITLE,
    CAST_DIALOG_WINDOW_TITLE,
    MAIN_WINDOW_TITLE,
    MINI_PLAYER_WINDOW_TITLE,
    SETTINGS_WINDOW_TITLE,
    SMART_PLAYLIST_EDITOR_WINDOW_TITLE,
)
from jellytoast.settings_migration import open_qsettings

_DESCRIPTION = "jellytoast — keep mini player above (managed)"
_APP_ID = "jellytoast"
_SETTINGS_KEY = "kwin/mini_player_rule_uuid"

# No-border rules: jellytoast's frameless-look windows are actually
# server-side-decorated on KDE Wayland (KWin keeps blur alive on a
# decorated window during a move); this Force rule strips the chrome.
_NOBORDER_DESCRIPTION = "jellytoast — borderless frameless window (managed)"
# (QSettings key holding the rule UUID, window-title substring) pairs.
_NOBORDER_TARGETS = (
    ("kwin/noborder_mini_rule_uuid", MINI_PLAYER_WINDOW_TITLE),
    ("kwin/noborder_settings_rule_uuid", SETTINGS_WINDOW_TITLE),
    ("kwin/noborder_cast_rule_uuid", CAST_DIALOG_WINDOW_TITLE),
    ("kwin/noborder_smartpl_rule_uuid", SMART_PLAYLIST_EDITOR_WINDOW_TITLE),
    ("kwin/noborder_about_rule_uuid", ABOUT_DIALOG_WINDOW_TITLE),
)
# The main window's noborder rule is toggleable (the "Use native window
# border" setting), so it lives outside _NOBORDER_TARGETS — which is the
# always-on mini player + settings dialog — and gets its own install /
# remove pair.
_MAIN_NOBORDER_KEY = "kwin/noborder_main_rule_uuid"


def is_supported() -> bool:
    """KDE Wayland *and* the kwriteconfig/kreadconfig/qdbus tools are on
    PATH. Returns False on any minimal install missing those tools."""
    return bool(_kwriteconfig_bin() and _kreadconfig_bin() and _qdbus_bin())


def install_mini_player_rule() -> bool:
    """Idempotently install (or refresh) the keep-above rule.
    Returns True on success, False if the environment isn't supported."""
    if not is_supported():
        return False

    qs = open_qsettings()
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

    qs = open_qsettings()
    rule_uuid = qs.value(_SETTINGS_KEY, "", type=str)
    if not rule_uuid:
        return False

    _remove_from_rules_list(rule_uuid)
    _delete_rule_group(rule_uuid)
    qs.remove(_SETTINGS_KEY)
    _reconfigure_kwin()
    return True


def install_noborder_rules() -> bool:
    """Idempotently install KWin ``noborder`` Force rules for jellytoast's
    frameless-look windows (mini player + settings dialog). They are
    server-side-decorated on KDE Wayland — this rule makes KWin draw no
    decoration so they still look frameless. Returns True on success,
    False if the environment isn't supported.

    Install this EARLY (before the windows map) so a fresh launch never
    flashes a titlebar. Idempotent + persisted, so it's a no-op refresh
    on every subsequent launch."""
    if not is_supported():
        return False

    qs = open_qsettings()
    for settings_key, title in _NOBORDER_TARGETS:
        rule_uuid = qs.value(settings_key, "", type=str)
        if not rule_uuid:
            rule_uuid = str(_uuid.uuid4())
            qs.setValue(settings_key, rule_uuid)
        _ensure_in_rules_list(rule_uuid)
        _write_noborder_rule_body(rule_uuid, title)
    _reconfigure_kwin()
    return True


def remove_noborder_rules() -> bool:
    """Idempotently remove the ``noborder`` rules. Returns True if any
    rule was present and removed."""
    if not is_supported():
        return False

    qs = open_qsettings()
    removed = False
    for settings_key, _title in _NOBORDER_TARGETS:
        rule_uuid = qs.value(settings_key, "", type=str)
        if not rule_uuid:
            continue
        _remove_from_rules_list(rule_uuid)
        _delete_noborder_rule_group(rule_uuid)
        qs.remove(settings_key)
        removed = True
    if removed:
        _reconfigure_kwin()
    return removed


def install_main_window_noborder() -> bool:
    """Idempotently install the toggleable ``noborder`` rule for the
    main window. Separate from install_noborder_rules() — the mini
    player + settings dialog are always borderless, the main window's
    borderless mode is the "Use native window border" setting. Uses an
    exact title match so the plain "jellytoast" title doesn't also
    strip chrome off the mini player / settings windows. Returns True
    on success, False if the environment isn't supported."""
    if not is_supported():
        return False

    qs = open_qsettings()
    rule_uuid = qs.value(_MAIN_NOBORDER_KEY, "", type=str)
    if not rule_uuid:
        rule_uuid = str(_uuid.uuid4())
        qs.setValue(_MAIN_NOBORDER_KEY, rule_uuid)
    _ensure_in_rules_list(rule_uuid)
    _write_noborder_rule_body(rule_uuid, MAIN_WINDOW_TITLE, titlematch="1")
    _reconfigure_kwin()
    return True


def remove_main_window_noborder() -> bool:
    """Idempotently remove the main window's ``noborder`` rule (the
    user turned native window decorations back on). Returns True if a
    rule was present and removed."""
    if not is_supported():
        return False

    qs = open_qsettings()
    rule_uuid = qs.value(_MAIN_NOBORDER_KEY, "", type=str)
    if not rule_uuid:
        return False
    _remove_from_rules_list(rule_uuid)
    _delete_noborder_rule_group(rule_uuid)
    qs.remove(_MAIN_NOBORDER_KEY)
    _reconfigure_kwin()
    return True


def diagnose() -> dict:
    """Returns the runtime values KWin would match against, plus the
    rule fields we'd write. Handy for figuring out why a rule isn't
    sticking — call from a debug toggle and log the dict."""
    return {
        "backend": "kwin",
        "is_supported": is_supported(),
        "kwriteconfig": _kwriteconfig_bin(),
        "kreadconfig": _kreadconfig_bin(),
        "qdbus": _qdbus_bin(),
        "rule_app_id": _APP_ID,
        "rule_title": MINI_PLAYER_WINDOW_TITLE,
    }


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
            [bin_, "--file", "kwinrulesrc", "--group", group, "--key", key, "--default", default],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
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
            [bin_, "--file", "kwinrulesrc", "--group", group, "--key", key, value],
            check=False,
            capture_output=True,
            timeout=5,
        )
    except Exception:
        pass


def _kdeleteconfig_key(group: str, key: str) -> None:
    bin_ = _kwriteconfig_bin()
    if not bin_:
        return
    try:
        subprocess.run(
            [bin_, "--file", "kwinrulesrc", "--group", group, "--key", key, "--delete"],
            check=False,
            capture_output=True,
            timeout=5,
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
    'jellytoast Mini Player'. Loose matchers (substring + complete=true)
    so we tolerate any X11/Wayland WM_CLASS quirks Qt or KWin might add.

    Action: above=true with aboverule=5 (Force) — KWin reapplies the
    value continuously and prevents user override. Apply (2) wasn't
    sticking on Plasma 6 in our testing; Force is the next-strongest
    SetRule value and is what System Settings → Window Rules generates
    when you pick "Force" for an attribute."""
    fields = {
        "Description": _DESCRIPTION,
        "clientmachine": "localhost",
        "clientmachinematch": "0",
        "wmclass": _APP_ID,
        "wmclassmatch": "2",  # 2 = substring
        "wmclasscomplete": "true",
        "title": MINI_PLAYER_WINDOW_TITLE,
        "titlematch": "2",  # 2 = substring
        "above": "true",
        "aboverule": "5",  # 5 = Force
    }
    for key, val in fields.items():
        _kwriteconfig(rule_uuid, key, val)


def _delete_rule_group(rule_uuid: str) -> None:
    # kwriteconfig has no "delete group" — wipe each key we wrote.
    for key in (
        "Description",
        "clientmachine",
        "clientmachinematch",
        "wmclass",
        "wmclassmatch",
        "wmclasscomplete",
        "title",
        "titlematch",
        "above",
        "aboverule",
    ):
        _kdeleteconfig_key(rule_uuid, key)


_NOBORDER_RULE_FIELDS = (
    "Description",
    "clientmachine",
    "clientmachinematch",
    "wmclass",
    "wmclassmatch",
    "wmclasscomplete",
    "title",
    "titlematch",
    "noborder",
    "noborderrule",
)


def _write_noborder_rule_body(
    rule_uuid: str, title: str, titlematch: str = "2"
) -> None:
    """Scope: wmclass contains 'jellytoast' AND title matches ``title``.
    ``titlematch`` is the KWin match mode: "2" = substring (default —
    the mini player / settings dialog have unique multi-word titles) or
    "1" = exact (the main window, whose plain "jellytoast" title would
    otherwise substring-match the other two).
    Action: noborder=true with noborderrule=2 (Force) — KWin draws no
    decoration for the matched window. ``noborderrule`` value 2 (=
    Force) verified against KWin 6.6."""
    fields = {
        "Description": f"{_NOBORDER_DESCRIPTION} [{title}]",
        "clientmachine": "localhost",
        "clientmachinematch": "0",
        "wmclass": _APP_ID,
        "wmclassmatch": "2",  # 2 = substring
        "wmclasscomplete": "true",
        "title": title,
        "titlematch": titlematch,
        "noborder": "true",
        "noborderrule": "2",  # 2 = Force
    }
    for key, val in fields.items():
        _kwriteconfig(rule_uuid, key, val)


def _delete_noborder_rule_group(rule_uuid: str) -> None:
    for key in _NOBORDER_RULE_FIELDS:
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
            check=False,
            capture_output=True,
            timeout=5,
        )
    except Exception:
        pass
