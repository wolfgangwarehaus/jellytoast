"""Smart-playlist editor live-preview generation guard (2026-05-28
audit #11): a stale preview result — an older edit's query finishing
after a newer edit fired a fresh one — must be dropped, not painted,
so the preview never shows results for a rule set the user has moved on
from. The editor builds with just the ``qapp`` fixture (no provider).
"""

from __future__ import annotations

from modules.smart_playlist_editor import SmartPlaylistEditorDialog


class TestPreviewGenerationGuard:
    def test_stale_result_dropped_current_result_painted(self, qapp):
        dlg = SmartPlaylistEditorDialog()
        try:
            # Simulate two previews in flight: the current generation is 5.
            dlg._preview_gen = 5
            dlg._preview_list.clear()

            # A result tagged with an older generation is ignored.
            dlg._on_preview_result([{"Name": "Stale", "Artist": "X"}], gen=3)
            assert dlg._preview_list.count() == 0

            # The current generation paints.
            dlg._on_preview_result([{"Name": "Fresh", "Artist": "Y"}], gen=5)
            assert dlg._preview_list.count() == 1
        finally:
            dlg.deleteLater()

    def test_each_refresh_bumps_the_generation(self, qapp, monkeypatch):
        # Stub the async dispatch: _refresh_preview fires
        # run_async(_go) where _go calls get_provider().query_items() —
        # a REAL provider build (keyring import) + HTTP on a pool worker
        # that outlives the test and can SIGSEGV mid-GC under load. The
        # generation bump we assert on happens BEFORE the dispatch, so
        # no-op'ing run_async keeps the test honest and leak-free.
        import modules.async_io as _aio

        monkeypatch.setattr(_aio, "run_async", lambda *a, **k: None)
        dlg = SmartPlaylistEditorDialog()
        try:
            before = dlg._preview_gen
            dlg._refresh_preview()
            assert dlg._preview_gen == before + 1
        finally:
            dlg.deleteLater()
