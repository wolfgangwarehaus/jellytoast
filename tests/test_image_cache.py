"""Disk image cache (jellytoast/image_cache.py) invariants.

Covers the two failure modes that motivated this cache: stable identity
across Subsonic-style auth-rotated URLs (cache_key, not URL, drives
identity) and bounded growth via mtime-LRU eviction. A regression in
either silently breaks the user-visible "albums load instantly on
relaunch" promise.
"""

import os
import time

import pytest
from PySide6.QtGui import QColor, QImage, QPixmap

from jellytoast import image_cache


@pytest.fixture
def isolated_cache(tmp_path, monkeypatch):
    """Redirect the cache dir to tmp_path so each test starts from a
    blank slate without touching the user's real cover cache."""
    target = tmp_path / "covers"
    target.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(image_cache, "_CACHE_DIR", target)
    monkeypatch.setattr(image_cache, "_puts_since_eviction", 0)
    yield target


def _make_pix(w: int = 16, h: int = 16, color: str = "#ff0000") -> QPixmap:
    img = QImage(w, h, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(QColor(color))
    return QPixmap.fromImage(img)


class TestRoundTrip:
    def test_miss_returns_none(self, qapp, isolated_cache):
        assert image_cache.get("nope") is None

    def test_put_then_get_round_trips(self, qapp, isolated_cache):
        pix = _make_pix()
        image_cache.put("album-1|360x360|r=8", pix)
        image_cache.flush_pending_writes()
        out = image_cache.get("album-1|360x360|r=8")
        assert out is not None
        assert not out.isNull()
        assert out.width() == 16 and out.height() == 16

    def test_put_skips_null_pixmap(self, qapp, isolated_cache):
        image_cache.put("empty", QPixmap())
        image_cache.flush_pending_writes()
        assert image_cache.get("empty") is None
        assert not any(isolated_cache.iterdir())

    def test_filename_is_hash_not_raw_key(self, qapp, isolated_cache):
        """Cache keys can contain slashes, pipes, and other filesystem-
        unsafe characters — they must be hashed, not used as filenames."""
        image_cache.put("a/b|c|x=1", _make_pix())
        image_cache.flush_pending_writes()
        files = list(isolated_cache.iterdir())
        assert len(files) == 1
        assert files[0].suffix == ".png"
        # Filename is a 40-char SHA1 hex digest.
        assert len(files[0].stem) == 40


class TestMtimeTouch:
    def test_get_touches_mtime(self, qapp, isolated_cache):
        """LRU eviction sorts by mtime — a hit must mark the entry
        recently used so frequently-loaded covers aren't evicted in
        favor of one-time fetches."""
        image_cache.put("hot", _make_pix())
        image_cache.flush_pending_writes()
        path = isolated_cache / (os.listdir(isolated_cache)[0])
        old_mtime = path.stat().st_mtime
        # File systems quantize mtime; sleep past the boundary.
        time.sleep(0.05)
        os.utime(path, (old_mtime - 60, old_mtime - 60))
        prev = path.stat().st_mtime
        image_cache.get("hot")
        new_mtime = path.stat().st_mtime
        assert new_mtime > prev


class TestEviction:
    def test_evicts_oldest_when_over_cap(self, qapp, isolated_cache, monkeypatch):
        """Force the eviction path with a tiny cap so we can verify
        oldest-first eviction without filling 200MB."""
        monkeypatch.setattr(image_cache, "_DISK_CACHE_MAX_BYTES", 200)
        # Each PNG is ~100B for a 16x16 solid color, so 5 entries
        # well over the 200B cap.
        for i in range(5):
            image_cache.put(f"k-{i}", _make_pix(color=f"#{i:02x}{i:02x}{i:02x}"))
            image_cache.flush_pending_writes()
            time.sleep(0.01)
        image_cache._evict_if_over_cap()
        # Oldest entries should be gone; newer ones remain.
        assert image_cache.get("k-0") is None
        assert image_cache.get("k-4") is not None

    def test_no_eviction_under_cap(self, qapp, isolated_cache, monkeypatch):
        monkeypatch.setattr(image_cache, "_DISK_CACHE_MAX_BYTES", 10 * 1024 * 1024)
        for i in range(3):
            image_cache.put(f"k-{i}", _make_pix())
            image_cache.flush_pending_writes()
        image_cache._evict_if_over_cap()
        for i in range(3):
            assert image_cache.get(f"k-{i}") is not None

    def test_schedule_coalesces_concurrent_sweeps(
        self, qapp, isolated_cache, monkeypatch
    ):
        """A second _schedule_eviction while a sweep is still running is a
        no-op — two concurrent disk walks double-count and can over-evict a
        cover a live read still needs."""
        calls = {"n": 0}
        held = []

        def _fake_run_async(fn, on_result=None, on_error=None):
            calls["n"] += 1
            held.append((fn, on_result))  # "in flight": don't call on_result yet

        monkeypatch.setattr(image_cache, "_eviction_in_flight", False)
        import jellytoast.async_io as aio

        monkeypatch.setattr(aio, "run_async", _fake_run_async)

        image_cache._schedule_eviction()
        image_cache._schedule_eviction()  # sweep still "running" → coalesced
        assert calls["n"] == 1
        # Completing the first sweep re-arms scheduling.
        fn, on_result = held[0]
        on_result(None)
        image_cache._schedule_eviction()
        assert calls["n"] == 2


class TestClear:
    def test_clear_wipes_directory(self, qapp, isolated_cache):
        image_cache.put("a", _make_pix())
        image_cache.flush_pending_writes()
        image_cache.put("b", _make_pix())
        image_cache.flush_pending_writes()
        assert len(list(isolated_cache.iterdir())) == 2
        image_cache.clear()
        assert list(isolated_cache.iterdir()) == []

    def test_clear_on_empty_dir_is_safe(self, qapp, isolated_cache):
        image_cache.clear()
        image_cache.clear()

    def test_clear_drains_inflight_write(self, qapp, isolated_cache, monkeypatch):
        """clear() must drain pooled writes BEFORE wiping — otherwise a
        write enqueued just before sign-out lands after the unlink sweep
        and resurrects a stale (possibly cross-user) cover. Regression
        for the async-write race: writes now rename onto disk from a
        worker thread, so clear() can't assume the dir stays empty."""
        import threading

        def slow_run_async(fn, on_error=None):
            # Keep the write pending past clear()'s unlink sweep so the
            # race is forced, not left to chance on a fast PNG encode.
            def runner():
                time.sleep(0.1)
                try:
                    fn()
                except Exception as exc:  # pragma: no cover - defensive
                    if on_error is not None:
                        on_error(exc)

            threading.Thread(target=runner, daemon=True).start()

        monkeypatch.setattr("jellytoast.async_io.run_async", slow_run_async)

        image_cache.put("about-to-sign-out", _make_pix())
        # No manual flush — clear() itself must drain the pending write.
        image_cache.clear()
        assert list(isolated_cache.iterdir()) == []
        # Give any un-drained late write the chance to resurrect a file.
        time.sleep(0.2)
        assert list(isolated_cache.iterdir()) == []
