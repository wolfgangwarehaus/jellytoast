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

Multi-server hostnames (A13): when ``settings.server_hostnames`` is
populated, the unreachable transition first walks the alternate list
in priority order, swapping the active provider's base URL via
``provider.with_url(...)``. Only when every alternate fails do we
flip to unreachable. A reconnect probe periodically tries the
configured primary so a degraded session can climb back up the
preference ladder.
"""

from __future__ import annotations

from typing import List, Optional, Tuple


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

# Label of the currently active host. Empty means "primary
# ``server_url``" (the canonical setup pre-A13). After a successful
# fallback we cache the alternate's label here so a subsequent success
# on that alternate doesn't try to climb back to the primary on every
# note_success — only the periodic primary-probe drives the climb back.
_active_host_label: str = ""

# Probe timeout for an alternate URL — kept short so a failover walk
# over three or four URLs doesn't compound into a multi-second stall.
_PROBE_TIMEOUT_S = 3.0


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


def active_host_label() -> str:
    """Label of the currently active host. Empty when the primary URL
    is active. Useful for status chips / toasts."""
    return _active_host_label


def note_success() -> None:
    """Hook from provider call sites on any successful response. Resets
    the failure counter and, on a transition from unreachable to
    reachable, fires ``connectivity_changed`` and (if the current
    offline-mode was set by auto, not by the user) clears offline
    mode.

    When alternate hostnames are configured, also opportunistically
    probes the primary so a session that failed over to (say)
    Tailscale can climb back to the LAN URL once it's reachable
    again. The climb-back is cheap — one extra probe per success
    while on an alternate, gated by a short cooldown so we don't
    hammer the primary on every API call."""
    global _consecutive_failures, _server_reachable
    _consecutive_failures = 0
    if _server_reachable:
        # Already reachable — but if we're sitting on an alternate,
        # try to climb back to the primary opportunistically.
        if _active_host_label:
            _try_climb_back_to_primary()
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
    consecutive-failure threshold is crossed we first walk the
    configured alternate hostnames; if one of them answers a fast
    health probe, we swap the active provider's base URL to it,
    emit ``host_switched`` with the alternate's label, and stay
    reachable. Only when every alternate fails do we flip to
    unreachable + (if auto-offline is enabled) into offline mode."""
    global _consecutive_failures, _server_reachable
    _consecutive_failures += 1
    if not _server_reachable:
        return
    if _consecutive_failures < _UNREACHABLE_THRESHOLD:
        return
    # Threshold tripped: try alternates before declaring unreachable.
    if _try_failover_to_alternate():
        # Reset the failure counter — the alternate is up, and the
        # next API call lands at it. A subsequent string of failures
        # against the alternate trips the threshold again, walks the
        # remaining alternates, and only then flips to unreachable.
        _consecutive_failures = 0
        return
    _server_reachable = False
    _emit_connectivity_changed(False)
    from modules.settings import get_settings
    if get_settings().auto_offline_mode and not _offline_mode:
        _set_offline_mode_internal(True, source="auto")


# ── Multi-server fallback walk ──────────────────────────────────────────────

def _ordered_hosts() -> List[Tuple[str, str, int]]:
    """All known hosts as ``(label, url, priority)`` tuples sorted by
    priority ascending. The primary ``server_url`` is fronted in as
    priority 0 with label "Primary"; alternates from
    ``server_hostnames`` follow. Empty list when no primary URL is
    configured (clean install, pre-login)."""
    from modules.settings import get_settings
    s = get_settings()
    primary_url = (s.server_url or "").rstrip("/")
    out: List[Tuple[str, str, int]] = []
    if primary_url:
        out.append(("Primary", primary_url, 0))
    for entry in s.server_hostnames:
        url = (entry.get("url") or "").rstrip("/")
        if not url:
            continue
        label = entry.get("label") or url
        try:
            priority = int(entry.get("priority") or 0)
        except (TypeError, ValueError):
            priority = 0
        out.append((label, url, priority))
    out.sort(key=lambda t: t[2])
    return out


