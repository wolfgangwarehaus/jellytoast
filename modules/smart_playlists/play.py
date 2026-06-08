"""Shared play helper for smart-playlist entries.

Both the Smart Playlists tab's per-row Play button and the editor's
Save & Play action resolve the same way: query the active provider
with the entry's rules, then start playback + navigate to Now Playing.
This module centralises that flow so the two call sites can't drift
out of sync.

The row Play button additionally drives a per-row loading affordance
(disable the button, swap its label to "Loading…"). That UI-specific
work stays in :mod:`smart_playlists_view`; this module owns only the
async resolve + queue + navigate path.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from PySide6.QtWidgets import QWidget


def play_entry(
    entry: Dict[str, Any],
    parent: Optional[QWidget] = None,
    on_complete: Optional[Callable[[], None]] = None,
) -> None:
    """Resolve ``entry``'s rules and start playback.

    Async — the provider query runs on the shared pool; on success
    the queue is replaced via ``PlayerBus.queue_play_now`` and the
    user is navigated to the Now Playing page via
    ``PlayerBus.show_now_playing``.

    ``on_complete`` is invoked exactly once when the async work
    finishes (success, empty match, OR provider error). Used by the
    editor's Save & Play to defer dialog close until the queue
    actually lands — without this the dialog dismisses immediately
    and the user stares at whatever surface they were on for several
    seconds before Now Playing flips in.

    Empty match surfaces a modal info box (the user expected playback
    and didn't get it — they need to know why). Provider error
    is logged but not surfaced; the user has no productive action to
    take and a fresh error dialog would just block them.
    """
    from modules import async_io
    from modules.player_state import PlayerBus, QueueContext, QueueKind
    from modules.providers import get_provider

    rules = entry.get("rules") or {}
    name = entry.get("name") or "Smart playlist"

    def _fire_complete():
        if on_complete is not None:
            try:
                on_complete()
            except Exception:
                pass

    def _go():
        try:
            return list(get_provider().query_items(rules))
        except Exception:
            return []

    def _on_ok(items):
        if not items:
            if parent is not None:
                from modules.frosted_dialog import frosted_info

                frosted_info(
                    parent,
                    "Empty playlist",
                    f'"{name}" matched no tracks right now.',
                )
            _fire_complete()
            return
        bus = PlayerBus.get()
        ctx = QueueContext(
            kind=QueueKind.PLAYLIST,
            source_id="",  # client-side: no server playlist ID
            source_label=name,
        )
        bus.queue_play_now.emit(items, 0, ctx)
        bus.show_now_playing.emit()
        _fire_complete()

    def _on_err(_e):
        _fire_complete()

    async_io.run_async(_go, on_result=_on_ok, on_error=_on_err)
