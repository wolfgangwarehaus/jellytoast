"""Connectivity awareness + offline mode — the cross-cutting fourth leg.

Audio, metadata, and art are the three legs of offline (design doc §2);
this is the fourth that ties them together: knowing the server is
unreachable and degrading gracefully instead of throwing, plus the
explicit offline-mode concept.

Two pieces of state:

- **reachable** — tracked from API call outcomes (and ``QNetworkInformation``
  where it's reliable, which on Linux it often isn't). A transition
  emits ``connectivity_changed`` on the bus. ``verify_session()`` already
  treats network errors as "creds still good" so a blip won't bounce the
  user to login — this just makes that state observable.

- **offline_mode** — an explicit user toggle, *plus* "automatic offline
  mode" (Symfonium's idea) that flips it on when the server goes
  unreachable. When on, library/search/detail views read from
  ``downloads.db`` only and non-downloaded items are hidden or disabled.

Phase 1: the offline-mode flag + accessors are functional (in-memory,
nothing reads them yet, so no behaviour change). Reachability probing
and bus wiring are Phase 5 — adding the ``connectivity_changed`` /
``offline_mode_changed`` signals to ``PlayerBus`` and feeding outcomes
in from the provider layer.
"""

from __future__ import annotations

# In-memory for Phase 1. Phase 5 decides whether offline_mode persists
# across launches (leaning yes, as a settings property) and wires the
# auto-offline transition.
_offline_mode: bool = False
_server_reachable: bool = True


# ── Offline mode (functional in Phase 1) ────────────────────────────────────

def is_offline_mode() -> bool:
    """True if offline mode is active — either the explicit toggle or
    auto-offline triggered by an unreachable server."""
    return _offline_mode


def set_offline_mode(enabled: bool) -> None:
    """Set offline mode. Phase 5 will emit ``offline_mode_changed`` on
    the bus here and have the views react; for now it just holds the
    flag so call sites can be wired ahead of the behaviour landing."""
    global _offline_mode
    _offline_mode = bool(enabled)
    # Phase 5: get_player_bus().offline_mode_changed.emit(_offline_mode)


# ── Reachability (Phase 5 skeleton) ─────────────────────────────────────────

def is_server_reachable() -> bool:
    """Best-known server reachability. Phase 1 default is optimistic
    (True) — nothing feeds transitions in yet."""
    return _server_reachable


def note_outcome(success: bool) -> None:
    """Feed an API call outcome in. On a reachable<->unreachable
    transition, Phase 5 emits ``connectivity_changed`` and, if
    "automatic offline mode" is enabled, flips offline mode. Phase 5."""
    raise NotImplementedError("offline.connectivity.note_outcome — Phase 5")
