"""First-launch album art: what a new user sees against a cold server.

Measured 2026-10-24 against the public Navidrome demo: a cold server can take
longer than the transfer timeout to generate each resized thumbnail, so the
first grid sat blank ~30 s per stuck request. These pin the fixes: bucketed
request sizes, a short wait for a sized cover before falling back to the
original, "no art" (404) remembered instead of retried, and the phone UI
retrying a failed cover instead of giving up for the session.

The pipeline tests run against a real local HTTP server so Qt's own
transfer-timeout behaviour is what's exercised, not a mock of it.
"""

from __future__ import annotations

import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from PySide6.QtCore import QBuffer, QByteArray, QEventLoop, QIODevice, QTimer
from PySide6.QtGui import QColor, QImage

from jellytoast import ui_helpers as uih
from jellytoast.providers.base import cover_request_px

# ── Request sizes ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "shown, asked",
    [(64, 600), (333, 600), (540, 600), (600, 600), (601, 1200), (1024, 1200), (4000, 1200),
     (None, 600), ("x", 600)],
)
def test_cover_sizes_are_bucketed(shown, asked):
    assert cover_request_px(shown) == asked


def test_both_providers_ask_for_bucketed_sizes():
    from urllib.parse import parse_qs, urlparse

    from jellytoast.jellyfin_api import JellyfinAPI
    from jellytoast.providers.subsonic import SubsonicProvider

    sub = SubsonicProvider()
    sub._username, sub._password, sub._server_url = "u", "p", "http://nd.local"
    q = parse_qs(urlparse(sub.get_image_url("al-1", "Primary", 540)).query)
    assert q["size"] == ["600"]

    jf = JellyfinAPI.__new__(JellyfinAPI)
    jf.server_url, jf.token = "http://jf.local", "t"
    q = parse_qs(urlparse(jf.get_image_url("abc", "Primary", 333)).query)
    assert q["width"] == ["600"]


# ── The pipeline against a real (local) server ──────────────────────────


def _png() -> bytes:
    img = QImage(32, 32, QImage.Format.Format_ARGB32)
    img.fill(QColor("#c04040"))
    ba = QByteArray()
    buf = QBuffer(ba)
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    img.save(buf, "PNG")
    return bytes(ba)


_PNG = None


class _Handler(BaseHTTPRequestHandler):
    hits: list = []

    def log_message(self, *a):
        pass

    def do_GET(self):  # noqa: N802
        _Handler.hits.append(self.path)
        if self.path.startswith("/missing"):
            self.send_response(404)
            self.end_headers()
            return
        if "size=" in self.path:
            # A cold server "generating" the thumbnail: nothing for a while.
            time.sleep(1.5)
        try:
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(_PNG)))
            self.end_headers()
            self.wfile.write(_PNG)
        except (BrokenPipeError, ConnectionResetError):
            pass  # the client gave up on the slow resize — expected


@pytest.fixture
def server(qapp, monkeypatch, tmp_path):
    global _PNG
    _PNG = _png()
    from jellytoast import image_cache, offline

    monkeypatch.setattr(offline, "is_offline_mode", lambda: False)
    monkeypatch.setattr(image_cache, "_CACHE_DIR", tmp_path)
    # Short allowance so the test doesn't wait 8 s for the real one.
    monkeypatch.setattr(uih, "_SIZED_TRANSFER_TIMEOUT_MS", 400)
    monkeypatch.setattr(uih, "_resize_timeouts", 0)
    monkeypatch.setattr(uih, "_prefer_original_covers", False)
    uih.clear_image_caches()
    _Handler.hits = []
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    uih.clear_image_caches()


def _load(key, url, timeout_s=8.0):
    """Run load_image_async to completion; returns ('pix', w) or ('err', None)."""
    out = []
    loop = QEventLoop()
    uih.load_image_async(
        key, url, 40, 40,
        lambda pix: (out.append(("pix", pix.width())), loop.quit()),
        on_error=lambda: (out.append(("err", None)), loop.quit()),
    )
    if not out:
        QTimer.singleShot(int(timeout_s * 1000), loop.quit)
        loop.exec()
    return out[0] if out else ("timeout", None)


def test_slow_resize_falls_back_to_the_original_fast(server):
    t0 = time.monotonic()
    got = _load("al-slow|t", f"{server}/cover?id=1&size=600")
    took = time.monotonic() - t0
    assert got[0] == "pix"
    # Gave up on the 1.5 s "resize" after the short allowance, not 30 s.
    assert took < 1.4
    assert any("size=" not in h for h in _Handler.hits)  # fetched the original