def _probe_host(url: str, kind: str) -> bool:
    """Fast health probe of ``url`` for the given provider ``kind``
    (``"jellyfin"`` / ``"subsonic"``). Returns True on a 2xx response,
    False on any error (network, HTTP, parse). Bounded by
    ``_PROBE_TIMEOUT_S`` so a failover walk over multiple alternates
    stays under a couple of seconds even when every alternate is
    dead."""
    if not url:
        return False
    import requests
    url = url.rstrip("/")
    try:
        if kind == "subsonic":
            # Unauth ping — Subsonic answers with status=failed (missing
            # creds) but the HTTP layer returns 200. Either status is
            # proof the host is up.
            r = requests.get(
                f"{url}/rest/ping.view",
                params={"v": "1.16.1", "c": "jellytoast", "f": "json"},
                timeout=_PROBE_TIMEOUT_S,
            )
        else:
            r = requests.get(
                f"{url}/System/Info/Public",
                timeout=_PROBE_TIMEOUT_S,
            )
        return r.status_code < 400
    except Exception:
        return False


def _current_provider_kind() -> str:
    """Best-effort lookup of the active provider's kind. Falls back to
    settings if the provider singleton hasn't been built yet (boot
    path)."""
    try:
        from modules.providers import get_provider
        return (get_provider().kind or "jellyfin").lower()
    except Exception:
        pass
    try:
        from modules.settings import get_settings
        return (get_settings().provider_kind or "jellyfin").lower()
    except Exception:
        return "jellyfin"


def _swap_active_provider_url(new_url: str) -> bool:
    """Re-point the active provider at ``new_url`` via ``with_url``.
    Best-effort: a provider that doesn't implement the swap (or a boot
    state where no provider exists) returns False; the failover walk
    treats that as "couldn't actually switch, don't claim victory"."""
    try:
        from modules.providers import get_provider
        provider = get_provider()
        if not hasattr(provider, "with_url"):
            return False
        provider.with_url(new_url)
        return True
    except Exception:
        return False


def _try_failover_to_alternate() -> bool:
    """Walk the configured alternate hostnames in priority order; the
    first one that answers a fast probe becomes the new active host.
    Returns True if we successfully swapped, False if no alternate is
    reachable (caller then emits ``connectivity_changed(False)``).

    The currently active host is skipped — it's the one that just
    failed, so re-probing it would burn the failover budget on the
    known-dead URL. The primary stays in the candidate pool when an
    alternate is currently active and the primary might be the one
    that's up."""
    hosts = _ordered_hosts()
    if not hosts:
        return False
    kind = _current_provider_kind()
    current_label = _active_host_label or "Primary"
    for label, url, _prio in hosts:
        if label == current_label:
            continue
        if _probe_host(url, kind):
            if _swap_active_provider_url(url):
                _set_active_host(label)
                return True
    return False


def _try_climb_back_to_primary() -> None:
    """Opportunistic probe of the primary URL while we're sitting on
    an alternate. Called from ``note_success`` so the cost is one
    extra probe per successful API call — fine for an idle desktop,
    cheap even under steady load. The climb-back only fires when the
    primary is genuinely up and reachable; on miss we stay on the
    alternate quietly."""
    from modules.settings import get_settings
    primary = (get_settings().server_url or "").rstrip("/")
    if not primary:
        return
    if _active_host_label in ("", "Primary"):
        return
    kind = _current_provider_kind()
    if not _probe_host(primary, kind):
        return
    if _swap_active_provider_url(primary):
        _set_active_host("Primary")


def _set_active_host(label: str) -> None:
    """Update the active-host label + announce on the bus. ``label``
    of "Primary" or empty string both mean the primary URL is active;
    we normalize to "Primary" for the emitted signal so a UI subscriber
    has a stable string to display."""
    global _active_host_label
    new_label = label or "Primary"
    if new_label == "Primary":
        _active_host_label = ""
    else:
        _active_host_label = new_label
    _emit_host_switched(new_label)


# ── Offline mode ────────────────────────────────────────────────────────────

def is_offline_mode() -> bool:
    return _offline_mode


def set_offline_mode(enabled: bool) -> None:
    """Public setter — used by the user's explicit toggle. Always
    treated as "user-set" so a reconnect won't undo it. The persistent
    setting is updated here too so the choice survives restart."""
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


def _emit_host_switched(label: str) -> None:
    try:
        from modules.player_state import PlayerBus
        PlayerBus.get().host_switched.emit(label)
    except Exception:
        pass
    print(f"[jellytoast] host → {label}", flush=True)


# ── Test-only reset ─────────────────────────────────────────────────────────

def _reset_for_tests() -> None:
    """Wipe module state. Used by tests so each case starts clean —
    not part of the public API."""
    global _server_reachable, _offline_mode, _offline_source
    global _consecutive_failures, _active_host_label
    _server_reachable = True
    _offline_mode = False
    _offline_source = None
    _consecutive_failures = 0
    _active_host_label = ""
