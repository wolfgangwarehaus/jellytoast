"""Settings access for the DLNA backend.

Read-only here; the UI follow-up handles writes. Honors
``cast/dlna_enabled`` and ``cast/dlna_user_agent_overrides``.

NOTE — shipped-but-unwired: the per-renderer User-Agent override path
(``_settings_user_agent_overrides`` → ``_ua_for_device``) is fully built +
unit-tested but **not yet called by any push**. ``controller.py`` does not
set a per-device User-Agent today. It's kept as the power-user escape hatch
for the Samsung-firmware UA quirk (research §6); to activate it, resolve
``_ua_for_device(device_name)`` in ``controller.py``'s request setup. (Same
status as the shipped-but-unwired ``SonosEventBridge``.)
"""

from __future__ import annotations

import json
from typing import Dict

from ._constants import USER_AGENT_TEMPLATE


def _settings_enabled() -> bool:
    """Honor ``cast/dlna_enabled`` (default True).

    Returning ``True`` when settings can't be read keeps the test path
    (no QSettings, no Qt) and the first-run path (key never written)
    aligned: DLNA on unless explicitly disabled."""
    try:
        from jellytoast.settings import get_settings

        s = get_settings()
    except Exception:
        return True
    # QSettings stores ints; fall back to a defensive bool() so any
    # legacy string write still does the right thing.
    raw = s._s.value("cast/dlna_enabled", True)
    if isinstance(raw, str):
        return raw.lower() not in ("0", "false", "no", "off", "")
    return bool(raw)


def _settings_user_agent_overrides() -> Dict[str, str]:
    """Per-renderer User-Agent overrides — ``cast/dlna_user_agent_overrides``.

    JSON map of ``device-name pattern → User-Agent``. No UI today; this
    is the power-user escape hatch for the Samsung-firmware UA quirk
    documented in the research doc §6. Returns an empty dict on missing
    / malformed values so the caller can always iterate."""
    try:
        from jellytoast.settings import get_settings

        s = get_settings()
    except Exception:
        return {}
    raw = s._s.value("cast/dlna_user_agent_overrides", "", type=str)
    if not raw:
        return {}
    try:
        v = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    if not isinstance(v, dict):
        return {}
    # Reject non-string values defensively — QSettings can return
    # surprising types if the user hand-edits the conf file.
    return {str(k): str(val) for k, val in v.items() if val}


def _ua_for_device(device_name: str, version: str = "0.x") -> str:
    """Pick a User-Agent for a push, honoring per-device overrides.

    Unwired today — see the module-level note; no push calls this yet.

    Pattern matching is substring (case-insensitive) — keeps the JSON
    config human-writable. First match wins; falls back to the global
    UA template if no override fires.

    ``_settings_user_agent_overrides`` is resolved through the package
    namespace (``jellytoast.cast.dlna``) so the test suite's monkeypatch
    of that name stays load-bearing."""
    from jellytoast.cast import dlna as _pkg

    overrides = _pkg._settings_user_agent_overrides()
    if overrides and device_name:
        haystack = device_name.lower()
        for pattern, ua in overrides.items():
            if pattern and pattern.lower() in haystack:
                return ua
    return USER_AGENT_TEMPLATE.format(ver=version)


__all__ = [
    "_settings_enabled",
    "_settings_user_agent_overrides",
    "_ua_for_device",
]
