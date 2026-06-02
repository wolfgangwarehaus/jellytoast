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

import logging
import threading
import time
from typing import Any, Callable, List, Optional, Tuple

logger = logging.getLogger(__name__)


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

# Rolling network-failure counter. One success resets to zero. Two
# consecutive failures flips us to unreachable. A single failure isn't
# enough — a flaky Wi-Fi roam (one dropped request, next one fine)
# shouldn't tank the UI — but at 3s connect timeout per call the old
# threshold of 3 felt sluggish (~9s to trip). 2 lands us at ~6s while
# still tolerating an isolated blip.
#
# The count alone is NOT sufficient: a library grid fans ~8 requests
# through the run_async pool at once, so a momentary hiccup can land
# 2+ failures within milliseconds and spuriously flip the app offline
# — observed flapping offline/online repeatedly right after a provider
# swap. So the flip also requires a minimum *elapsed window* since the
# first failure of the current run: a near-simultaneous burst can't
# trip it, only failures that persist across the window (a genuine
# outage) do. The counter is mutated from worker threads, so all
# read-modify-writes are guarded by ``_state_lock``.
_consecutive_failures: int = 0
_UNREACHABLE_THRESHOLD = 2
_UNREACHABLE_MIN_WINDOW_S = 4.0
# Monotonic timestamp of the first failure in the current unbroken run
# (reset to 0.0 on any success). 0.0 means "no run in progress".
_first_failure_ts: float = 0.0
# Guards the failure/auth counters + reachable flag against the 8-thread
# run_async pool that drives every provider call site.
_state_lock = threading.Lock()
# Indirection over the clock so tests can install a deterministic /
# auto-advancing stand-in via ``_reset_for_tests``. Production uses the
# real monotonic clock.
_now: Callable[[], float] = time.monotonic

# Rolling auth-failure counter. Definitive auth-reject from the server
# (HTTP 401/403 for Jellyfin, Subsonic error 40 for Subsonic). Resets
# on any successful auth-bearing call. When the threshold trips we
# emit ``auth_failed`` on the PlayerBus so MainWindow can drop to
# LoginView. Higher threshold than connectivity because we know
# Navidrome's first ping-after-restart can return code 40 spuriously
# (see [[known-issue-navidrome-boot-ping]]) — 3 protects against that
# one-off while still catching genuinely-bad stored creds quickly.
_consecutive_auth_failures: int = 0
_AUTH_FAILURE_THRESHOLD = 3
# One-shot latch: with the >= threshold test (vs the old exact ==), this keeps
# auth_failed firing exactly once past the threshold even if a concurrent
# burst overshoots the count. Cleared on a success / server change.
_auth_failed_emitted: bool = False

# Label of the currently active host. Empty means "primary
# ``server_url``" (the canonical setup pre-A13). After a successful
# fallback we cache the alternate's label here so a subsequent success
# on that alternate doesn't try to climb back to the primary on every
# note_success — only the periodic primary-probe drives the climb back.
_active_host_label: str = ""

# Probe timeout for an alternate URL — kept short so a failover walk
# over three or four URLs doesn't compound into a multi-second stall.
_PROBE_TIMEOUT_S = 3.0

# Periodic probe cadence while auto-offline is active. Fast enough that
# "I plugged Ethernet back in" feels responsive (≤10s), slow enough that
# a multi-hour outage doesn't burn cycles. One HTTP request per tick.
_AUTO_PROBE_INTERVAL_MS = 10_000

# A QObject living on the GUI thread that owns the auto-probe QTimer.
# Built eagerly during init() so the timer has a real event loop to
# fire on — note_network_failure / note_success are invoked from
# worker threads (run_async in jellyfin_api._get/_post), so a lazily-
# created timer would otherwise be owned by a pool worker with no
# event loop, never firing. Start/stop go through a Qt signal with
# AutoConnection so cross-thread calls automatically queue onto the
# controller's GUI thread.
_auto_probe_controller = None  # type: ignore[var-annotated]


# ── Init / lifecycle ────────────────────────────────────────────────────────


