"""Tests for the cast proxy's in-memory token map.

Only the parts that don't need a real cast device: token registration,
resolution, and the eviction policy. The ``_ProxyServer`` is bound to an
ephemeral localhost port (``("127.0.0.1", 0)``) so the test is fast and
firewall-independent; the HTTP handler / streaming path needs a real
renderer and is out of scope here.
"""

import pytest

from modules.cast_proxy import _ProxyServer


@pytest.fixture
def server():
    s = _ProxyServer(("127.0.0.1", 0))  # ephemeral port, localhost only
    yield s
    s.server_close()


class TestTokenMap:
    def test_register_and_resolve(self, server):
        t = server.register("http://up/stream")
        assert server.upstream_for(t) == "http://up/stream"

    def test_unknown_token_is_none(self, server):
        assert server.upstream_for("nope") is None

    def test_eviction_is_lru_not_fifo(self, server):
        """#294: upstream_for promotes on access, so an actively-streamed
        token (re-requested on every buffer refill / seek) can't age out
        and get FIFO-evicted mid-playback. With the old non-promoting
        read, ``first`` would be the oldest entry and evicted."""
        first = server.register("http://up/0")
        for i in range(1, 256):  # fill to the 256-entry cap
            server.register(f"http://up/{i}")

        # Touch `first` so LRU promotes it off the eviction front.
        assert server.upstream_for(first) == "http://up/0"

        # One more registration trips the cap and evicts the LRU victim —
        # which must be the next-oldest, NOT the just-accessed `first`.
        server.register("http://up/new")
        assert server.upstream_for(first) == "http://up/0"
