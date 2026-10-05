"""Word-level ("karaoke") lyrics — OpenSubsonic songLyrics v2.

Provider: enhanced=true only when the server advertises songLyrics v2;
cue byte offsets (UTF-8, inclusive end) become Jellyfin LyricLineCue
character positions (exclusive end) into the displayed line; `offset`
shifts every timestamp; translation/pronunciation layers are ignored.

Renderer: the active line splits into sung / unsung runs at word
boundaries, re-renders only when the split moves, and returns to plain
text when the line is left.
"""

from __future__ import annotations

import pytest

from jellytoast.providers.subsonic import SubsonicProvider


def _provider(entries, song_lyrics_versions=(1, 2)):
    p = SubsonicProvider()
    p._server_url = "http://nd.local"
    seen = []

    def fake_request(method, params=None, **_kw):
        seen.append((method, dict(params or {})))
        if method == "getOpenSubsonicExtensions":
            return {
                "openSubsonicExtensions": [
                    {"name": "songLyrics", "versions": list(song_lyrics_versions)}
                ]
            }
        return {"lyricsList": {"structuredLyrics": entries}}

    p._request = fake_request
    return p, seen


def _cue(start, b0, b1, value, end=None):
    c = {"start": start, "byteStart": b0, "byteEnd": b1, "value": value}
    if end is not None:
        c["end"] = end
    return c


_HELLO = {
    "synced": True,
    "line": [{"start": 1000, "value": "Hello world"}, {"start": 3000, "value": "Bye"}],
    "cueLine": [
        {
            "index": 0,
            "start": 1000,
            "value": "Hello world",
            "cue": [_cue(1000, 0, 4, "Hello", 1400), _cue(1500, 6, 10, "world", 2000)],
        }
    ],
}


class TestProviderCues:
    def test_requests_enhanced_only_for_v2(self):
        p, seen = _provider([_HELLO])
        p.get_lyrics("s1")
        assert seen[-1] == ("getLyricsBySongId", {"id": "s1", "enhanced": "true"})
        p, seen = _provider([_HELLO], song_lyrics_versions=(1,))
        p.get_lyrics("s1")
        assert seen[-1] == ("getLyricsBySongId", {"id": "s1"})

    def test_cues_project_to_jellyfin_shape(self):
        p, _ = _provider([_HELLO])
        lines = p.get_lyrics("s1")["Lyrics"]
        assert lines[0]["Cues"] == [
            {"Position": 0, "EndPosition": 5, "Start": 1000 * 10_000, "End": 1400 * 10_000},
            {"Position": 6, "EndPosition": 11, "Start": 1500 * 10_000, "End": 2000 * 10_000},
        ]
        assert "Cues" not in lines[1]

    def test_multibyte_offsets_become_character_positions(self):
        # "Café ñu": é and ñ are 2 bytes each in UTF-8.
        text = "Café ñu"
        entry = {
            "synced": True,
            "line": [{"start": 10, "value": text}],
            "cueLine": [
                {
                    "index": 0,
                    "value": text,
                    "cue": [
                        _cue(10, 0, 4, "Café"),  # bytes 0..4 (é = bytes 3-4)
                        _cue(20, 6, 8, "ñu"),  # bytes 6..8 (ñ = 6-7, u = 8)
                    ],
                }
            ],
        }
        p, _ = _provider([entry])
        cues = p.get_lyrics("s1")["Lyrics"][0]["Cues"]
        assert [(c["Position"], c["EndPosition"]) for c in cues] == [(0, 4), (5, 7)]
        assert [text[c["Position"] : c["EndPosition"]] for c in cues] == ["Café", "ñu"]
        assert cues[0]["End"] is None  # start-only timing (Enhanced LRC)

    def test_agent_layer_is_located_inside_the_line(self):
        # Lead + background vocals share line 0; the main agent's cueLine
        # comes first and only covers part of the displayed text.
        entry = {
            "synced": True,
            "agents": [{"id": "a", "role": "main"}, {"id": "b", "role": "bg"}],
            "line": [{"start": 100, "value": "  Run away (away)"}],
            "cueLine": [
                {
                    "index": 0,
                    "agentId": "a",
                    "value": "Run away",
                    "cue": [_cue(100, 0, 2, "Run"), _cue(300, 4, 7, "away")],
                },
                {"index": 0, "agentId": "b", "value": "(away)", "cue": [_cue(500, 0, 5, "(away)")]},
            ],
        }
        p, _ = _provider([entry])
        line = p.get_lyrics("s1")["Lyrics"][0]
        assert line["Text"] == "Run away (away)"
        assert [line["Text"][c["Position"] : c["EndPosition"]] for c in line["Cues"]] == [
            "Run",
            "away",
        ]

    def test_unplaceable_cue_line_drops_cues_not_the_line(self):
        entry = {
            "synced": True,
            "line": [{"start": 100, "value": "Shown text"}],
            "cueLine": [{"index": 0, "value": "different", "cue": [_cue(100, 0, 3, "diff")]}],
        }
        p, _ = _provider([entry])
        line = p.get_lyrics("s1")["Lyrics"][0]
        assert line["Text"] == "Shown text" and "Cues" not in line

    def test_offset_shifts_lines_and_cues_earlier(self):
        p, _ = _provider([dict(_HELLO, offset=500)])
        lines = p.get_lyrics("s1")["Lyrics"]
        assert lines[0]["Start"] == 500 * 10_000
        assert lines[0]["Cues"][0]["Start"] == 500 * 10_000
        assert lines[1]["Start"] == 2500 * 10_000

    def test_translation_layers_are_ignored(self):
        translation = {
            "kind": "translation",
            "lang": "es",
            "synced": True,
            "line": [{"start": 1000, "value": "Hola mundo"}],
        }
        p, _ = _provider([translation, _HELLO])
        assert p.get_lyrics("s1")["Lyrics"][0]["Text"] == "Hello world"

    def test_prefers_synced_entry_with_cues(self):
        plain_synced = {"synced": True, "line": [{"start": 1000, "value": "Hello world"}]}
        p, _ = _provider([plain_synced, _HELLO])
        assert "Cues" in p.get_lyrics("s1")["Lyrics"][0]