def init() -> None:
    """Restore persisted offline-mode + announce the boot state on the
    bus. Called once from ``_post_show_init``, after the PlayerBus +
    Settings singletons exist. Idempotent so a re-init from a test
    harness doesn't double-emit.

    Also builds the auto-probe controller (QObject + QTimer) on the
    GUI thread. This MUST happen here — not lazily on first failure —
    because the failure callbacks run on worker threads, and a QTimer
    created on a worker pool thread without an event loop never fires."""
    global _offline_mode, _offline_source
    _ensure_auto_probe_controller()
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
    global _consecutive_failures, _server_reachable, _first_failure_ts
    with _state_lock:
        _consecutive_failures = 0
        _first_failure_ts = 0.0
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
    global _consecutive_failures, _server_reachable, _first_failure_ts
    now = _now()
    with _state_lock:
        _consecutive_failures += 1
        if _consecutive_failures == 1:
            _first_failure_ts = now
        count = _consecutive_failures
        first_ts = _first_failure_ts
        reachable = _server_reachable
    if not reachable:
        return
    # Flip only when BOTH the count threshold is met AND the failures have
    # persisted across the minimum window. A near-simultaneous burst (the
    # grid's parallel request fan-out) clusters inside the window and is
    # ignored; a genuine outage keeps failing past it and trips.
    if count < _UNREACHABLE_THRESHOLD or (now - first_ts) < _UNREACHABLE_MIN_WINDOW_S:
        return
    # Threshold tripped: try alternates before declaring unreachable.
    if _try_failover_to_alternate():
        # Reset the failure counter — the alternate is up, and the
        # next API call lands at it. A subsequent string of failures
        # against the alternate trips the threshold again, walks the
        # remaining alternates, and only then flips to unreachable.
        with _state_lock:
            _consecutive_failures = 0
            _first_failure_ts = 0.0
        return
    with _state_lock:
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
        # Best-effort reachability probe — any failure means "not up".
        # Swallow, but trace at debug so a misconfigured alternate host
        # is visible under JT_LOG_LEVEL=DEBUG.
        logger.debug("host probe failed for %s (kind=%s)", url, kind, exc_info=True)
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


def _try_recover_any_host() -> bool:
    """Walk ALL configured hosts (primary + alternates) and return True
    as soon as one answers a fast health probe. Sister of
    ``_try_failover_to_alternate`` but without the "skip current" rule
    — the current host may be exactly the one coming back online (most
    common case: Wi-Fi flicker, primary recovers). On success the
    matching host is set active. On total failure, returns False and
    leaves state alone — the caller decides what to do (retry later
    for the auto-probe, fall back to "still offline" UI for the chip)."""
    hosts = _ordered_hosts()
    if not hosts:
        return False
    kind = _current_provider_kind()
    for label, url, _prio in hosts:
        if _probe_host(url, kind):
            if _swap_active_provider_url(url):
                _set_active_host(label)
            return True
    return False


def probe_now(on_done: Optional[Callable[[bool], None]] = None) -> None:
    """Public entry point for "try to come back online right now". Runs
    the probe off the GUI thread; on success, marks the server
    reachable and lifts auto-offline (user-set offline is left alone —
    their choice wins). ``on_done`` fires on the GUI thread with the
    recovery result so callers (the chip's connecting animation) can
    exit their pending UI state regardless of outcome.

    Safe to call from any state. If no hosts are configured (clean
    install, pre-login) the probe no-ops with False."""

    def _on_result(recovered: Any) -> None:
        if recovered:
            _on_recovery_succeeded()
        if on_done is not None:
            try:
                on_done(bool(recovered))
            except Exception:
                pass

    def _on_error(_e: Exception) -> None:
        if on_done is not None:
            try:
                on_done(False)
            except Exception:
                pass

    try:
        from modules.async_io import run_async

        run_async(_try_recover_any_host, on_result=_on_result, on_error=_on_error)
    except Exception:
        # No GUI / async harness — probe synchronously so unit tests
        # exercising this path still work.
        try:
            _on_result(_try_recover_any_host())
        except Exception as e:
            _on_error(e)


def _on_recovery_succeeded() -> None:
    """Shared post-probe bookkeeping for the recovery path. Flips the
    reachable flag + lifts auto-offline (not user-set)."""
    global _server_reachable, _consecutive_failures, _first_failure_ts
    with _state_lock:
        _consecutive_failures = 0
        _first_failure_ts = 0.0
    if not _server_reachable:
        _server_reachable = True
        _emit_connectivity_changed(True)
    if _offline_mode and _offline_source == "auto":
        _set_offline_mode_internal(False, source=None)


