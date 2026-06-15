"""The now-playing bar must not clobber ICY radio title/artist on a
replayed _on_started.

Bug-hunt regression: _on_radio_state owns _track_title/_track_subtitle for
a live stream, but _on_started wrote them from np unconditionally — so any
replay (dpr change, image-cache clear) overwrote the ICY title with the
stream's (empty/wrong) np fields. The mini player + NP page already guard
on _is_radio; the bar didn't.
"""


def _make_bar(qapp, monkeypatch):
    from jellytoast import now_playing_bar as _npb

    # No real cover fetch in the non-radio control case.
    monkeypatch.setattr(_npb, "load_image_async", lambda *a, **k: None)
    return _npb.NowPlayingBar()


def test_radio_title_survives_replayed_on_started(qapp, monkeypatch):
    from jellytoast.player_state import NowPlaying

    bar = _make_bar(qapp, monkeypatch)
    bar._is_radio = True
    bar._track_title = "ICY Song"
    bar._track_subtitle = "ICY Artist"

    # A replayed playback_started (e.g. dpr change) for the radio stream —
    # np carries the stream's fallback fields, not the ICY title.
    np = NowPlaying(
        item_id="radio-1",
        title="http stream fallback",
        subtitle="",
        album="",
        item_type="Audio",
        stream_url="http://stream",
    )
    bar._on_started(np)

    assert bar._track_title == "ICY Song"
    assert bar._track_subtitle == "ICY Artist"


def test_non_radio_on_started_sets_title(qapp, monkeypatch):
    from jellytoast.player_state import NowPlaying

    bar = _make_bar(qapp, monkeypatch)
    bar._is_radio = False
    np = NowPlaying(
        item_id="track-1",
        title="Real Title",
        subtitle="Real Artist",
        album="Album",
        item_type="Audio",
        stream_url="http://stream",
    )
    bar._on_started(np)

    assert bar._track_title == "Real Title"
    assert bar._track_subtitle == "Real Artist"