# ── Renderer ──────────────────────────────────────────────────────


@pytest.fixture
def host(qapp, isolated_settings, monkeypatch):
    from PySide6.QtWidgets import QScrollArea, QVBoxLayout, QWidget

    from jellytoast import np_lyrics

    class _Host(np_lyrics._LyricsMixin, QWidget):
        def __init__(self):
            super().__init__()
            self._lyrics_scroll = QScrollArea(self)
            self._lyrics_container = QWidget()
            self._lyrics_layout = QVBoxLayout(self._lyrics_container)
            self._lyrics_layout.addStretch(1)
            self._lyrics_scroll.setWidget(self._lyrics_container)
            self._lyrics_widgets = []
            self._lyrics_starts_ms = []
            self._lyrics_synced = False
            self._active_line_idx = -1
            self._user_off_live = False
            self._status_label = None
            self._reset_karaoke()

        def _update_lyrics_visibility(self):
            pass

        def _update_live_btn_visibility(self):
            pass

    class _NP:
        position = 0

    monkeypatch.setattr(np_lyrics, "get_now_playing", lambda: _NP())
    return _Host()


def _payload():
    p, _ = _provider([_HELLO])
    return p.get_lyrics("s1")


def _text(label):
    from PySide6.QtGui import QTextDocument

    doc = QTextDocument()
    doc.setHtml(label.text())
    return doc.toPlainText()


class TestRenderer:
    def test_split_advances_word_by_word(self, host):
        from PySide6.QtCore import Qt

        host._render_lyrics_payload(_payload())
        label = host._lyrics_widgets[0]
        host._update_active_lyric(1100)  # "Hello" started
        assert label.textFormat() == Qt.TextFormat.RichText
        assert host._karaoke == (0, 5)
        assert label.text().startswith("Hello<span")
        assert _text(label) == "Hello world"
        host._update_active_lyric(1600)  # "world" started
        assert host._karaoke == (0, 11)

    def test_no_rerender_within_a_word(self, host):
        host._render_lyrics_payload(_payload())
        host._update_active_lyric(1100)
        label = host._lyrics_widgets[0]
        label.setText("sentinel")
        host._update_active_lyric(1200)  # still inside "Hello"
        assert label.text() == "sentinel"

    def test_leaving_the_line_restores_plain_text(self, host):
        from PySide6.QtCore import Qt

        host._render_lyrics_payload(_payload())
        host._update_active_lyric(1600)
        host._update_active_lyric(3100)  # line 1 has no cues
        first = host._lyrics_widgets[0]
        assert first.textFormat() == Qt.TextFormat.PlainText
        assert first.text() == "Hello world"
        assert host._karaoke is None

    def test_seek_back_within_line_unsings(self, host):
        host._render_lyrics_payload(_payload())
        host._update_active_lyric(1600)
        host._update_active_lyric(1100)
        assert host._karaoke == (0, 5)

    def test_markup_in_lyrics_is_escaped(self, host):
        payload = {
            "Lyrics": [
                {
                    "Text": "<b>loud</b>",
                    "Start": 10_000,
                    "Cues": [{"Position": 0, "EndPosition": 11, "Start": 10_000, "End": None}],
                }
            ]
        }
        host._render_lyrics_payload(payload)
        host._update_active_lyric(5)
        assert _text(host._lyrics_widgets[0]) == "<b>loud</b>"

    def test_jellyfin_leading_space_positions_shift(self, host):
        payload = {
            "Lyrics": [
                {
                    "Text": "  Hey you",
                    "Start": 10_000,
                    "Cues": [
                        {"Position": 2, "EndPosition": 5, "Start": 10_000, "End": None},
                        {"Position": 6, "EndPosition": 9, "Start": 20_000, "End": None},
                    ],
                }
            ]
        }
        host._render_lyrics_payload(payload)
        host._update_active_lyric(1)
        assert host._karaoke == (0, 3)  # "Hey" in the stripped text

    def test_plain_lines_stay_plain_text(self, host):
        from PySide6.QtCore import Qt

        host._render_lyrics_payload({"Lyrics": [{"Text": "a <i>b</i>", "Start": 10_000}]})
        host._update_active_lyric(5)
        label = host._lyrics_widgets[0]
        assert label.textFormat() == Qt.TextFormat.PlainText
        assert label.text() == "a <i>b</i>"