def reset_after_server_change() -> None:
    """Reset connectivity state after an in-app sign-in / server swap.

    The failure/auth counters, the active-host label, and any AUTO-set
    offline mode are carried-over module globals. After a swap (e.g.
    Jellyfin → Navidrome in the same process) the previous server's
    leftover failure state — and the new server's first-load request
    burst — would otherwise trip offline mode spuriously, flapping the UI
    in and out of offline mode during the initial load. Reset to the
    optimistic boot state so the new server starts clean. A *user*-set
    offline mode is left intact (their explicit choice wins)."""
    global _server_reachable, _consecutive_failures, _first_failure_ts
    global _consecutive_auth_failures, _active_host_label, _auth_failed_emitted
    with _state_lock:
        _server_reachable = True
        _consecutive_failures = 0
        _first_failure_ts = 0.0
        _consecutive_auth_failures = 0
        _auth_failed_emitted = False
        _active_host_label = ""
    _stop_auto_probe()
    if _offline_mode and _offline_source == "auto":
        _set_offline_mode_internal(False, source=None)


# ── Auto-probe loop ────────────────────────────────────────────────────────
# Periodic background probe started when auto-offline engages. Without
# this, recovery is purely reactive on the next provider call — but in
# offline mode all UI reads go through the local cache, so without
# traffic to learn from the app would sit offline indefinitely.
#
# Thread-safety: the failure / success hooks that drive start / stop are
# called from worker threads (jellyfin_api._get/_post run on the
# run_async pool). The controller QObject lives on the GUI thread and
# exposes signals connected via AutoConnection — so cross-thread emits
# get queued onto the GUI event loop where the QTimer can actually fire.


def _ensure_auto_probe_controller() -> None:
    """Build the controller eagerly on the GUI thread. Called from
    ``init()`` so by the time any worker thread tries to start the
    probe, the timer is already alive and owned by the GUI event loop.
    Headless tests skip Qt entirely and leave the controller None —
    start/stop become no-ops."""
    global _auto_probe_controller
    if _auto_probe_controller is not None:
        return
    try:
        from PySide6.QtCore import QObject, Qt, QTimer, Signal

        class _Controller(QObject):
            _start_requested = Signal()
            _stop_requested = Signal()

            def __init__(self) -> None:
                super().__init__()
                self._timer = QTimer(self)
                self._timer.setInterval(_AUTO_PROBE_INTERVAL_MS)
                self._timer.timeout.connect(_auto_probe_tick)
                # AutoConnection → DirectConnection on the GUI thread,
                # QueuedConnection from worker threads. Either way the
                # slot runs on the controller's owner thread (GUI).
                self._start_requested.connect(
                    self._do_start,
                    Qt.ConnectionType.AutoConnection,
                )
                self._stop_requested.connect(
                    self._do_stop,
                    Qt.ConnectionType.AutoConnection,
                )

            def _do_start(self) -> None:
                if not self._timer.isActive():
                    self._timer.start()

            def _do_stop(self) -> None:
                if self._timer.isActive():
                    self._timer.stop()

            def request_start(self) -> None:
                self._start_requested.emit()

            def request_stop(self) -> None:
                self._stop_requested.emit()

            def is_active(self) -> bool:
                return self._timer.isActive()

        _auto_probe_controller = _Controller()
    except Exception:
        _auto_probe_controller = None


def _start_auto_probe() -> None:
    if _auto_probe_controller is not None:
        _auto_probe_controller.request_start()


def _stop_auto_probe() -> None:
    if _auto_probe_controller is not None:
        _auto_probe_controller.request_stop()


