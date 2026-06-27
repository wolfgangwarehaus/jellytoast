"""Tests for the DLNA / UPnP cast backend (``jellytoast/cast/dlna.py``).

Covers:

- DIDL-Lite builder: shape, mandatory ``upnp:class``, XML escaping,
  cover-URL cap (Bose ≤ 4 kB), conditional fields, duration / size
  formatting, protocolInfo profile-name mapping.
- Codec / 714-fallback decision tree: native push, force-transcode
  short-circuit, transcode-retry on 701 / 714, non-retryable codes.
- SSDP discovery dedup: USN parsing, in-flight dedup, malformed
  responses, location → host/port parsing.
- Asyncio loop thread: start / stop / re-entry idempotency.
- DlnaController push flow: mocked DmrDevice with happy-path push,
  714 retry that triggers the provider transcode callback, retry with
  no callback (silent fail), transport-control dispatch (pause /
  resume / stop / seek / volume), polling state capture.
- User-Agent overrides: pattern match, missing setting.

No live network — every SSDP response and SOAP call is mocked. The
``DmrDevice`` is replaced with a ``FakeDmrDevice`` that records every
push + raises spec-style errors on demand. The asyncio-loop thread
test starts a real loop on a daemon thread so the controller can
``run_coroutine_threadsafe`` against it; that's the only "live" piece.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List, Optional

import pytest

from jellytoast.cast import dlna as _dlna
from jellytoast.cast.dlna import (
    SSDP_ST_MEDIA_RENDERER,
    DlnaController,
    DlnaDevice,
    TrackMetadata,
    _container_from_mime,
    _DlnaLoopThread,
    _format_duration,
    _meta_with_mime,
    _parse_udn_from_usn,
    _protocol_info_for,
    _td_to_sec,
    _truncate_cover_url,
    _ua_for_device,
    build_didl_lite,
    decide_push_format,
    decide_retry_after_error,
    dedupe_search_response,
    get_dlna_controller,
    is_available,
    parse_host_from_location,
)

# ── DIDL-Lite builder ───────────────────────────────────────────────────────


class TestDidlLiteBuilder:
    """``build_didl_lite`` shape + invariants."""

    def _meta(self, **kwargs) -> TrackMetadata:
        defaults: Dict[str, Any] = dict(
            item_id="jt-1",
            title="Idioteque",
            artist="Radiohead",
            album="Kid A",
            album_artist="Radiohead",
            track_number=8,
            duration_sec=309.0,
            mime="audio/flac",
            size_bytes=48217600,
            cover_url="http://127.0.0.1:8943/s/COVER/cover.jpg",
        )
        defaults.update(kwargs)
        return TrackMetadata(**defaults)

    def test_emits_didl_lite_root_with_namespaces(self):
        doc = build_didl_lite(self._meta(), "http://x/track.flac")
        assert doc.startswith("<DIDL-Lite")
        assert 'xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/"' in doc
        assert "xmlns:dc=" in doc
        assert "xmlns:upnp=" in doc
        assert "xmlns:dlna=" in doc

    def test_mandatory_upnp_class_emitted(self):
        # Sony Bravia / Yamaha throw 714 without this; spec says
        # ``upnp:class`` is required on every DIDL item.
        doc = build_didl_lite(self._meta(), "http://x")
        assert "<upnp:class>object.item.audioItem.musicTrack</upnp:class>" in doc

    def test_title_and_artist_present(self):
        doc = build_didl_lite(self._meta(), "http://x")
        assert "<dc:title>Idioteque</dc:title>" in doc
        assert "<dc:creator>Radiohead</dc:creator>" in doc
        assert "<upnp:artist>Radiohead</upnp:artist>" in doc

    def test_album_track_and_cover_present(self):
        doc = build_didl_lite(self._meta(), "http://x")
        assert "<upnp:album>Kid A</upnp:album>" in doc
        assert "<upnp:originalTrackNumber>8</upnp:originalTrackNumber>" in doc
        assert 'dlna:profileID="JPEG_TN"' in doc
        assert "cover.jpg" in doc

    def test_cover_omitted_when_empty(self):
        doc = build_didl_lite(self._meta(cover_url=""), "http://x")
        assert "albumArtURI" not in doc

    def test_track_number_zero_is_omitted(self):
        doc = build_didl_lite(self._meta(track_number=0), "http://x")
        assert "originalTrackNumber" not in doc

    def test_album_omitted_when_empty(self):
        doc = build_didl_lite(self._meta(album=""), "http://x")
        # The ``xmlns:upnp=`` declaration carries the substring; assert
        # on the actual element tag, not a bare substring.
        assert "<upnp:album>" not in doc

    def test_album_artist_role_emitted_when_distinct(self):
        doc = build_didl_lite(
            self._meta(artist="Thom Yorke", album_artist="Radiohead"),
            "http://x",
        )
        assert '<upnp:artist role="AlbumArtist">Radiohead</upnp:artist>' in doc

    def test_album_artist_not_duplicated_when_equal_to_artist(self):
        doc = build_didl_lite(self._meta(), "http://x")
        assert 'role="AlbumArtist"' not in doc

    def test_xml_escapes_title_with_special_chars(self):
        # & < > " in title must not break the DIDL XML parse.
        m = self._meta(title='A & B <c> "d"')
        doc = build_didl_lite(m, "http://x")
        assert "&amp;" in doc
        assert "&lt;c&gt;" in doc
        # html.escape with quote=False leaves ASCII " alone inside text;
        # asserts on the &amp; case which is the bug-classic.
        assert "A & B" not in doc  # raw '&' would mean unescaped

    def test_xml_escapes_stream_url_with_query_string(self):
        # A real Subsonic URL has &salt= etc.; the URL goes in element
        # text, so the & must be escaped.
        doc = build_didl_lite(
            self._meta(),
            "http://server/stream?u=alice&p=enc:abc",
        )
        assert "&amp;" in doc
        assert "p=enc:abc" in doc

    def test_size_attribute_emitted_when_known(self):
        doc = build_didl_lite(self._meta(size_bytes=12345), "http://x")
        assert 'size="12345"' in doc

    def test_size_attribute_omitted_when_zero(self):
        doc = build_didl_lite(self._meta(size_bytes=0), "http://x")
        assert "size=" not in doc

    def test_protocol_info_uses_flac_dlna_profile(self):
        doc = build_didl_lite(self._meta(mime="audio/flac"), "http://x")
        assert "audio/flac:DLNA.ORG_PN=FLAC" in doc

    def test_protocol_info_uses_mp3_dlna_profile(self):
        doc = build_didl_lite(self._meta(mime="audio/mpeg"), "http://x")
        assert "audio/mpeg:DLNA.ORG_PN=MP3" in doc

    def test_protocol_info_omits_profile_for_unknown_mime(self):
        # Ogg / WebM have no profile in our table — emit MIME only.
        doc = build_didl_lite(self._meta(mime="audio/ogg"), "http://x")
        assert "audio/ogg" in doc
        assert "DLNA.ORG_PN" not in doc

    def test_duration_format_zero(self):
        doc = build_didl_lite(self._meta(duration_sec=0), "http://x")
        assert 'duration="0:00:00.000"' in doc

    def test_duration_format_with_ms(self):
        doc = build_didl_lite(self._meta(duration_sec=309.5), "http://x")
        assert 'duration="0:05:09.500"' in doc

    def test_duration_format_over_hour(self):
        doc = build_didl_lite(self._meta(duration_sec=3661.0), "http://x")
        assert 'duration="1:01:01.000"' in doc

    def test_cover_truncated_when_too_long_and_falls_back_to_no_cover(self):
        # > 200 chars → drop the cover entirely (Bose-friendly).
        long_url = "http://srv/" + ("x" * 250)
        doc = build_didl_lite(self._meta(cover_url=long_url), "http://x")
        assert "albumArtURI" not in doc

    def test_didl_size_under_4kb_for_typical_payload(self):
        doc = build_didl_lite(self._meta(), "http://x/track.flac")
        # Bose SoundTouch refuses DIDL > 4 kB; typical track must fit.
        assert len(doc.encode("utf-8")) < 4096

    def test_didl_falls_back_to_minimal_when_oversized(self):
        # Force the oversize fallback by stuffing a huge title.
        m = self._meta(title="X" * 5000)
        doc = build_didl_lite(m, "http://x")
        # Fallback keeps title + class + res only.
        assert "<dc:title>" in doc
        assert "<upnp:class>" in doc
        assert "albumArtURI" not in doc
        assert "upnp:album>" not in doc

    def test_default_item_id_when_caller_omits(self):
        m = self._meta(item_id="")
        doc = build_didl_lite(m, "http://x")
        assert 'id="jt-1"' in doc

    def test_item_id_with_special_chars_is_attr_escaped(self):
        m = self._meta(item_id='a"b')
        doc = build_didl_lite(m, "http://x")
        assert "&quot;" in doc

    def test_title_falls_back_to_unknown(self):
        m = self._meta(title="")
        doc = build_didl_lite(m, "http://x")
        assert "<dc:title>Unknown</dc:title>" in doc

    def test_seek_op_flag_advertised(self):
        # DLNA.ORG_OP=01 = seekable via Range — needed so the renderer
        # offers a position bar.
        doc = build_didl_lite(self._meta(), "http://x")
        assert "DLNA.ORG_OP=01" in doc

    def test_doc_has_single_res_element(self):
        # Bose-friendly: only one <res>, no <desc> bloat.
        doc = build_didl_lite(self._meta(), "http://x")
        assert doc.count("<res ") == 1
        assert "<desc" not in doc


# ── Codec / 714-fallback decision tree ──────────────────────────────────────


class TestDecidePushFormat:
    def test_native_flac_no_transcode(self):
        d = decide_push_format("flac")
        assert d.transcode is False
        assert d.mime == "audio/flac"
        assert d.reason == "native"

    def test_native_mp3(self):
        d = decide_push_format("mp3")
        assert d.mime == "audio/mpeg"
        assert d.transcode is False

    def test_force_transcode_short_circuits_to_mp3(self):
        d = decide_push_format("flac", force_transcode=True)
        assert d.transcode is True
        assert d.mime == "audio/mpeg"
        assert d.transcode_bitrate == 320
        assert d.reason == "force_transcode"

    def test_unknown_container_falls_back_to_octet_stream(self):
        d = decide_push_format("xyz")
        assert d.mime == "application/octet-stream"
        assert d.transcode is False

    def test_upstream_mime_wins_over_container(self):
        d = decide_push_format("xyz", upstream_mime="audio/aac")
        assert d.mime == "audio/aac"

    def test_container_dot_prefix_tolerated(self):
        d = decide_push_format(".M4A")
        assert d.mime == "audio/mp4"

    def test_empty_container_with_no_upstream_mime(self):
        d = decide_push_format("")
        assert d.mime == "application/octet-stream"


class TestDecideRetryAfterError:
    def test_714_triggers_transcode_retry(self):
        d = decide_retry_after_error(714, "flac")
        assert d is not None
        assert d.transcode is True
        assert d.mime == "audio/mpeg"
        assert d.transcode_bitrate == 320
        assert "714" in d.reason

    def test_701_triggers_transcode_retry(self):
        d = decide_retry_after_error(701, "flac")
        assert d is not None
        assert d.transcode is True

    def test_non_retryable_error_returns_none(self):
        # 718 = Illegal CurrentURI — that's a URL problem, not a codec
        # problem; transcoding wouldn't help.
        assert decide_retry_after_error(718, "flac") is None
        assert decide_retry_after_error(500, "flac") is None
        assert decide_retry_after_error(404, "flac") is None

    def test_none_error_code_returns_none(self):
        # Network / parse failures with no UPnP code — don't transcode.
        assert decide_retry_after_error(None, "flac") is None


# ── SSDP discovery dedup + parsing ──────────────────────────────────────────


class TestParseUdnFromUsn:
    def test_root_device_usn(self):
        assert _parse_udn_from_usn("uuid:abc-123::upnp:rootdevice") == "uuid:abc-123"

    def test_service_usn(self):
        assert (
            _parse_udn_from_usn("uuid:abc-123::urn:schemas-upnp-org:device:MediaRenderer:1")
            == "uuid:abc-123"
        )

    def test_bare_uuid_usn(self):
        assert _parse_udn_from_usn("uuid:abc-123") == "uuid:abc-123"

    def test_malformed_returns_empty(self):
        assert _parse_udn_from_usn("notauuid::stuff") == ""
        assert _parse_udn_from_usn("") == ""

    def test_case_insensitive_uuid_prefix(self):
        # SSDP headers are sometimes capitalised oddly; spec is
        # case-insensitive on header *names* but values are usually
        # lowercase. Belt-and-braces.
        assert _parse_udn_from_usn("UUID:ABC::upnp:rootdevice") == "UUID:ABC"


class TestDedupSearchResponse:
    def test_novel_response_returned(self):
        seen: Dict[str, str] = {}
        r = dedupe_search_response(
            {"usn": "uuid:1::upnp:rootdevice", "location": "http://1.2.3.4:8080/desc.xml"},
            seen,
        )
        assert r == ("uuid:1", "http://1.2.3.4:8080/desc.xml")
        assert seen["uuid:1"] == "http://1.2.3.4:8080/desc.xml"

    def test_duplicate_returns_none(self):
        seen = {"uuid:1": "http://1.2.3.4:8080/desc.xml"}
        r = dedupe_search_response(
            {
                "usn": "uuid:1::urn:schemas-upnp-org:device:MediaRenderer:1",
                "location": "http://1.2.3.4:8080/different.xml",
            },
            seen,
        )
        assert r is None
        # The original location should still be pinned.
        assert seen["uuid:1"] == "http://1.2.3.4:8080/desc.xml"

    def test_missing_usn_returns_none(self):
        assert dedupe_search_response({"location": "http://1.2.3.4/desc.xml"}, {}) is None

    def test_missing_location_returns_none(self):
        assert dedupe_search_response({"usn": "uuid:1::upnp:rootdevice"}, {}) is None

    def test_uppercase_header_keys_tolerated(self):
        # CaseInsensitiveDict from async-upnp-client preserves case on
        # iteration — sometimes upper.
        seen: Dict[str, str] = {}
        r = dedupe_search_response(
            {"USN": "uuid:1::upnp:rootdevice", "LOCATION": "http://1.2.3.4/desc.xml"},
            seen,
        )
        assert r == ("uuid:1", "http://1.2.3.4/desc.xml")


class TestParseHostFromLocation:
    def test_http_with_explicit_port(self):
        assert parse_host_from_location("http://1.2.3.4:8080/desc.xml") == ("1.2.3.4", 8080)

    def test_http_default_port_80(self):
        assert parse_host_from_location("http://1.2.3.4/desc.xml") == ("1.2.3.4", 80)

    def test_hostname_resolved(self):
        host, port = parse_host_from_location("http://renderer.lan:9000/x.xml")
        assert host == "renderer.lan"
        assert port == 9000

    def test_empty_input(self):
        assert parse_host_from_location("") == ("", 0)

    def test_malformed_input(self):
        host, port = parse_host_from_location("not a url")
        assert host == ""
        assert port == 0


# ── Helpers ─────────────────────────────────────────────────────────────────


class TestSmallHelpers:
    def test_protocol_info_known_mime(self):
        assert "DLNA.ORG_PN=MP3" in _protocol_info_for("audio/mpeg")

    def test_protocol_info_unknown_falls_to_wildcard(self):
        assert _protocol_info_for("audio/xyz").endswith(":*")

    def test_protocol_info_blank_mime_defaults_to_octet_stream(self):
        info = _protocol_info_for("")
        assert "application/octet-stream" in info

    def test_format_duration_zero(self):
        assert _format_duration(0) == "0:00:00.000"

    def test_format_duration_fractional(self):
        assert _format_duration(1.234) == "0:00:01.234"

    def test_format_duration_negative_clamped(self):
        assert _format_duration(-5.0) == "0:00:00.000"

    def test_truncate_cover_url_short_unchanged(self):
        url = "http://x/cover.jpg"
        assert _truncate_cover_url(url) == url

    def test_truncate_cover_url_long_dropped(self):
        url = "http://x/" + ("y" * 300)
        assert _truncate_cover_url(url) == ""

    def test_truncate_cover_url_empty_returns_empty(self):
        assert _truncate_cover_url("") == ""

    def test_container_from_mime_reverse_lookup(self):
        assert _container_from_mime("audio/flac") == "flac"
        assert _container_from_mime("audio/mpeg") == "mp3"

    def test_container_from_mime_unknown_returns_empty(self):
        assert _container_from_mime("audio/xyz") == ""
        assert _container_from_mime("") == ""

    def test_meta_with_mime_swaps_mime_only(self):
        m = TrackMetadata(
            item_id="x",
            title="t",
            artist="a",
            duration_sec=10.0,
            mime="audio/flac",
            cover_url="http://c",
        )
        new = _meta_with_mime(m, "audio/mpeg")
        assert new.mime == "audio/mpeg"
        assert new.title == "t"
        assert new.artist == "a"
        # Returns a copy — original untouched.
        assert m.mime == "audio/flac"
        # size_bytes is intentionally zeroed on the retry (the
        # transcoded stream will have a different length).
        assert new.size_bytes == 0

    def test_td_to_sec_none(self):
        assert _td_to_sec(None) == 0.0

    def test_td_to_sec_numeric(self):
        assert _td_to_sec(12.5) == 12.5
        assert _td_to_sec(7) == 7.0

    def test_td_to_sec_timedelta(self):
        import datetime as dt

        assert _td_to_sec(dt.timedelta(seconds=42)) == 42.0


# ── User-Agent overrides ────────────────────────────────────────────────────


class TestUserAgentOverrides:
    def test_default_ua_when_no_overrides(self, monkeypatch):
        monkeypatch.setattr(_dlna, "_settings_user_agent_overrides", lambda: {})
        ua = _ua_for_device("Living Room TV")
        assert "jellytoast" in ua
        assert "DLNADOC/1.50" in ua

    def test_override_matched_substring(self, monkeypatch):
        monkeypatch.setattr(
            _dlna, "_settings_user_agent_overrides", lambda: {"Samsung": "custom/1.0"}
        )
        assert _ua_for_device("Samsung Living Room") == "custom/1.0"

    def test_override_case_insensitive(self, monkeypatch):
        monkeypatch.setattr(
            _dlna, "_settings_user_agent_overrides", lambda: {"BRAVIA": "sony-ua/2.0"}
        )
        assert _ua_for_device("Sony Bravia 65") == "sony-ua/2.0"

    def test_override_misses_returns_default(self, monkeypatch):
        monkeypatch.setattr(
            _dlna, "_settings_user_agent_overrides", lambda: {"Yamaha": "yam-ua/1.0"}
        )
        ua = _ua_for_device("Sony Bravia")
        assert "jellytoast" in ua


# ── Asyncio loop thread ─────────────────────────────────────────────────────


class TestDlnaLoopThread:
    def test_start_then_stop(self):
        t = _DlnaLoopThread(name="test-loop-1")
        t.start()
        assert t.is_running()
        t.stop()
        # After stop, _loop and _thread are cleared.
        assert not t.is_running()

    def test_start_is_idempotent(self):
        t = _DlnaLoopThread(name="test-loop-2")
        t.start()
        loop1 = t.loop
        t.start()  # second call should be a no-op
        assert t.loop is loop1
        t.stop()

    def test_submit_blocking_returns_coro_result(self):
        t = _DlnaLoopThread(name="test-loop-3")
        t.start()

        async def _coro():
            await asyncio.sleep(0)
            return 42

        try:
            assert t.submit_blocking(_coro(), timeout=2.0) == 42
        finally:
            t.stop()

    def test_submit_blocking_propagates_exception(self):
        t = _DlnaLoopThread(name="test-loop-4")
        t.start()

        async def _coro():
            raise RuntimeError("boom")

        try:
            with pytest.raises(RuntimeError, match="boom"):
                t.submit_blocking(_coro(), timeout=2.0)
        finally:
            t.stop()


# ── DlnaController push flow (mocked DmrDevice) ─────────────────────────────


class _UpnpActionResponseErrorStub(Exception):
    """Stand-in for ``async_upnp_client.exceptions.UpnpActionResponseError``.

    The real class is imported lazily inside ``_try_set_and_play`` so we
    swap that import out via monkeypatch and feed our stub through. The
    decision tree only reads ``.error_code`` off the exception."""

    def __init__(self, error_code: int, message: str = ""):
        super().__init__(message or f"UPnP error {error_code}")
        self.error_code = error_code


class _UpnpErrorStub(Exception):
    """Stand-in for the parent ``UpnpError`` class."""


class FakeDmrDevice:
    """Minimal DmrDevice double — records every call, can be primed to
    raise on the first ``async_set_transport_uri`` call."""

    def __init__(self, *, raise_codes: Optional[List[int]] = None):
        self.calls: List[tuple] = []
        # Sequence of error codes to raise on successive set_transport
        # calls — pop one each time. None = no error this time.
        self._raise_codes = list(raise_codes or [])
        self.name = "Fake Renderer"
        self.manufacturer = "TestCorp"
        self.model_name = "FakeBox-1"
        # Polled state.
        self.transport_state = "PLAYING"
        self.media_position = None
        self.media_duration = None
        self.volume_level = 0.5

    async def async_set_transport_uri(self, url, title, didl):
        self.calls.append(("set_transport_uri", url, title, didl))
        code = self._raise_codes.pop(0) if self._raise_codes else None
        if code is not None:
            raise _UpnpActionResponseErrorStub(code)

    async def async_play(self):
        self.calls.append(("play",))

    async def async_pause(self):
        self.calls.append(("pause",))

    async def async_stop(self):
        self.calls.append(("stop",))

    async def async_seek_rel_time(self, time):
        # async-upnp-client's DmrDevice.async_seek_rel_time takes a
        # datetime.timedelta — record it as-is so tests assert the real
        # contract (not a pre-formatted string).
        self.calls.append(("seek", time))

    async def async_set_volume_level(self, level):
        self.calls.append(("volume", level))

    async def async_mute_volume(self, on):
        self.calls.append(("mute", on))

    async def async_update(self):
        self.calls.append(("update",))


def _patch_exceptions(monkeypatch):
    """Redirect the lazy ``from async_upnp_client.exceptions import …``
    inside ``_try_set_and_play`` to our stubs so the controller's error
    classification still works without dragging the real classes in."""
    import async_upnp_client.exceptions as real

    monkeypatch.setattr(real, "UpnpActionResponseError", _UpnpActionResponseErrorStub)
    monkeypatch.setattr(real, "UpnpError", _UpnpErrorStub)


def _track_meta(**kwargs) -> TrackMetadata:
    defaults = dict(
        item_id="t1",
        title="Idioteque",
        artist="Radiohead",
        album="Kid A",
        track_number=8,
        duration_sec=309.0,
        mime="audio/flac",
        size_bytes=48217600,
        cover_url="http://c/x.jpg",
    )
    defaults.update(kwargs)
    return TrackMetadata(**defaults)


def _device() -> DlnaDevice:
    return DlnaDevice(
        name="Fake Renderer",
        host="192.168.1.50",
        port=8080,
        udn="uuid:abc",
        location="http://192.168.1.50:8080/desc.xml",
    )


class TestDlnaControllerPlayFlow:
    """End-to-end push flow with a mocked DmrDevice."""

    @pytest.fixture
    def controller(self, monkeypatch):
        _patch_exceptions(monkeypatch)
        c = DlnaController()
        # Skip async-upnp-client install check.
        monkeypatch.setattr(_dlna, "_ensure_async_upnp", lambda: True)
        monkeypatch.setattr(_dlna, "_settings_enabled", lambda: True)
        c.start()
        yield c
        c.stop()

    def test_native_push_happy_path(self, controller, monkeypatch):
        dev = _device()
        fake = FakeDmrDevice()

        async def _fake_bind(self, d):
            d.device_obj = fake
            return fake

        monkeypatch.setattr(DlnaController, "async_bind", _fake_bind)

        ok = controller.play(dev, "http://proxy/s/abc", _track_meta())
        assert ok is True
        # Stop FIRST (LG webOS 701 fix), then set_transport_uri + play.
        kinds = [c[0] for c in fake.calls]
        assert kinds == ["stop", "set_transport_uri", "play"]
        # DIDL is the 3rd arg of the set_transport_uri call.
        set_calls = [c for c in fake.calls if c[0] == "set_transport_uri"]
        didl = set_calls[0][3]
        assert "<dc:title>Idioteque</dc:title>" in didl
        # Active device tracked for transport-control dispatch.
        assert controller._active_udn == "uuid:abc"

    def test_pre_push_stop_is_best_effort(self, controller, monkeypatch):
        """The LG-quirk reset Stop is best-effort: a renderer that errors
        on Stop (e.g. an already-stopped transport) must not abort the
        Set + Play that follow."""
        dev = _device()
        fake = FakeDmrDevice()

        async def _boom():
            raise RuntimeError("already stopped")

        fake.async_stop = _boom  # override the recorder with a raiser

        async def _fake_bind(self, d):
            d.device_obj = fake
            return fake

        monkeypatch.setattr(DlnaController, "async_bind", _fake_bind)

        ok = controller.play(dev, "http://proxy/s/abc", _track_meta())
        assert ok is True
        # Stop raised → not recorded, but Set + Play still ran.
        kinds = [c[0] for c in fake.calls]
        assert kinds == ["set_transport_uri", "play"]

    def test_start_sec_seeks_to_resume_after_play(self, controller, monkeypatch):
        """Casting a mid-track item resumes at the offset (best-effort
        seek after the push) instead of restarting from 0."""
        import datetime as dt

        dev = _device()
        fake = FakeDmrDevice()

        async def _fake_bind(self, d):
            d.device_obj = fake
            return fake

        monkeypatch.setattr(DlnaController, "async_bind", _fake_bind)
        ok = controller.play(dev, "http://proxy/s/abc", _track_meta(), start_sec=65.0)
        assert ok is True
        seeks = [c for c in fake.calls if c[0] == "seek"]
        # async_seek_rel_time takes a timedelta, not a formatted string —
        # passing a string was a swallowed AttributeError (silent no-op).
        assert seeks and seeks[0][1] == dt.timedelta(seconds=65.0)

    def test_no_resume_seek_when_start_sec_zero(self, controller, monkeypatch):
        dev = _device()
        fake = FakeDmrDevice()

        async def _fake_bind(self, d):
            d.device_obj = fake
            return fake

        monkeypatch.setattr(DlnaController, "async_bind", _fake_bind)
        ok = controller.play(dev, "http://proxy/s/abc", _track_meta())  # default start_sec=0
        assert ok is True
        assert not any(c[0] == "seek" for c in fake.calls)

    def test_714_triggers_transcode_retry(self, controller, monkeypatch):
        dev = _device()
        fake = FakeDmrDevice(raise_codes=[714])

        async def _fake_bind(self, d):
            d.device_obj = fake
            return fake

        monkeypatch.setattr(DlnaController, "async_bind", _fake_bind)

        called_with: List[tuple] = []

        def _transcode_url(url, bitrate):
            called_with.append((url, bitrate))
            return f"{url}?transcode=mp3&br={bitrate}"

        ok = controller.play(
            dev,
            "http://proxy/s/abc",
            _track_meta(),
            transcode_url_fn=_transcode_url,
        )
        assert ok is True
        # Provider asked once for an MP3 URL.
        assert called_with == [("http://proxy/s/abc", 320)]
        # Two set_transport_uri attempts — native + transcoded.
        kinds = [c[0] for c in fake.calls]
        assert kinds.count("set_transport_uri") == 2
        set_calls = [c for c in fake.calls if c[0] == "set_transport_uri"]
        # Retry URL carries the transcode marker.
        assert "transcode=mp3" in set_calls[1][1]
        # And the retry DIDL flips MIME to audio/mpeg.
        assert "audio/mpeg:DLNA.ORG_PN=MP3" in set_calls[1][3]
        # UDN is now pinned to transcode for the session.
        assert controller._transcode_cache["uuid:abc"] is True

    def test_701_also_triggers_retry(self, controller, monkeypatch):
        dev = _device()
        fake = FakeDmrDevice(raise_codes=[701])

        async def _fake_bind(self, d):
            d.device_obj = fake
            return fake

        monkeypatch.setattr(DlnaController, "async_bind", _fake_bind)

        ok = controller.play(
            dev,
            "http://proxy/s/abc",
            _track_meta(),
            transcode_url_fn=lambda u, br: f"{u}?mp3={br}",
        )
        assert ok is True
        # Retry happened.
        kinds = [c[0] for c in fake.calls]
        assert kinds.count("set_transport_uri") == 2

    def test_retry_without_transcode_fn_fails(self, controller, monkeypatch):
        dev = _device()
        fake = FakeDmrDevice(raise_codes=[714])
        # A genuine format rejection with no retry leaves the renderer
        # stopped — so the errors-but-plays rescue doesn't mask the failure.
        fake.transport_state = "STOPPED"

        async def _fake_bind(self, d):
            d.device_obj = fake
            return fake

        monkeypatch.setattr(DlnaController, "async_bind", _fake_bind)

        ok = controller.play(dev, "http://proxy/s/abc", _track_meta())
        # No callback supplied → no retry, push fails.
        assert ok is False
        kinds = [c[0] for c in fake.calls]
        assert kinds.count("set_transport_uri") == 1

    def test_unretryable_error_fails(self, controller, monkeypatch):
        dev = _device()
        # 718 = Illegal CurrentURI — not in our retry set.
        fake = FakeDmrDevice(raise_codes=[718])
        fake.transport_state = "STOPPED"  # rejected → not playing

        async def _fake_bind(self, d):
            d.device_obj = fake
            return fake

        monkeypatch.setattr(DlnaController, "async_bind", _fake_bind)

        ok = controller.play(
            dev,
            "http://proxy/s/abc",
            _track_meta(),
            transcode_url_fn=lambda u, br: f"{u}?mp3",
        )
        assert ok is False
        # Even with a callback present, no retry fired.
        kinds = [c[0] for c in fake.calls]
        assert kinds.count("set_transport_uri") == 1

    def test_session_pinned_transcode_skips_native(self, controller, monkeypatch):
        dev = _device()
        fake = FakeDmrDevice()
        controller._transcode_cache[dev.udn] = True  # pin from a prior 714

        async def _fake_bind(self, d):
            d.device_obj = fake
            return fake

        monkeypatch.setattr(DlnaController, "async_bind", _fake_bind)

        called_with: List[tuple] = []

        def _transcode_url(url, bitrate):
            called_with.append((url, bitrate))
            return f"{url}?mp3"

        ok = controller.play(
            dev,
            "http://proxy/s/abc",
            _track_meta(),
            transcode_url_fn=_transcode_url,
        )
        assert ok is True
        # Goes straight to transcode, no native attempt.
        assert called_with == [("http://proxy/s/abc", 320)]
        # Only one push attempt — the transcoded one.
        kinds = [c[0] for c in fake.calls]
        assert kinds.count("set_transport_uri") == 1
        set_calls = [c for c in fake.calls if c[0] == "set_transport_uri"]
        assert "audio/mpeg" in set_calls[0][3]

    def test_force_transcode_param_short_circuits_native(self, controller, monkeypatch):
        dev = _device()
        fake = FakeDmrDevice()

        async def _fake_bind(self, d):
            d.device_obj = fake
            return fake

        monkeypatch.setattr(DlnaController, "async_bind", _fake_bind)

        ok = controller.play(
            dev,
            "http://proxy/s/abc",
            _track_meta(),
            transcode_url_fn=lambda u, br: f"{u}?mp3",
            force_transcode=True,
        )
        assert ok is True
        kinds = [c[0] for c in fake.calls]
        assert kinds.count("set_transport_uri") == 1
        set_calls = [c for c in fake.calls if c[0] == "set_transport_uri"]
        assert "audio/mpeg" in set_calls[0][3]

    def test_bind_failure_returns_false(self, controller, monkeypatch):
        dev = _device()

        async def _fake_bind(self, d):
            return None

        monkeypatch.setattr(DlnaController, "async_bind", _fake_bind)

        ok = controller.play(dev, "http://proxy/s/abc", _track_meta())
        assert ok is False


class TestDlnaControllerTransport:
    """Pause / resume / stop / seek / volume / mute dispatch."""

    @pytest.fixture
    def controller_with_active(self, monkeypatch):
        _patch_exceptions(monkeypatch)
        c = DlnaController()
        monkeypatch.setattr(_dlna, "_ensure_async_upnp", lambda: True)
        monkeypatch.setattr(_dlna, "_settings_enabled", lambda: True)
        c.start()
        fake = FakeDmrDevice()
        c._active_udn = "uuid:abc"
        c._active_device_obj = fake
        yield c, fake
        c.stop()

    def test_pause_dispatches(self, controller_with_active):
        c, fake = controller_with_active
        assert c.pause() is True
        assert ("pause",) in fake.calls

    def test_resume_dispatches_play(self, controller_with_active):
        # In UPnP-AV "resume" is just another Play call.
        c, fake = controller_with_active
        assert c.resume() is True
        assert ("play",) in fake.calls

    def test_stop_dispatches_and_clears_active(self, controller_with_active):
        c, fake = controller_with_active
        assert c.stop_renderer() is True
        assert ("stop",) in fake.calls
        assert c._active_udn is None
        assert c._active_device_obj is None

    def test_seek_passes_timedelta(self, controller_with_active):
        import datetime as dt

        c, fake = controller_with_active
        assert c.seek(75.5) is True
        # async_seek_rel_time wants a timedelta, not a formatted string.
        kinds = [(x[0], x[1] if len(x) > 1 else None) for x in fake.calls]
        assert ("seek", dt.timedelta(seconds=75.5)) in kinds

    def test_seek_negative_clamped_to_zero(self, controller_with_active):
        import datetime as dt

        c, fake = controller_with_active
        assert c.seek(-5.0) is True
        kinds = [(x[0], x[1] if len(x) > 1 else None) for x in fake.calls]
        assert ("seek", dt.timedelta(seconds=0.0)) in kinds

    def test_volume_normalises_to_0_1(self, controller_with_active):
        c, fake = controller_with_active
        assert c.set_volume(50) is True
        kinds = [(x[0], x[1] if len(x) > 1 else None) for x in fake.calls]
        assert any(k == "volume" and abs(v - 0.5) < 1e-6 for k, v in kinds)

    def test_volume_clamps_above_100(self, controller_with_active):
        c, fake = controller_with_active
        assert c.set_volume(150) is True
        kinds = [(x[0], x[1] if len(x) > 1 else None) for x in fake.calls]
        assert ("volume", 1.0) in kinds

    def test_volume_clamps_below_zero(self, controller_with_active):
        c, fake = controller_with_active
        assert c.set_volume(-10) is True
        kinds = [(x[0], x[1] if len(x) > 1 else None) for x in fake.calls]
        assert ("volume", 0.0) in kinds

    def test_mute_dispatches(self, controller_with_active):
        c, fake = controller_with_active
        assert c.set_mute(True) is True
        assert ("mute", True) in fake.calls

    def test_transport_no_op_when_no_active_device(self, monkeypatch):
        c = DlnaController()
        monkeypatch.setattr(_dlna, "_ensure_async_upnp", lambda: True)
        monkeypatch.setattr(_dlna, "_settings_enabled", lambda: True)
        # Don't start the loop or set an active device — every call
        # short-circuits to False.
        assert c.pause() is False
        assert c.resume() is False
        assert c.stop_renderer() is False
        assert c.seek(10) is False
        assert c.set_volume(50) is False
        assert c.set_mute(True) is False


# ── Discovery (mocked async_search) ─────────────────────────────────────────


class TestDlnaControllerDiscover:
    def test_async_discover_dedupes_and_returns_devices(self, monkeypatch):
        # Build a fake ``async_search`` that yields three SSDP-shaped
        # responses, two from the same UDN.
        responses = [
            {"usn": "uuid:1::upnp:rootdevice", "location": "http://1.1.1.1:8000/desc.xml"},
            {
                "usn": "uuid:1::urn:schemas-upnp-org:device:MediaRenderer:1",
                "location": "http://1.1.1.1:8000/desc2.xml",
            },
            {"usn": "uuid:2::upnp:rootdevice", "location": "http://2.2.2.2:9000/desc.xml"},
        ]

        async def _fake_search(*, async_callback, timeout, search_target, source=None):
            assert search_target == SSDP_ST_MEDIA_RENDERER
            for r in responses:
                await async_callback(r)

        # The controller imports ``async_search`` lazily inside
        # ``async_discover``; patch the module-level symbol it pulls.
        import async_upnp_client.search as _s

        monkeypatch.setattr(_s, "async_search", _fake_search)

        c = DlnaController()
        monkeypatch.setattr(_dlna, "_ensure_async_upnp", lambda: True)
        monkeypatch.setattr(_dlna, "_settings_enabled", lambda: True)
        c.start()
        try:
            found = c.discover(timeout=1)
        finally:
            c.stop()
        assert len(found) == 2
        udns = {d.udn for d in found}
        assert udns == {"uuid:1", "uuid:2"}
        # Host/port populated from the LOCATION URL.
        by_udn = {d.udn: d for d in found}
        assert by_udn["uuid:1"].host == "1.1.1.1"
        assert by_udn["uuid:1"].port == 8000
        assert by_udn["uuid:2"].host == "2.2.2.2"
        assert by_udn["uuid:2"].port == 9000

    def test_async_discover_validate_drops_non_renderers(self, monkeypatch):
        """validate=True binds each candidate and drops those that don't
        bind as a real DMR — the 'advertises MediaRenderer over SSDP but
        isn't one' case (the .248 box found alongside the LG TV)."""
        responses = [
            {
                "usn": "uuid:good::urn:schemas-upnp-org:device:MediaRenderer:1",
                "location": "http://1.1.1.1:8000/d.xml",
            },
            {
                "usn": "uuid:bad::urn:schemas-upnp-org:device:MediaRenderer:1",
                "location": "http://2.2.2.2:9000/d.xml",
            },
        ]

        async def _fake_search(*, async_callback, timeout, search_target, source=None):
            for r in responses:
                await async_callback(r)

        import async_upnp_client.search as _s

        monkeypatch.setattr(_s, "async_search", _fake_search)

        async def _fake_bind(self, d):
            if d.udn == "uuid:bad":
                return None  # advertises MediaRenderer but won't bind
            d.device_obj = object()
            return d.device_obj

        monkeypatch.setattr(DlnaController, "async_bind", _fake_bind)

        c = DlnaController()
        monkeypatch.setattr(_dlna, "_ensure_async_upnp", lambda: True)
        monkeypatch.setattr(_dlna, "_settings_enabled", lambda: True)
        c.start()
        try:
            found = c.discover(timeout=1, validate=True)
        finally:
            c.stop()
        assert {d.udn for d in found} == {"uuid:good"}

    def test_on_device_callback_fires_per_new_device(self, monkeypatch):
        responses = [
            {"usn": "uuid:1::upnp:rootdevice", "location": "http://1.1.1.1:8000/desc.xml"},
            {
                "usn": "uuid:1::upnp:rootdevice",  # dup, no callback
                "location": "http://1.1.1.1:8000/desc.xml",
            },
            {"usn": "uuid:2::upnp:rootdevice", "location": "http://2.2.2.2:9000/desc.xml"},
        ]

        async def _fake_search(*, async_callback, timeout, search_target, source=None):
            for r in responses:
                await async_callback(r)

        import async_upnp_client.search as _s

        monkeypatch.setattr(_s, "async_search", _fake_search)

        seen: List[DlnaDevice] = []
        c = DlnaController()
        monkeypatch.setattr(_dlna, "_ensure_async_upnp", lambda: True)
        monkeypatch.setattr(_dlna, "_settings_enabled", lambda: True)
        c.start()
        try:
            c.discover(timeout=1, on_device=lambda d: seen.append(d))
        finally:
            c.stop()
        assert len(seen) == 2

    def test_discover_short_circuits_when_disabled(self, monkeypatch):
        monkeypatch.setattr(_dlna, "_ensure_async_upnp", lambda: True)
        monkeypatch.setattr(_dlna, "_settings_enabled", lambda: False)
        c = DlnaController()
        # Loop never starts in this path.
        assert c.discover(timeout=1) == []


# ── Singleton + ``is_available`` ────────────────────────────────────────────


class TestSingletonAndAvailability:
    def test_get_dlna_controller_is_singleton(self):
        a = get_dlna_controller()
        b = get_dlna_controller()
        assert a is b

    def test_is_available_true_when_lib_installed(self):
        # The test environment has async-upnp-client installed (it's a
        # runtime dep). If this test ever runs in a stripped environment
        # the assertion would flip — that's the intended signal.
        assert is_available() is True


# ── Polling ──────────────────────────────────────────────────────────────────


class TestPolling:
    def test_sample_once_returns_state_dict(self, monkeypatch):
        _patch_exceptions(monkeypatch)
        c = DlnaController()
        monkeypatch.setattr(_dlna, "_ensure_async_upnp", lambda: True)
        monkeypatch.setattr(_dlna, "_settings_enabled", lambda: True)
        c.start()
        fake = FakeDmrDevice()
        fake.transport_state = "PLAYING"
        # FakeDmrDevice exposes timedelta-typed positions in real life;
        # the helper coerces None to 0.0 and numeric to float.
        fake.media_position = 12.5
        fake.media_duration = 309.0
        fake.volume_level = 0.42
        c._active_device_obj = fake
        try:
            state = c._loop_thread.submit_blocking(c._sample_once(), timeout=2.0)
        finally:
            c.stop()
        assert state == {
            "transport_state": "PLAYING",
            "position_sec": 12.5,
            "duration_sec": 309.0,
            "volume_level": 0.42,
        }

    def test_sample_once_returns_none_with_no_active(self, monkeypatch):
        c = DlnaController()
        monkeypatch.setattr(_dlna, "_ensure_async_upnp", lambda: True)
        monkeypatch.setattr(_dlna, "_settings_enabled", lambda: True)
        c.start()
        try:
            state = c._loop_thread.submit_blocking(c._sample_once(), timeout=2.0)
        finally:
            c.stop()
        assert state is None

    def test_last_state_returns_copy(self, monkeypatch):
        c = DlnaController()
        c._last_state = {"transport_state": "PAUSED_PLAYBACK"}
        snap = c.last_state()
        snap["mutated"] = True
        # Mutating the returned dict must not affect the controller's
        # stored state.
        assert "mutated" not in c._last_state


# ── Settings reads (defensive) ──────────────────────────────────────────────


class TestSettingsReads:
    def test_enabled_default_false_on_settings_failure(self, monkeypatch):
        # Simulate get_settings raising — cast is opt-in, so the error
        # path returns False (don't scan the network when we can't confirm
        # the user enabled DLNA), aligned with the first-run default.
        import jellytoast.settings as s

        def _boom():
            raise RuntimeError("no settings here")

        monkeypatch.setattr(s, "get_settings", _boom)
        assert _dlna._settings_enabled() is False

    def test_ua_overrides_empty_on_settings_failure(self, monkeypatch):
        import jellytoast.settings as s

        def _boom():
            raise RuntimeError("no settings here")

        monkeypatch.setattr(s, "get_settings", _boom)
        assert _dlna._settings_user_agent_overrides() == {}

    def test_ua_overrides_parses_json(self, monkeypatch):
        # Fake a settings object whose QSettings .value returns our JSON.
        class _FakeQS:
            def value(self, key, default="", type=str):
                if key == "cast/dlna_user_agent_overrides":
                    return json.dumps({"Samsung": "samsung-ua/1.0"})
                return default

        class _FakeSettings:
            _s = _FakeQS()

        import jellytoast.settings as s

        monkeypatch.setattr(s, "get_settings", lambda: _FakeSettings())
        out = _dlna._settings_user_agent_overrides()
        assert out == {"Samsung": "samsung-ua/1.0"}

    def test_ua_overrides_malformed_json_returns_empty(self, monkeypatch):
        class _FakeQS:
            def value(self, key, default="", type=str):
                return "not-json{{"

        class _FakeSettings:
            _s = _FakeQS()

        import jellytoast.settings as s

        monkeypatch.setattr(s, "get_settings", lambda: _FakeSettings())
        assert _dlna._settings_user_agent_overrides() == {}


# ── Renderer "errors-but-plays" rescue (audit #8 / GUI-cast failure) ────────
# Some renderers (LG webOS) return a UPnP error on the push yet transition to
# PLAYING. _renderer_started polls the real transport state so async_play can
# report success for a cast that's audibly working, instead of "Cast failed".

import asyncio as _asyncio  # noqa: E402

from jellytoast.cast.dlna import controller as _ctrl_mod  # noqa: E402


class _FakeDmr:
    def __init__(self, state, *, fail_update=False):
        self.transport_state = state
        self._fail = fail_update
        self.updates = 0

    async def async_update(self):
        self.updates += 1
        if self._fail:
            raise RuntimeError("renderer offline")


def _run_started(monkeypatch, dmr):
    async def _no_sleep(*a, **k):
        return None

    monkeypatch.setattr(_ctrl_mod.asyncio, "sleep", _no_sleep)
    c = DlnaController.__new__(DlnaController)
    return _asyncio.run(c._renderer_started(dmr))


class TestRendererStartedRescue:
    def test_playing_is_success(self, monkeypatch):
        assert _run_started(monkeypatch, _FakeDmr("PLAYING")) is True

    def test_transitioning_is_success(self, monkeypatch):
        assert _run_started(monkeypatch, _FakeDmr("TRANSITIONING")) is True

    def test_paused_is_success(self, monkeypatch):
        assert _run_started(monkeypatch, _FakeDmr("PAUSED_PLAYBACK")) is True

    def test_stopped_is_failure_after_full_poll(self, monkeypatch):
        dmr = _FakeDmr("STOPPED")
        assert _run_started(monkeypatch, dmr) is False
        assert dmr.updates == 4  # polled the whole window before giving up

    def test_update_error_is_failure(self, monkeypatch):
        assert _run_started(monkeypatch, _FakeDmr("PLAYING", fail_update=True)) is False

    def test_enum_valued_state(self, monkeypatch):
        class _E:
            value = "playing"  # async-upnp-client returns a TransportState enum

        assert _run_started(monkeypatch, _FakeDmr(_E())) is True


# ── LAN-interface binding (Tailscale fix) ────────────────────────────────────


class TestDlnaLanBinding:
    """The SSDP M-SEARCH binds to the LAN interface(s), Tailscale/CGNAT overlay
    excluded — on a Tailscale host a default-bound search leaves via the tunnel
    and finds no renderers (the bug this fixes; Chromecast already LAN-binds)."""

    def test_lan_search_sources_maps_each_interface(self, monkeypatch):
        import jellytoast.cast_manager as cm

        monkeypatch.setattr(cm, "_discovery_interfaces", lambda: ["10.0.0.7", "192.168.1.5"])
        assert _ctrl_mod._lan_search_sources() == [("10.0.0.7", 0), ("192.168.1.5", 0)]

    def test_lan_search_sources_empty_without_interfaces(self, monkeypatch):
        import jellytoast.cast_manager as cm

        monkeypatch.setattr(cm, "_discovery_interfaces", lambda: None)
        assert _ctrl_mod._lan_search_sources() == []

    def test_lan_search_sources_swallows_errors(self, monkeypatch):
        import jellytoast.cast_manager as cm

        def _boom():
            raise RuntimeError("ifaddr exploded")

        monkeypatch.setattr(cm, "_discovery_interfaces", _boom)
        assert _ctrl_mod._lan_search_sources() == []

    def test_async_discover_binds_search_to_lan_source(self, monkeypatch):
        import jellytoast.cast_manager as cm

        monkeypatch.setattr(cm, "_discovery_interfaces", lambda: ["10.0.0.7"])
        recorded = []

        async def _fake_search(*, async_callback, timeout, search_target, source=None):
            recorded.append(source)

        import async_upnp_client.search as _s

        monkeypatch.setattr(_s, "async_search", _fake_search)
        monkeypatch.setattr(_dlna, "_ensure_async_upnp", lambda: True)
        monkeypatch.setattr(_dlna, "_settings_enabled", lambda: True)

        c = DlnaController()
        c.start()
        try:
            c.discover(timeout=1)
        finally:
            c.stop()
        # The M-SEARCH went out the LAN interface, not the default route / tunnel.
        assert recorded == [("10.0.0.7", 0)]
