"""The relay records whether a cast device ever fetched a stream token, so a
failed cast can be blamed on reachability (a host firewall) when it didn't."""

from jellytoast import cast_proxy as cp


def test_was_fetched_tracks_device_requests(monkeypatch):
    monkeypatch.setattr(cp, "_lan_ip", lambda: "127.0.0.1")
    proxy = cp.CastProxy()
    try:
        url = proxy.proxy_url("http://100.64.0.1:4533/rest/stream?id=1")
        assert url
        assert proxy.was_fetched(url) is False
        token = url.rsplit("/", 1)[1]
        assert proxy._server.upstream_for(token)
        assert proxy.was_fetched(url) is True
        assert proxy.was_fetched("http://192.168.1.5:4533/rest/stream") is None
        proxy.clear_tokens()
        assert proxy.was_fetched(url) is None
    finally:
        proxy.stop()
