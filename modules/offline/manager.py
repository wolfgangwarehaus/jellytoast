"""Download manager — the queue that turns "Download this" into bytes.

Sits on top of ``snapshot`` (freeze metadata), ``index`` (node graph),
and ``store`` (blob files). Its own job is orchestration: a queue with
progress, pause/resume/retry, Wi-Fi-only gating, and writing per-node
state back to ``nodes.state``.

Getting the bytes (design doc §5.3): an **independent background HTTP
GET** of ``get_audio_stream_url(item_id)`` at the configured *download*
quality, into a ``.part`` temp file, atomic-renamed on completion.
Decoupled from mpv entirely — a pause/seek/skip can't corrupt it, and an
interrupted download just leaves a ``.part`` to resume or discard. Runs
on the existing ``async_io`` bounded pool. (mpv ``--stream-record`` was
considered and rejected: it corrupts on seek and only captures from the
demuxer position at the moment it's enabled.)

Subsonic rotates salt/token per request, so the worker resolves the
stream URL at fetch time — it is never stored.

Phase 1: skeleton. Phase 2 wires single-track end-to-end; Phase 3 adds
album/playlist/artist cascade via ``index`` edges.
"""

from __future__ import annotations

from typing import Any, Dict


def enqueue(item: Dict[str, Any]) -> None:
    """Queue ``item`` for download. Snapshots metadata, expands children
    into the node graph (album -> tracks, playlist -> tracks, artist ->
    albums -> tracks), marks the user-touched node ``requested = 1``,
    and starts fetching audio blobs for the leaf tracks. Returns
    immediately; progress is reported via the ``download_progress`` bus
    signal. Phase 2 (track) / Phase 3 (cascade)."""
    raise NotImplementedError("offline.manager.enqueue — Phase 2")


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
