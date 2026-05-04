"""Tiny disk-backed JSON cache for native view payloads.

Used so a cold-launched native view can render from disk immediately
while the server fetch runs in the background. Each cache entry is
keyed by a name (e.g. "songs") plus a "scope" dict that captures the
caller's invariants (parent_id, sort_by, sort_order, …); if any of
those change, the cache is treated as a miss and a fresh load is
required.

Atomic writes via tempfile + rename so a partial flush can't corrupt
subsequent reads. JSON, not pickle, because the payloads are plain
Jellyfin item dicts and inspectable cache files have made debugging
much easier across this codebase.
"""

import json
from pathlib import Path
from typing import Any, Optional

from PySide6.QtCore import QStandardPaths


_CACHE_DIR: "Optional[Path]" = None


def _cache_dir() -> Path:
    global _CACHE_DIR
    if _CACHE_DIR is None:
        base = Path(
            QStandardPaths.writableLocation(
                QStandardPaths.StandardLocation.AppDataLocation
            )
        )
        _CACHE_DIR = base / "view_cache"
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return _CACHE_DIR


def load(name: str, scope: dict) -> Optional[Any]:
    """Return the cached payload for `name`, or None if no cache or
    the stored scope doesn't match `scope`. Scope mismatch covers the
    "user changed sort" case — old payload is for a different ordering
    so we can't reuse it."""
    path = _cache_dir() / f"{name}.json"
    try:
        with open(path) as f:
            data = json.load(f)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    if data.get("scope") != scope:
        return None
    return data.get("payload")


def save(name: str, scope: dict, payload: Any):
    """Persist `payload` under `name` keyed by `scope`. Best-effort —
    a write failure is logged but not raised, since the in-memory
    state is still valid and the next successful save will overwrite."""
    path = _cache_dir() / f"{name}.json"
    tmp = path.with_suffix(".json.tmp")
    try:
        with open(tmp, "w") as f:
            json.dump({"scope": scope, "payload": payload}, f)
        tmp.replace(path)
    except OSError as e:
        print(f"[JellyToast] cache write failed for {name}: {e}", flush=True)


def clear(name: str):
    """Remove the cache file for `name`. No-op if it doesn't exist."""
    path = _cache_dir() / f"{name}.json"
    try:
        path.unlink()
    except (FileNotFoundError, OSError):
        pass
