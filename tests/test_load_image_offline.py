"""Offline-mode gate on ``load_image_async`` (jellytoast/ui_helpers.py).

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

from jellytoast import image_cache, ui_helpers


def _drain(qapp):
    """The disk tiers of load_image_async resolve on the shared pool —
    wait for the lookup, then deliver its queued GUI callback. Two
    rounds: the first callback can enqueue a follow-up pooled write."""
    from jellytoast.async_io import get_thread_pool

    for _ in range(2):
        get_thread_pool().waitForDone(2000)
        qapp.processEvents()


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

    def _clear_loader_state():
        ui_helpers._image_cache.clear()
        ui_helpers._raw_image_cache.clear()
        # The loader's TRANSPORT state leaks across tests too, and either
        # leak silently swallows a load without it ever reaching QNAM:
        #   • an _inflight_subscribers residue for the same cache_key
        #     coalesces the new call onto a dead in-flight entry;
        #   • a leaked _gated_in_flight counter >= _GATED_MAX_INFLIGHT parks
        #     the request in a deferred queue nothing will ever drain.
        # Concretely: test_large_library_covers exercises the gate, and in
        # random suite order its leftovers made
        # test_online_uncached_falls_through_to_network see zero QNAM gets.
        ui_helpers._inflight_subscribers.clear()
        ui_helpers._pending_replies.clear()
        ui_helpers._deferred_normal.clear()
        ui_helpers._deferred_low.clear()
        ui_helpers._gated_in_flight = 0

    _clear_loader_state()
    yield target
    _clear_loader_state()


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
                "QNAM.get() called while offline-mode gate should have short-circuited"
            )

    spy = _SpyQNAM()
    monkeypatch.setattr(ui_helpers, "get_qnam", lambda: spy)
    return spy


def _make_pix(w: int = 16, h: int = 16, color: str = "#3366ff") -> QPixmap:
    img = QImage(w, h, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(QColor(color))
    return QPixmap.fromImage(img)


def _png_bytes(w: int = 8, h: int = 8):
    """A QByteArray of valid PNG data that QImage.loadFromData accepts."""
    from PySide6.QtCore import QBuffer, QByteArray

    img = QImage(w, h, QImage.Format.Format_RGB32)
    img.fill(QColor("#ff8800"))
    ba = QByteArray()
    buf = QBuffer(ba)
    buf.open(QBuffer.OpenModeFlag.WriteOnly)
    assert img.save(buf, "PNG")
    return ba


class _FakeReply:
    """A QNetworkReply stand-in for driving _on_image_reply_finished."""

    def __init__(self, data, error):
        self._data = data
        self._error = error

    def error(self):
        return self._error

    def readAll(self):
        return self._data

    def deleteLater(self):
        pass


class TestImageReplyFanoutGuard:
    """Many widgets coalesce onto ONE in-flight image reply. A single
    subscriber raising (canonically a deleted-widget RuntimeError when the
    widget was torn down mid-fetch) must NOT abort _on_image_reply_finished's
    fan-out loop and starve the remaining subscribers of their pixmap.
    """

    def _drive(self, cache_key, waiters, data, error):
        reply = _FakeReply(data, error)
        sem_key = cache_key.split("|")[0]
        ui_helpers._pending_replies[reply] = (
            cache_key,
            sem_key,
            8,
            8,
            0,
            "normal",
        )
        ui_helpers._inflight_subscribers[cache_key] = list(waiters)
        # Must NOT raise even though one subscriber does.
        ui_helpers._on_image_reply_finished(reply)

    def test_success_fanout_survives_a_raising_subscriber(self, qapp, isolated_caches):
        from PySide6.QtNetwork import QNetworkReply

        got = []

        def bad(_pix):
            raise RuntimeError("Internal C++ object already deleted")

        def good(_pix):
            got.append(_pix)

        self._drive(
            "fanout-ok|8x8|r=0",
            [(bad, None), (good, None), (good, None)],
            _png_bytes(),
            QNetworkReply.NetworkError.NoError,
        )
        # Both non-raising subscribers still received the decoded pixmap.
        assert len(got) == 2

    def test_failure_fanout_survives_a_raising_on_error(self, qapp, isolated_caches):
        from PySide6.QtCore import QByteArray
        from PySide6.QtNetwork import QNetworkReply

        fired = []

        def bad_err():
            raise RuntimeError("Internal C++ object already deleted")

        def good_err():
            fired.append(1)

        self._drive(
            "fanout-err|8x8|r=0",
            [
                (lambda _p: None, bad_err),
                (lambda _p: None, good_err),
                (lambda _p: None, good_err),
            ],
            QByteArray(b""),
            QNetworkReply.NetworkError.HostNotFoundError,
        )
        # Both later on_error callbacks still fired despite the first raising.
        assert len(fired) == 2

    def test_failure_placeholder_fanout_survives_a_raising_callback(
        self, qapp, isolated_caches
    ):
        from PySide6.QtCore import QByteArray
        from PySide6.QtNetwork import QNetworkReply

        got = []

        def bad(_pix):
            raise RuntimeError("Internal C++ object already deleted")

        def good(_pix):
            got.append(_pix)

        # Legacy waiters (no on_error) get the placeholder pixmap.
        self._drive(
            "fanout-ph|8x8|r=0",
            [(bad, None), (good, None), (good, None)],
            QByteArray(b""),
            QNetworkReply.NetworkError.HostNotFoundError,
        )
        assert len(got) == 2


class TestOfflineCached:
    def test_cached_pixmap_returned_via_callback(
        self, qapp, isolated_caches, fake_qnam, monkeypatch
    ):
        """Image cached on disk → callback fires with that pixmap,
        QNAM is never touched."""
        from jellytoast import offline as offline_mod

        monkeypatch.setattr(offline_mod, "is_offline_mode", lambda: True)

        cache_key = "album-cached|360x360|r=8"
        image_cache.put(cache_key, _make_pix())

        results: list = []
        ui_helpers.load_image_async(
            "album-cached",
            "http://example/cover.jpg",
            360,
            360,
            callback=results.append,
            rounded_radius=8,
        )
        _drain(qapp)

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
        from jellytoast import offline as offline_mod

        monkeypatch.setattr(offline_mod, "is_offline_mode", lambda: True)

        cache_key = "album-hot|256x256|r=0"
        warm = _make_pix(color="#00ff00")
        ui_helpers._image_cache[cache_key] = warm

        results: list = []
        ui_helpers.load_image_async(
            "album-hot",
            "http://example/cover.jpg",
            256,
            256,
            callback=results.append,
        )
        _drain(qapp)

        assert results == [warm]
        assert fake_qnam.calls == []


class TestOfflineUncached:
    def test_uncached_with_on_error_invokes_error_callback(
        self, qapp, isolated_caches, fake_qnam, monkeypatch
    ):
        """Image not on disk + caller provided ``on_error`` → mirror
        the network-failure path: invoke ``on_error``, do NOT invoke
        ``callback`` with a placeholder. QNAM stays untouched."""
        from jellytoast import offline as offline_mod

        monkeypatch.setattr(offline_mod, "is_offline_mode", lambda: True)

        cb_results: list = []
        err_count = {"n": 0}

        def _on_err():
            err_count["n"] += 1

        ui_helpers.load_image_async(
            "album-missing",
            "http://example/cover.jpg",
            360,
            360,
            callback=cb_results.append,
            on_error=_on_err,
            rounded_radius=8,
        )
        _drain(qapp)

        assert err_count["n"] == 1
        assert cb_results == []
        assert fake_qnam.calls == []

    def test_uncached_without_on_error_returns_placeholder(
        self, qapp, isolated_caches, fake_qnam, monkeypatch
    ):
        """Image not on disk + no ``on_error`` → legacy callers see a
        placeholder pixmap (matches the existing network-failure
        behavior). QNAM stays untouched."""
        from jellytoast import offline as offline_mod

        monkeypatch.setattr(offline_mod, "is_offline_mode", lambda: True)

        results: list = []
        ui_helpers.load_image_async(
            "album-missing-2",
            "http://example/cover.jpg",
            200,
            200,
            callback=results.append,
            rounded_radius=8,
        )
        _drain(qapp)

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
        from jellytoast import offline as offline_mod

        monkeypatch.setattr(offline_mod, "is_offline_mode", lambda: True)

        results: list = []
        ui_helpers.load_image_async(
            "album-missing-3",
            "http://example/cover.jpg",
            64,
            64,
            callback=results.append,
            rounded_radius=0,
        )
        _drain(qapp)

        assert len(results) == 1
        assert results[0].width() == 64 and results[0].height() == 64
        assert fake_qnam.calls == []


class TestOnlineGateDoesNothing:
    def test_online_uncached_falls_through_to_network(self, qapp, isolated_caches, monkeypatch):
        """``is_offline_mode()`` False → the gate is a no-op. The
        request proceeds to the QNAM layer as it does today. We don't
        test the network details (separate concern) — just assert
        that the gate didn't short-circuit and a network request was
        kicked off."""
        from jellytoast import offline as offline_mod

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
            "album-online",
            "http://example/cover.jpg",
            360,
            360,
            callback=lambda _p: None,
            rounded_radius=8,
        )
        _drain(qapp)

        # The offline gate did NOT intercept; the network path ran
        # and a QNAM request was created.
        assert len(gets) == 1
