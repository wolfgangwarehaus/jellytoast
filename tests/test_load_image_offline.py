"""Offline-mode gate on ``load_image_async`` (modules/ui_helpers.py).

A cold cover-load against a server jellytoast can't reach takes 20s to
time out (QNAM transfer-timeout). Multiply by every tile entering the
viewport and an offline launch becomes a slow procession of placeholder
pixmaps. The offline gate short-circuits to the disk cache + missing
sentinel so the same flow finishes in milliseconds.

These tests verify the three branches of that gate and, critically, that
the network layer is never touched while offline.
"""

import pytest
from PySide6.QtGui import QColor, QImage, QPixmap

from modules import image_cache, ui_helpers


@pytest.fixture
def isolated_caches(tmp_path, monkeypatch):
    """Redirect the on-disk cover cache + clear the in-memory tiers so
    each test starts from a blank slate. Without the memory clears, an
    earlier test's cached pixmap would short-circuit the gate before
    it ran."""
    target = tmp_path / "covers"
    target.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(image_cache, "_CACHE_DIR", target)
    monkeypatch.setattr(image_cache, "_puts_since_eviction", 0)
    ui_helpers._image_cache.clear()
    ui_helpers._raw_image_cache.clear()
    yield target
    ui_helpers._image_cache.clear()
    ui_helpers._raw_image_cache.clear()


@pytest.fixture
def fake_qnam(monkeypatch):
    """Replace ``get_qnam()`` with a spy that records every ``.get()``
    call. The offline gate's contract is that the network layer is
    untouched — checking call_count on this spy is how we prove it."""

    class _SpyQNAM:
        def __init__(self):
            self.calls = []

        def get(self, req):
            self.calls.append(req)
            raise AssertionError(
                "QNAM.get() called while offline-mode gate should "
                "have short-circuited"
            )

    spy = _SpyQNAM()
    monkeypatch.setattr(ui_helpers, "get_qnam", lambda: spy)
    return spy


def _make_pix(w: int = 16, h: int = 16, color: str = "#3366ff") -> QPixmap:
    img = QImage(w, h, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(QColor(color))
    return QPixmap.fromImage(img)


class TestOfflineCached:
    def test_cached_pixmap_returned_via_callback(
        self, qapp, isolated_caches, fake_qnam, monkeypatch
    ):
        """Image cached on disk → callback fires with that pixmap,
        QNAM is never touched."""
        from modules import offline as offline_mod

        monkeypatch.setattr(offline_mod, "is_offline_mode", lambda: True)

        cache_key = "album-cached|360x360|r=8"
        image_cache.put(cache_key, _make_pix())

        results: list = []
        ui_helpers.load_image_async(
            "album-cached", "http://example/cover.jpg", 360, 360,
            callback=results.append, rounded_radius=8,
        )

        assert len(results) == 1
        assert results[0] is not None
        assert not results[0].isNull()
        assert fake_qnam.calls == []

    def test_memory_hit_short_circuits_before_gate(
        self, qapp, isolated_caches, fake_qnam, monkeypatch
    ):
        """L1 memory hit predates the offline gate — it should still
        serve from memory without touching the network either way.
        Guards against a refactor that moves the gate above L1 and
        accidentally drops warm in-memory pixmaps."""
        from modules import offline as offline_mod

        monkeypatch.setattr(offline_mod, "is_offline_mode", lambda: True)

        cache_key = "album-hot|256x256|r=0"
        warm = _make_pix(color="#00ff00")
        ui_helpers._image_cache[cache_key] = warm

        results: list = []
        ui_helpers.load_image_async(
            "album-hot", "http://example/cover.jpg", 256, 256,
            callback=results.append,
        )

        assert results == [warm]
        assert fake_qnam.calls == []


class TestOfflineUncached:
    def test_uncached_with_on_error_invokes_error_callback(
        self, qapp, isolated_caches, fake_qnam, monkeypatch
    ):
        """Image not on disk + caller provided ``on_error`` → mirror
        the network-failure path: invoke ``on_error``, do NOT invoke
        ``callback`` with a placeholder. QNAM stays untouched."""
        from modules import offline as offline_mod

        monkeypatch.setattr(offline_mod, "is_offline_mode", lambda: True)

        cb_results: list = []
        err_count = {"n": 0}

        def _on_err():
            err_count["n"] += 1

        ui_helpers.load_image_async(
            "album-missing", "http://example/cover.jpg", 360, 360,
            callback=cb_results.append,
            on_error=_on_err,
            rounded_radius=8,
        )

        assert err_count["n"] == 1
        assert cb_results == []
        assert fake_qnam.calls == []

    def test_uncached_without_on_error_returns_placeholder(
        self, qapp, isolated_caches, fake_qnam, monkeypatch
    ):
        """Image not on disk + no ``on_error`` → legacy callers see a
        placeholder pixmap (matches the existing network-failure
        behavior). QNAM stays untouched."""
        from modules import offline as offline_mod

        monkeypatch.setattr(offline_mod, "is_offline_mode", lambda: True)

        results: list = []
        ui_helpers.load_image_async(
            "album-missing-2", "http://example/cover.jpg", 200, 200,
            callback=results.append,
            rounded_radius=8,
        )

        assert len(results) == 1
        pix = results[0]
        assert pix is not None
        assert not pix.isNull()
        # Placeholder is sized to the request, same as the existing
        # network-failure fallback.
        assert pix.width() == 200 and pix.height() == 200
        assert fake_qnam.calls == []

    def test_uncached_without_radius_returns_unrounded_placeholder(
        self, qapp, isolated_caches, fake_qnam, monkeypatch
    ):
        """Sanity: ``rounded_radius=0`` skips the rounding step in the
        offline placeholder path. Mirrors the same conditional in the
        network-failure path."""
        from modules import offline as offline_mod

        monkeypatch.setattr(offline_mod, "is_offline_mode", lambda: True)

        results: list = []
        ui_helpers.load_image_async(
            "album-missing-3", "http://example/cover.jpg", 64, 64,
            callback=results.append,
            rounded_radius=0,
        )

        assert len(results) == 1
        assert results[0].width() == 64 and results[0].height() == 64
        assert fake_qnam.calls == []


class TestOnlineGateDoesNothing:
    def test_online_uncached_falls_through_to_network(
        self, qapp, isolated_caches, monkeypatch
    ):
        """``is_offline_mode()`` False → the gate is a no-op. The
        request proceeds to the QNAM layer as it does today. We don't
        test the network details (separate concern) — just assert
        that the gate didn't short-circuit and a network request was
        kicked off."""
        from modules import offline as offline_mod

        monkeypatch.setattr(offline_mod, "is_offline_mode", lambda: False)

        gets: list = []

        class _StubReply:
            def __init__(self):
                self._cbs = []

            class _Signal:
                def __init__(self, owner):
                    self._owner = owner

                def connect(self, cb):
                    self._owner._cbs.append(cb)

            @property
            def finished(self):
                return self._Signal(self)

            def deleteLater(self):
                pass

        class _StubQNAM:
            def get(self, req):
                reply = _StubReply()
                gets.append((req, reply))
                return reply

        monkeypatch.setattr(ui_helpers, "get_qnam", lambda: _StubQNAM())

        ui_helpers.load_image_async(
            "album-online", "http://example/cover.jpg", 360, 360,
            callback=lambda _p: None,
            rounded_radius=8,
        )

        # The offline gate did NOT intercept; the network path ran
        # and a QNAM request was created.
        assert len(gets) == 1
