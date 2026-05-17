"""Pending-scrobble queue — persists scrobbles that couldn't be sent
(no connectivity, transient API failure) so they're flushed once the
service is reachable again.

On-disk format: ``scrobble_queue.json`` in ``AppConfigLocation``, a
list of records:

```
[
  {"service": "listenbrainz",
   "track_metadata": {...},
   "listened_at": 1700000000},
  {"service": "lastfm", "artist": "...", "track": "...", ...},
]
```

Storage shape is per-service so we don't lock ourselves into a single
schema for both. The ListenBrainz path uses the API's wire format
verbatim (``track_metadata`` blob), so a flush is a passthrough into
``listenbrainz.send_listens_batch``. The Last.fm shape is decided when
that client lands; for now the queue is service-agnostic and treats
each entry as opaque-except-for-service.

Atomic writes (``.tmp`` + ``os.replace``) match the queue.json pattern
so a crash mid-write leaves the previous good file intact instead of
truncating to junk JSON.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Dict, List

from PySide6.QtCore import QStandardPaths


_QUEUE_FILE = "scrobble_queue.json"

# Belt-and-braces lock around read/modify/write — the manager calls add
# from the GUI thread but flush completion runs from a pool worker via
# run_async's signal slot, which is also queued onto the GUI thread.
# Still, a future caller (e.g. a startup-flush from a different thread)
# would otherwise race; the lock is essentially free in the common case.
_lock = threading.Lock()


def _path() -> Path:
    base = Path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppConfigLocation))
    base.mkdir(parents=True, exist_ok=True)
    return base / _QUEUE_FILE


def _load_raw() -> List[Dict[str, Any]]:
    p = _path()
    if not p.exists():
        return []
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    return [d for d in data if isinstance(d, dict) and d.get("service")]


def _save_raw(items: List[Dict[str, Any]]) -> None:
    p = _path()
    tmp = p.with_suffix(".json.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(items, f)
        os.replace(tmp, p)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass


# ── Public API ─────────────────────────────────────────────────────────────


def add(service: str, record: Dict[str, Any]) -> None:
    """Append a single pending scrobble. ``record`` is the per-service
    payload; the queue stores it verbatim along with the service tag."""
    if not service or not record:
        return
    entry = {"service": service, **record}
    with _lock:
        items = _load_raw()
        items.append(entry)
        _save_raw(items)


def pending(service: str = "") -> List[Dict[str, Any]]:
    """Return pending records, optionally filtered to one service.
    Records are returned in insertion order (oldest first)."""
    with _lock:
        items = _load_raw()
    if not service:
        return items
    return [it for it in items if it.get("service") == service]


def remove(service: str, count: int) -> None:
    """Drop the oldest ``count`` records for ``service`` after a
    successful flush. Idempotent — drops up to ``count``, no error if
    fewer remain."""
    if count <= 0 or not service:
        return
    with _lock:
        items = _load_raw()
        kept: List[Dict[str, Any]] = []
        dropped = 0
        for it in items:
            if dropped < count and it.get("service") == service:
                dropped += 1
                continue
            kept.append(it)
        if dropped > 0:
            _save_raw(kept)


def size(service: str = "") -> int:
    return len(pending(service))


def clear(service: str = "") -> None:
    """Wipe the queue (or just one service's slice). Used by the
    settings page's "Clear pending scrobbles" affordance + by tests."""
    with _lock:
        items = _load_raw()
        if not service:
            _save_raw([])
            return
        kept = [it for it in items if it.get("service") != service]
        _save_raw(kept)
