"""Connectivity awareness + offline mode — the cross-cutting fourth leg.

Audio, metadata, and art are the three legs of offline (design doc §2);
this is the fourth that ties them together: knowing the server is
unreachable and degrading gracefully instead of throwing, plus the
explicit offline-mode concept.

Two pieces of state:

- **reachable** — fed by ``note_success`` / ``note_network_failure``
  from each provider call site. A short consecutive-failure threshold
  filters transient blips; once the threshold trips, the state flips
  and ``connectivity_changed`` is emitted on ``PlayerBus``.

- **offline_mode** — an explicit user toggle, *plus* "automatic offline
  mode" (Symfonium's idea) that flips it on when the server goes
  unreachable. We track whether the current offline state was set by
  the user or by auto so a reconnect doesn't undo a manual choice.

Public API: ``is_server_reachable``, ``is_offline_mode``,
``set_offline_mode``, ``note_success``, ``note_network_failure``.
Initialised at app startup via ``init()`` so the persisted offline
state from ``settings.offline_mode`` is restored.
"""

from __future__ import annotations

from typing import Optional


# ── Module state ────────────────────────────────────────────────────────────

# True until we've heard otherwise — optimistic start, since the typical
# launch hits the server within seconds.
_server_reachable: bool = True

# Current offline-mode state. Mirrors ``settings.offline_mode`` once
# ``init()`` has run; held in memory so reads don't hit QSettings.
_offline_mode: bool = False

# Was the current offline state set by the user (True) or by the auto
# transition (False)? When auto-set, reconnect clears it; when user-set,
# reconnect leaves it alone. ``None`` means "not currently active".
_offline_source: Optional[str] = None  # "user" | "auto" | None

# Rolling network-failure counter. One success resets to zero. Phase 1
# threshold: three consecutive failures flips us to unreachable. A
# single failure isn't enough — a flaky Wi-Fi roam shouldn't tank the
# whole UI mid-session.
_consecutive_failures: int = 0
_UNREACHABLE_THRESHOLD = 3


# ── Init / lifecycle ────────────────────────────────────────────────────────

def init() -> None:
    """Restore persisted offline-mode + announce the boot state on the
    bus. Called once from ``_post_show_init``, after the PlayerBus +
    Settings singletons exist. Idempotent so a re-init from a test
    harness doesn't double-emit."""
    global _offline_mode, _offline_source
    # Import here so importing this module doesn't require Qt.
    from modules.settings import get_settings
    s = get_settings()
    if s.offline_mode and not _offline_mode:
        _offline_mode = True
        _offline_source = "user"
        _emit_offline_mode_changed(True)


# ── Connectivity (reachable / unreachable) ─────────────────────────────────

def is_server_reachable() -> bool:
    return _server_reachable


def note_success() -> None:
    """Hook from provider call sites on any successful response. Resets
    the failure counter and, on a transition from unreachable to
    reachable, fires ``connectivity_changed`` and (if the current
    offline-mode was set by auto, not by the user) clears offline
    mode."""
    global _consecutive_failures, _server_reachable
    _consecutive_failures = 0
    if _server_reachable:
        return
    _server_reachable = True
    _emit_connectivity_changed(True)
    # If auto flipped us into offline mode, lift it now. A user who
    # manually toggled offline mode stays offline — their choice wins.
    if _offline_mode and _offline_source == "auto":
        _set_offline_mode_internal(False, source=None)


def note_network_failure() -> None:
    """Hook from provider call sites on a network-class exception
    (``requests.RequestException``, timeout). *Not* on HTTPError 4xx —
    those mean the server is reachable but rejected the call. Once the
    consecutive-failure threshold is crossed we flip to unreachable
    and (if auto-offline is enabled) into offline mode."""
    global _consecutive_failures, _server_reachable
    _consecutive_failures += 1
    if not _server_reachable:
        return
    if _consecutive_failures < _UNREACHABLE_THRESHOLD:
        return
    _server_reachable = False
    _emit_connectivity_changed(False)
    from modules.settings import get_settings
    if get_settings().auto_offline_mode and not _offline_mode:
        _set_offline_mode_internal(True, source="auto")


# ── Offline mode ────────────────────────────────────────────────────────────

def is_offline_mode() -> bool:
    return _offline_mode


def set_offline_mode(enabled: bool) -> None:
    """Public setter — used by the user's explicit toggle. Always
    treated as "user-set" so a reconnect won't undo it. The persistent
    setting is updated here too so the choice survives restart."""
    enabled = bool(enabled)
    _set_offline_mode_internal(
        enabled, source=("user" if enabled else None),
    )
    from modules.settings import get_settings
    get_settings().offline_mode = bool(enabled)


def _set_offline_mode_internal(enabled: bool, *, source: Optional[str]) -> None:
    """Flip the in-memory offline-mode state + announce on the bus.
    ``source`` tracks who set the flag — "user" persists across
    reconnect, "auto" lifts when the server comes back. The persistent
    setting is *not* touched here; auto transitions stay in-memory so
    a launch after a transient outage doesn't pin the user offline."""
    global _offline_mode, _offline_source
    if _offline_mode == enabled:
        return
    _offline_mode = enabled
    _offline_source = source if enabled else None
    _emit_offline_mode_changed(enabled)


# ── Bus emit helpers ────────────────────────────────────────────────────────
# Lazy imports keep this module GUI-thread-agnostic and unit-testable
# without a QApplication.

def _emit_connectivity_changed(reachable: bool) -> None:
    try:
        from modules.player_state import PlayerBus
        PlayerBus.get().connectivity_changed.emit(reachable)
    except Exception:
        pass
    state = "reachable" if reachable else "unreachable"
    print(f"[jellytoast] connectivity → {state}", flush=True)


def _emit_offline_mode_changed(enabled: bool) -> None:
    try:
        from modules.player_state import PlayerBus
        PlayerBus.get().offline_mode_changed.emit(enabled)
    except Exception:
        pass
    state = "on" if enabled else "off"
    print(f"[jellytoast] offline mode → {state}", flush=True)


# ── Test-only reset ─────────────────────────────────────────────────────────

def _reset_for_tests() -> None:
    """Wipe module state. Used by tests so each case starts clean —
    not part of the public API."""
    global _server_reachable, _offline_mode, _offline_source
    global _consecutive_failures
    _server_reachable = True
    _offline_mode = False
    _offline_source = None
    _consecutive_failures = 0
