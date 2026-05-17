"""
Smart shuffle — weighted-random ordering that spreads artists out.

Truly-random shuffle (`random.shuffle`) clusters tracks by the same
artist often enough that humans read it as "broken." Spotify rewrote
their shuffle in 2014 for the same reason. This module produces a
permutation by greedy weighted sampling: each candidate's weight is
docked when its artist appeared recently in the in-progress output
(spread penalty) or in the caller-supplied recent-history window
(recency penalty). Floors at 0.05 so nothing is fully blacklisted in
a small library.

Single public entry point: ``smart_shuffle(items, recent_artist_ids,
rng=None)``. Pure function — does not mutate ``items``. Passing a
seeded ``random.Random`` makes the output deterministic. Designed for
``queue_manager._apply_shuffle`` to call on the "rest" list after the
anchored-head item is split off, so the existing keep-current-at-head
semantics survive unchanged.
"""

from __future__ import annotations

import random
from collections import deque
from typing import Iterable, List, Mapping, Optional

# Rolling window cap. Anything older than this in the recent-artist
# history is ignored. Matches the design doc's "N = 8" starting point;
# the in-progress output is tracked separately and unbounded within a
# single shuffle call.
_HISTORY_WINDOW = 8

# Artist-spread penalty: how steeply to dock the weight of a candidate
# whose artist already appeared near the tail of the in-progress
# output. Index 0 = same artist as the most recent pick, index 1 = two
# picks back, … The fall-off is intentionally aggressive at distance 0
# (we *really* don't want back-to-back same-artist) and tapers off so
# distance ≥ 6 is essentially unpenalized.
_SPREAD_PENALTY = (0.95, 0.75, 0.55, 0.35, 0.20, 0.10)

# Recency penalty: same shape but applied to the caller-supplied
# history window (last tracks the user actually heard before this
# shuffle). Less aggressive than spread — historical clutter matters
# less than what's clustered right now in the new order.
_RECENCY_PENALTY = (0.60, 0.45, 0.30, 0.20, 0.10, 0.05)

# Hard floor on a candidate's weight. Without this a small library
# whose artists are all "recent" would end up with every candidate at
# weight 0 (uniformly random.choices error) — and even short of that,
# a near-zero weight starves a track for many positions in a row.
_WEIGHT_FLOOR = 0.05

# Below this item count the spread penalty starves picks (every artist
# is "recent") and the result is barely distinguishable from classic
# shuffle anyway. Fall back to classic in that regime.
_SMART_MIN_ITEMS = 2 * _HISTORY_WINDOW


def _artist_id(item: Mapping) -> str:
    """Return the item's artist id, tolerating both Jellyfin
    (``ArtistId``) and Subsonic (``artistId``) casings. Missing /
    blank treated as a single anonymous bucket so a library of
    "Unknown Artist" tracks still gets the spread penalty applied
    (avoids true-random clumping among the anonymous group)."""
    v = item.get("ArtistId") or item.get("artistId") or ""
    return str(v) if v else "__unknown__"


def _weight_for(
    artist: str,
    output_artists: List[str],
    recent_artists: List[str],
) -> float:
    """Compute the sampling weight for a candidate whose artist is
    ``artist``. Starts at 1.0, docks for recency in both the
    in-progress output and the caller's history window, floors at
    ``_WEIGHT_FLOOR``."""
    w = 1.0
    # Spread: walk backwards through the in-progress output; the
    # closer the same-artist match, the heavier the dock.
    for distance, prev in enumerate(reversed(output_artists)):
        if distance >= len(_SPREAD_PENALTY):
            break
        if prev == artist:
            w -= _SPREAD_PENALTY[distance]
    # Recency: same walk through the user-supplied history window.
    for distance, prev in enumerate(reversed(recent_artists)):
        if distance >= len(_RECENCY_PENALTY):
            break
        if prev == artist:
            w -= _RECENCY_PENALTY[distance]
    return max(w, _WEIGHT_FLOOR)


def smart_shuffle(
    items: List[Mapping],
    recent_artist_ids: Iterable[str],
    rng: Optional[random.Random] = None,
) -> List[Mapping]:
    """Return a new list — ``items`` reordered so same-artist tracks
    don't cluster. Does NOT mutate ``items``.

    ``recent_artist_ids`` seeds the rolling-history penalty so the
    first few picks avoid artists the user just heard. Order matters:
    last element is treated as the most recent. Anything beyond
    ``_HISTORY_WINDOW`` is dropped.

    ``rng`` is the ``random.Random`` to draw from. Pass a seeded one
    for deterministic output (tests, reproducibility). Defaults to
    the module-level ``random`` for production use.
    """
    if not items:
        return []
    if len(items) == 1:
        return [items[0]]
    if rng is None:
        rng = random
    # Tiny libraries: classic shuffle (see _SMART_MIN_ITEMS docstring).
    if len(items) < _SMART_MIN_ITEMS:
        out = list(items)
        rng.shuffle(out)
        return out

    bag = list(items)
    bag_artists = [_artist_id(it) for it in bag]
    history: deque = deque(
        (str(a) for a in recent_artist_ids if a is not None),
        maxlen=_HISTORY_WINDOW,
    )

    output: List[Mapping] = []
    output_artists: List[str] = []
    history_list = list(history)

    while bag:
        weights = [
            _weight_for(a, output_artists, history_list)
            for a in bag_artists
        ]
        # random.choices needs at least one positive weight; the floor
        # guarantees it, but be defensive.
        if not any(w > 0 for w in weights):
            weights = [1.0] * len(bag)
        idx = rng.choices(range(len(bag)), weights=weights, k=1)[0]
        picked = bag.pop(idx)
        picked_artist = bag_artists.pop(idx)
        output.append(picked)
        output_artists.append(picked_artist)
        # Keep the history window updated as we pick — once enough
        # picks have happened the seeded history naturally rolls off.
        history_list.append(picked_artist)
        if len(history_list) > _HISTORY_WINDOW:
            history_list = history_list[-_HISTORY_WINDOW:]

    return output
