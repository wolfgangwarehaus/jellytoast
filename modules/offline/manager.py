"""Download manager — the queue that turns "Download this" into bytes.

Sits on top of ``snapshot`` (freeze metadata), ``index`` (node graph),
and ``store`` (blob files). Its own job is orchestration: progress,
pause/resume/retry, Wi-Fi-only gating, and writing per-node state back
to ``nodes.state``.

Getting the bytes (design doc §5.3): an **independent background HTTP
GET** of ``get_audio_stream_url(item_id)``, streamed in chunks into a
``.part`` temp file, atomic-renamed on completion. Decoupled from mpv
entirely — a pause/seek/skip can't corrupt it, and an interrupted
download just leaves a ``.part`` to discard or resume. The blocking GET
runs on the shared ``async_io`` pool via ``run_async``. (mpv
``--stream-record`` was considered and rejected: it corrupts on seek and
only captures from the demuxer position at the moment it's enabled.)

Subsonic rotates salt/token per request, so the URL is resolved at fetch
time inside :func:`enqueue` and handed straight to the worker — never
stored.

Phase 2: single-track download end to end. Quality note: Phase 2 uses
``get_audio_stream_url`` as-is, which honours the user's *streaming*
``audio_quality`` setting (default "original"). A separate
``download_quality`` setting, independent of stream quality, is Phase 6
(design doc §7). The album/playlist/artist cascade — expanding children
through ``index`` edges and a real progress-tracked queue — is Phase 3.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

# Content-Type -> file extension. Original-format streams (Jellyfin
# static=true, Subsonic format=raw) report the real container here;
# the item's own ``Container`` field is the fallback when a server
# sends something generic like application/octet-stream.
_CTYPE_EXT = {
    "audio/flac": "flac",
    "audio/x-flac": "flac",
    "audio/mpeg": "mp3",
    "audio/mp3": "mp3",
    "audio/mp4": "m4a",
    "audio/aac": "m4a",
    "audio/x-m4a": "m4a",
    "audio/ogg": "ogg",
    "audio/opus": "opus",
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "audio/wave": "wav",
}

# Emit a progress signal at most every ~2% so a fast download doesn't
# flood the bus with hundreds of updates per second.
_PROGRESS_STEP = 0.02


def _ext_for(content_type: str, container_hint: str) -> str:
    """Best-guess file extension from the response Content-Type, falling
    back to the item's ``Container`` metadata, then a neutral default."""
    ct = (content_type or "").split(";", 1)[0].strip().lower()
    if ct in _CTYPE_EXT:
        return _CTYPE_EXT[ct]
    hint = (container_hint or "").strip().lower().lstrip(".")
    return hint or "audio"


def enqueue(item: Dict[str, Any]) -> None:
    """Queue ``item`` for download.

    Phase 2 handles a single **track**: snapshot its metadata, upsert
    the node as ``requested``, resolve the stream URL, and kick a
    background GET on the ``async_io`` pool. Returns immediately;
    progress and terminal state are reported via the
    ``download_progress`` bus signal — ``(item_id, state, fraction)``
    where state is pending/downloading/complete/failed.

    Album / playlist / artist cascade is Phase 3 and currently raises.
    """
    from . import snapshot, index

    kind = snapshot.kind_of(item)
    if kind != "track":
        raise NotImplementedError(
            f"offline.manager.enqueue: {kind} cascade — Phase 3"
        )

    item_id = item.get("Id", "")
    if not item_id:
        return

    from modules.player_state import PlayerBus
    bus = PlayerBus.get()

    # Already downloaded — report complete and bail. Cheap idempotency
    # so a double-click on "Download" doesn't re-fetch.
    if index.is_complete(item_id):
        bus.download_progress.emit(item_id, "complete", 1.0)
        return

    meta, _children = snapshot.freeze(item)
    index.upsert_node(item_id, "track", meta, requested=True, state="pending")
    bus.download_progress.emit(item_id, "pending", 0.0)

    from modules.providers import get_provider
    url = get_provider().get_audio_stream_url(item_id)
    if not url:
        index.set_state(item_id, "failed")
        bus.download_progress.emit(item_id, "failed", 0.0)
        return

    container_hint = item.get("Container", "")

    def _work() -> int:
        return _download_track(item_id, url, container_hint, bus)

    def _ok(_bytes: int) -> None:
        index.set_state(item_id, "complete")
        bus.download_progress.emit(item_id, "complete", 1.0)

    def _err(exc: Exception) -> None:
        index.set_state(item_id, "failed")
        bus.download_progress.emit(item_id, "failed", 0.0)
        print(f"[JellyToast] download failed for {item_id}: {exc}",
              flush=True)

    index.set_state(item_id, "downloading")
    bus.download_progress.emit(item_id, "downloading", 0.0)

    from modules.async_io import run_async
    run_async(_work, on_result=_ok, on_error=_err)


def _download_track(item_id: str, url: str, container_hint: str,
                    bus: Any) -> int:
    """Blocking, chunked HTTP GET -> ``.part`` -> atomic commit. Runs on
    an ``async_io`` pool worker. Emits ``download_progress`` as bytes
    arrive (the bus signal is thread-safe — queued connection onto the
    GUI thread). Returns the byte count; raises on any HTTP / IO error,
    which ``run_async`` routes to the ``on_error`` callback. The ``.part``
    is left behind on failure for a future resume / discard."""
    import requests

    from . import store

    with requests.get(url, stream=True, timeout=30) as resp:
        resp.raise_for_status()
        ext = _ext_for(resp.headers.get("Content-Type", ""), container_hint)
        try:
            total = int(resp.headers.get("Content-Length") or 0)
        except ValueError:
            total = 0

        part = store.part_path_for(item_id, ext)
        got = 0
        last_emit = 0.0
        with open(part, "wb") as f:
            for chunk in resp.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                f.write(chunk)
                got += len(chunk)
                if total:
                    frac = got / total
                    if frac - last_emit >= _PROGRESS_STEP:
                        last_emit = frac
                        bus.download_progress.emit(
                            item_id, "downloading", frac)

    codec = ext if ext != "audio" else ""
    store.commit_blob(item_id, part, quality="original", codec=codec,
                      bytes_=got)
    return got


def remove(item_id: str) -> None:
    """Cancel any in-flight work for ``item_id``, then cascade-delete it
    from the index and unlink orphaned blob files off the GUI thread
    (deleting a big playlist must not freeze the UI — Finamp's lesson).
    Phase 3."""
    raise NotImplementedError("offline.manager.remove — Phase 3")


def pause(item_id: str) -> None:
    """Pause an in-flight download; its ``.part`` file is kept for
    resume. Phase 6."""
    raise NotImplementedError("offline.manager.pause — Phase 6")


def resume(item_id: str) -> None:
    """Resume a paused or failed download from its ``.part`` file (HTTP
    range request where the server supports it). Phase 6."""
    raise NotImplementedError("offline.manager.resume — Phase 6")


def retry_failed() -> None:
    """Re-enqueue every node in state ``failed``. Phase 6."""
    raise NotImplementedError("offline.manager.retry_failed — Phase 6")