def test_two_slow_resizes_latch_the_session_onto_originals(server):
    _load("al-a|t", f"{server}/cover?id=a&size=600")
    _load("al-b|t", f"{server}/cover?id=b&size=600")
    assert uih._prefer_original_covers is True
    _Handler.hits.clear()
    t0 = time.monotonic()
    assert _load("al-c|t", f"{server}/cover?id=c&size=600")[0] == "pix"
    assert time.monotonic() - t0 < 1.0
    assert all("size=" not in h for h in _Handler.hits)  # straight to the original


def test_missing_art_is_remembered_not_retried(server):
    assert _load("al-none|t", f"{server}/missing?id=9&size=600") == ("err", None)
    assert uih.image_known_missing("al-none|othertile")
    _Handler.hits.clear()
    assert _load("al-none|t2", f"{server}/missing?id=9&size=600") == ("err", None)
    assert _Handler.hits == []  # answered without the network


# ── Desktop grid: items with no image aren't requested ──────────────────


def test_grid_skips_items_that_report_no_image(qapp, monkeypatch):
    from jellytoast.library_grid import LibraryGrid

    g = LibraryGrid(kind="album")
    asked = []
    monkeypatch.setattr(g.api, "get_image_url", lambda *a, **k: asked.append(a[0]) or "http://x/c")
    monkeypatch.setattr(uih, "load_image_async", lambda *a, **k: None)
    import jellytoast.library_grid as lg

    monkeypatch.setattr(lg, "load_image_async", lambda *a, **k: None)
    g._model.set_items([
        {"Id": "has", "Name": "A", "ImageTags": {"Primary": "t1"}},
        {"Id": "none", "Name": "B", "ImageTags": {}},
        {"Id": "legacy", "Name": "C"},  # no ImageTags field at all: still asked
    ])
    for r in range(3):
        g._fire_cover_load(r)
    assert asked == ["has", "legacy"]
    assert g._cover_retries.get(1) == g.COVER_RETRY_LIMIT  # benched, not spinning
    g.deleteLater()


# ── Phone UI: a failed cover is retried ─────────────────────────────────


def test_phone_cover_retries_after_a_failure(qapp, monkeypatch):
    from jellytoast import offline
    from jellytoast.quick import covers as covers_mod

    monkeypatch.setattr(offline, "is_offline_mode", lambda: False)
    monkeypatch.setattr(covers_mod, "_RETRY_DELAYS_S", (0.05, 0.05, 0.05))
    attempts = []

    def fake_load(key, url, w, h, cb, rounded_radius=0, on_error=None, priority="normal"):
        attempts.append(key)
        if len(attempts) < 3:
            on_error()
        else:
            from PySide6.QtGui import QPixmap

            p = QPixmap(8, 8)
            p.fill(QColor("red"))
            cb(p)

    monkeypatch.setattr(uih, "load_image_async", fake_load)

    class _Prov:
        def get_image_url(self, *a, **k):
            return "http://x/c"

    monkeypatch.setattr("jellytoast.providers.get_provider", lambda: _Prov())
    c = covers_mod.Covers()
    ready = []
    c.ready.connect(ready.append)
    assert c.request("al-1/300/8") is False
    deadline = time.monotonic() + 3
    while not ready and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.01)
    assert ready == ["al-1/300/8"]
    assert len(attempts) == 3


def test_phone_cover_with_no_art_is_not_retried(qapp, monkeypatch):
    from jellytoast import offline
    from jellytoast.quick import covers as covers_mod

    monkeypatch.setattr(offline, "is_offline_mode", lambda: False)
    monkeypatch.setattr(covers_mod, "_RETRY_DELAYS_S", (0.05,))
    monkeypatch.setattr(uih, "_known_no_art", {"al-2"})
    attempts = []
    monkeypatch.setattr(
        uih, "load_image_async", lambda key, *a, on_error=None, **k: (attempts.append(key), on_error())
    )

    class _Prov:
        def get_image_url(self, *a, **k):
            return "http://x/c"

    monkeypatch.setattr("jellytoast.providers.get_provider", lambda: _Prov())
    c = covers_mod.Covers()
    c.request("al-2/300/8")
    t0 = time.monotonic()
    while time.monotonic() - t0 < 0.3:
        qapp.processEvents()
        time.sleep(0.01)
    assert len(attempts) == 1
