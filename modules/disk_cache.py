"""Tiny disk-backed JSON cache for native view payloads.

Used so a cold-launched native view can render from disk immediately
while the server fetch runs in the background. Each cache entry is
keyed by a name (e.g. "songs") plus a "scope" dict that captures the
caller's invariants (parent_id, sort_by, sort_order, …); if any of
those change, the cache is treated as a miss and a fresh load is
required.

Server identity (server_url + provider_kind) is automatically merged
into every scope by this module — callers don't pass it. That way a
fresh sign-in to a different server (or a switch from Jellyfin to
Subsonic on the same machine) can't accidentally hit cache entries
populated against the previous identity.

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


def _server_scope() -> dict:
    """Server identity bits merged into every scope. Lazy-imported so
    disk_cache stays importable from test contexts that don't fully
    initialize settings. Empty dict on failure so a misconfigured
    install still gets isolation across runs (worst case: cache is
    "shared across servers" the way it was before this change)."""
    try:
        from modules.settings import get_settings
        s = get_settings()
        return {
            "_server_url": s.server_url or "",
            "_provider_kind": s.provider_kind or "",
        }
    except Exception:
        return {}


def _full_scope(scope: dict) -> dict:
    return {**_server_scope(), **(scope or {})}


def load(name: str, scope: dict) -> Optional[Any]:
    """Return the cached payload for `name`, or None if no cache or
    the stored scope doesn't match. Scope match requires both the
    caller's keys (sort, parent_id, …) and the implicit server
    identity injected by this module — switching servers invalidates
    every cache entry written under the previous server."""
    path = _cache_dir() / f"{name}.json"
    try:
        with open(path) as f:
            data = json.load(f)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    if data.get("scope") != _full_scope(scope):
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
            json.dump({"scope": _full_scope(scope), "payload": payload}, f)
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


def clear_all():
    """Wipe every view-cache file. Used on sign-out / server-change
    so a logged-out user doesn't see stale Albums / Playlists /
    Genres / Songs / Suggestions from the previous session — the
    server-identity isolation in `_server_scope` doesn't help when
    the server URL is the same and only the access_token went
    away. Best-effort: an individual unlink failure isn't fatal."""
    try:
        for path in _cache_dir().glob("*.json"):
            try:
                path.unlink()
            except OSError:
                pass
    except OSError:
        pass
