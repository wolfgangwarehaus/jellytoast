"""Disk image cache (modules/image_cache.py) invariants.

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

from modules import image_cache


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
        out = image_cache.get("album-1|360x360|r=8")
        assert out is not None
        assert not out.isNull()
        assert out.width() == 16 and out.height() == 16

    def test_put_skips_null_pixmap(self, qapp, isolated_cache):
        image_cache.put("empty", QPixmap())
        assert image_cache.get("empty") is None
        assert not any(isolated_cache.iterdir())

    def test_filename_is_hash_not_raw_key(self, qapp, isolated_cache):
        """Cache keys can contain slashes, pipes, and other filesystem-
        unsafe characters — they must be hashed, not used as filenames."""
        image_cache.put("a/b|c|x=1", _make_pix())
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
            time.sleep(0.01)
        image_cache._evict_if_over_cap()
        # Oldest entries should be gone; newer ones remain.
        assert image_cache.get("k-0") is None
        assert image_cache.get("k-4") is not None

    def test_no_eviction_under_cap(self, qapp, isolated_cache, monkeypatch):
        monkeypatch.setattr(image_cache, "_DISK_CACHE_MAX_BYTES", 10 * 1024 * 1024)
        for i in range(3):
            image_cache.put(f"k-{i}", _make_pix())
        image_cache._evict_if_over_cap()
        for i in range(3):
            assert image_cache.get(f"k-{i}") is not None


class TestClear:
    def test_clear_wipes_directory(self, qapp, isolated_cache):
        image_cache.put("a", _make_pix())
        image_cache.put("b", _make_pix())
        assert len(list(isolated_cache.iterdir())) == 2
        image_cache.clear()
        assert list(isolated_cache.iterdir()) == []

    def test_clear_on_empty_dir_is_safe(self, qapp, isolated_cache):
        image_cache.clear()
        image_cache.clear()