def _auto_probe_tick() -> None:
    # Defensive: if state drifted (user claimed offline, or we already
    # recovered) the timer should have been stopped; stop it now and
    # bail before firing a wasted probe.
    if not (_offline_mode and _offline_source == "auto"):
        _stop_auto_probe()
        return
    probe_now()


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
    setting is updated here too so the choice survives restart.

    If auto-offline is currently active and the user re-asserts
    offline=True, we promote the source to "user" (and stop the
    background probe) so their choice locks the state in. Without this,
    ``_set_offline_mode_internal`` early-returns on the no-op transition
    and the source stays "auto" — the recovery probe would then keep
    trying to bring them online behind their back."""
    global _offline_source
    enabled = bool(enabled)
    if enabled and _offline_mode and _offline_source != "user":
        _offline_source = "user"
        _stop_auto_probe()
    _set_offline_mode_internal(
        enabled,
        source=("user" if enabled else None),
    )
    from modules.settings import get_settings

    get_settings().offline_mode = bool(enabled)


def _set_offline_mode_internal(enabled: bool, *, source: Optional[str]) -> None:
    """Flip the in-memory offline-mode state + announce on the bus.
    ``source`` tracks who set the flag — "user" persists across
    reconnect, "auto" lifts when the server comes back. The persistent
    setting is *not* touched here; auto transitions stay in-memory so
    a launch after a transient outage doesn't pin the user offline.

    Also gates the auto-probe timer: starts it on an auto-engage,
    stops it on any disengage. The probe is the only path back from
    auto-offline without a user-initiated provider call."""
    global _offline_mode, _offline_source
    if _offline_mode == enabled:
        return
    _offline_mode = enabled
    _offline_source = source if enabled else None
    _emit_offline_mode_changed(enabled)
    if enabled and source == "auto":
        _start_auto_probe()
    else:
        _stop_auto_probe()


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
    logger.info("connectivity → %s", state)


def _emit_offline_mode_changed(enabled: bool) -> None:
    try:
        from modules.player_state import PlayerBus

        PlayerBus.get().offline_mode_changed.emit(enabled)
    except Exception:
        pass
    state = "on" if enabled else "off"
    logger.info("offline mode → %s", state)


def note_auth_failure() -> None:
    """Hook from provider call sites on a definitive auth-rejection
    (HTTP 401/403 for Jellyfin, Subsonic error 40 for Subsonic). At
    the threshold we emit ``auth_failed`` so MainWindow drops to
    LoginView. Idempotent past threshold — additional failures don't
    re-emit.

    Mutated from the 8-thread run_async pool (jellyfin_api._get/_post,
    subsonic._request), so the read-modify-write is guarded by _state_lock —
    matching the sibling network counters. The threshold test reads the
    post-increment value captured INSIDE the lock and uses >= + a one-shot
    latch, so a concurrent overshoot (two workers racing the counter past 3)
    still emits exactly once instead of skipping the == 3 crossing entirely
    and stranding the user on silent 401 empty-states. The emit happens
    OUTSIDE the lock (a Qt signal must not run under it)."""
    global _consecutive_auth_failures, _auth_failed_emitted
    with _state_lock:
        _consecutive_auth_failures += 1
        should_emit = (
            _consecutive_auth_failures >= _AUTH_FAILURE_THRESHOLD
            and not _auth_failed_emitted
        )
        if should_emit:
            _auth_failed_emitted = True
    if should_emit:
        _emit_auth_failed()


def note_auth_success() -> None:
    """Hook from provider call sites on a successful auth-bearing
    response. Resets the auth-failure counter so a single transient
    rejection (Navidrome's boot-ping quirk, a brief server hiccup)
    doesn't accumulate toward the threshold across long sessions."""
    global _consecutive_auth_failures, _auth_failed_emitted
    with _state_lock:
        _consecutive_auth_failures = 0
        _auth_failed_emitted = False


def _emit_auth_failed() -> None:
    try:
        from modules.player_state import PlayerBus

        PlayerBus.get().auth_failed.emit()
    except Exception:
        pass
    logger.warning("auth → failed (threshold reached)")


def _emit_host_switched(label: str) -> None:
    try:
        from modules.player_state import PlayerBus

        PlayerBus.get().host_switched.emit(label)
    except Exception:
        pass
    logger.info("host → %s", label)


# ── Test-only reset ─────────────────────────────────────────────────────────


def _reset_for_tests() -> None:
    """Wipe module state. Used by tests so each case starts clean —
    not part of the public API."""
    global _server_reachable, _offline_mode, _offline_source
    global _consecutive_failures, _active_host_label
    global _consecutive_auth_failures, _first_failure_ts, _now, _auth_failed_emitted
    _server_reachable = True
    _offline_mode = False
    _offline_source = None
    _consecutive_failures = 0
    _consecutive_auth_failures = 0
    _auth_failed_emitted = False
    _first_failure_ts = 0.0
    _active_host_label = ""
    # Install an auto-advancing fake clock: each discrete note_* call in
    # a test reads as a distinct failure separated by more than the
    # hysteresis window, so the long-standing "N failures → flip" test
    # contract holds without per-test clock plumbing. A test that wants
    # to exercise burst-immunity installs its own fixed clock by setting
    # `_now` after this reset runs.
    _clock = {"t": 0.0}

    def _auto_advancing_now() -> float:
        _clock["t"] += _UNREACHABLE_MIN_WINDOW_S + 1.0
        return _clock["t"]

    _now = _auto_advancing_now
    _stop_auto_probe()
