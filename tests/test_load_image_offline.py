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
        # The adaptive slow-resize latch is a module global too — a timeout in
        # one test must not leave the whole suite fetching originals.
        ui_helpers._resize_timeouts = 0
        ui_helpers._prefer_original_covers = False

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

    def attribute(self, _attr):
        return 200  # HttpStatusCodeAttribute — value doesn't matter here

    def deleteLater(self):
        pass


class TestImageReplyFanoutGuard:
    """Many widgets coalesce onto ONE in-flight image reply. A single
    subscriber raising (canonically a deleted-widget RuntimeError when the
    widget was torn down mid-fetch) must NOT abort _on_image_reply_finished's
    fan-out loop and starve the remaining subscribers of their pixmap.
    """

    @pytest.fixture(autouse=True)
    def _inline_pool(self, monkeypatch):
        """Decoding moved to the worker pool (it was janking the GUI
        thread on full-size originals), so the success fan-out is now
        asynchronous. Run the pool inline to keep these assertions
        synchronous — the fan-out logic under test is unchanged."""
        import jellytoast.async_io as aio

        def _inline(fn, *a, on_result=None, on_error=None, **kw):
            try:
                res = fn(*a)
            except Exception as e:  # noqa: BLE001
                if on_error is not None:
                    on_error(e)
                return
            if on_result is not None:
                on_result(res)

        monkeypatch.setattr(aio, "run_async", _inline)

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
            "http://srv/rest/getCoverArt?id=x",  # url (no resize param)
            True,  # resize_fallback_done — go straight to the fan-out under test
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

    def test_failure_placeholder_fanout_survives_a_raising_callback(self, qapp, isolated_caches):
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


# ── Cover resize-fallback + URL helpers (#cover-stall) ──────────────────────


class TestResizeParamHelpers:
    def test_strips_subsonic_size(self):
        out = ui_helpers._strip_resize_params("http://s/rest/getCoverArt?id=al-1&size=400")
        assert out is not None and "size=" not in out and "id=al-1" in out

    def test_strips_jellyfin_maxwidth(self):
        out = ui_helpers._strip_resize_params("http://s/Items/x/Images/Primary?maxWidth=300&tag=t")
        assert out is not None and "maxWidth" not in out and "tag=t" in out

    def test_no_resize_params_returns_none(self):
        assert ui_helpers._strip_resize_params("http://s/rest/getCoverArt?id=al-1") is None

    def test_redact_drops_subsonic_auth_keeps_id(self):
        red = ui_helpers._redact_url("http://s/rest/getCoverArt?id=al-1&u=bob&t=abc&s=xy&size=400")
        assert "u=bob" not in red and "t=abc" not in red and "s=xy" not in red
        assert "id=al-1" in red and "size=400" in red  # non-secret bits kept for debugging


class TestCoverResizeFallback:
    """A sized cover request that FAILS retries ONCE for the original asset
    (resize params stripped) before giving up — the recurring "loads some
    art then stops" is a slow/quirky server-side resize."""

    def _ctx(self, url, fb_done):
        return ("ck", "ck", 100, 100, 0, "normal", url, fb_done)

    def test_failed_sized_request_retries_without_size(self, qapp, isolated_caches, monkeypatch):
        from PySide6.QtNetwork import QNetworkReply

        fired = []
        monkeypatch.setattr(
            ui_helpers,
            "_fire_image_request",
            lambda *a, **k: fired.append((a, k)),
        )
        reply = _FakeReply(b"", QNetworkReply.NetworkError.TimeoutError)
        ui_helpers._pending_replies[reply] = self._ctx(
            "http://s/rest/getCoverArt?id=al-1&size=400", False
        )
        waiters = [(lambda _p: None, None)]
        ui_helpers._inflight_subscribers["ck"] = list(waiters)

        ui_helpers._on_image_reply_finished(reply)

        # Retried exactly once, without the size param, marked fallback-done.
        assert len(fired) == 1
        retry_url = fired[0][0][2]
        assert "size=" not in retry_url and "id=al-1" in retry_url
        assert fired[0][1].get("resize_fallback_done") is True
        # Subscribers preserved for the retry — NOT fanned out yet.
        assert "ck" in ui_helpers._inflight_subscribers

    def test_no_second_retry_after_fallback(self, qapp, isolated_caches, monkeypatch):
        from PySide6.QtNetwork import QNetworkReply

        fired = []
        monkeypatch.setattr(ui_helpers, "_fire_image_request", lambda *a, **k: fired.append(a))
        reply = _FakeReply(b"", QNetworkReply.NetworkError.TimeoutError)
        # fb_done=True → the already-stripped retry failed; must NOT loop.
        ui_helpers._pending_replies[reply] = self._ctx("http://s/rest/getCoverArt?id=al-1", True)
        errs = []
        ui_helpers._inflight_subscribers["ck"] = [(lambda _p: None, lambda: errs.append(1))]

        ui_helpers._on_image_reply_finished(reply)

        assert fired == []  # no further retry
        assert errs == [1]  # terminal failure fanned out to on_error
        assert "ck" not in ui_helpers._inflight_subscribers  # popped/finished


class _CapQNAM:
    """A QNAM spy that records every request URL and returns a reply whose
    finished signal is a no-op (the test drives the finish handler by hand)."""

    def __init__(self, sink):
        self._sink = sink

    class _Rep:
        class _Sig:
            def connect(self, _cb):
                pass

        finished = _Sig()

    def get(self, req):
        self._sink.append(req.url().toString())
        return _CapQNAM._Rep()


class TestSlowResizeAdaptation:
    """After a couple of resize TIMEOUTs, the session latches onto original-
    fetch so we stop paying the timeout per cover (#cover-stall — verified
    against a CPU-throttled 5,200-album Navidrome)."""

    def _timeout_a_sized_cover(self, cache_key="ck"):
        from PySide6.QtNetwork import QNetworkReply

        reply = _FakeReply(b"", QNetworkReply.NetworkError.TimeoutError)
        ui_helpers._pending_replies[reply] = (
            cache_key,
            cache_key,
            100,
            100,
            0,
            "high",  # high → no gate accounting
            "http://s/rest/getCoverArt?id=x&size=400",
            False,
        )
        ui_helpers._inflight_subscribers[cache_key] = [(lambda _p: None, None)]
        ui_helpers._on_image_reply_finished(reply)
        ui_helpers._inflight_subscribers.pop(cache_key, None)

    def test_latches_after_trip_and_strips_size(self, qapp, isolated_caches, monkeypatch):
        urls: list = []
        monkeypatch.setattr(ui_helpers, "get_qnam", lambda: _CapQNAM(urls))

        assert ui_helpers._prefer_original_covers is False
        for i in range(ui_helpers._RESIZE_TIMEOUT_TRIP):
            self._timeout_a_sized_cover(f"ck{i}")
        assert ui_helpers._prefer_original_covers is True

        # Fresh fire now skips the sized request entirely — straight to original.
        urls.clear()
        ui_helpers._fire_image_request(
            "ck-new",
            "ck-new",
            "http://s/rest/getCoverArt?id=y&size=400",
            100,
            100,
            0,
            "normal",
        )
        assert urls and "size=" not in urls[-1] and "id=y" in urls[-1]

    def test_healthy_server_never_latches(self, qapp, isolated_caches, monkeypatch):
        urls: list = []
        monkeypatch.setattr(ui_helpers, "get_qnam", lambda: _CapQNAM(urls))
        # A single timeout (below the trip) must NOT latch the session.
        self._timeout_a_sized_cover()
        assert ui_helpers._resize_timeouts == 1
        assert ui_helpers._prefer_original_covers is False
