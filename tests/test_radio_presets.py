"""Tests for the curated radio-presets list + picker dialog wiring.

The preset list is in-repo data; these tests guard its shape so an
edit can't accidentally drop a required field or ship a malformed
URL. The dialog tests cover the "already-added" rendering and the
add-requested signal contract that RadioView depends on.
"""

from __future__ import annotations

import pytest

from jellytoast.radio_presets import POPULAR_STATIONS, category_order

# ── Preset list shape ──────────────────────────────────────────────────────


class TestPresetShape:
    def test_list_is_nonempty(self):
        assert len(POPULAR_STATIONS) > 0

    @pytest.mark.parametrize("idx", range(len(POPULAR_STATIONS)))
    def test_each_entry_has_required_fields(self, idx):
        preset = POPULAR_STATIONS[idx]
        for key in ("name", "streamUrl", "homePageUrl", "description", "category"):
            assert key in preset, f"preset {idx} missing key {key!r}"
            assert isinstance(preset[key], str)
        # Non-empty values for the load-bearing fields. homePageUrl /
        # description / category may be informational — those can be
        # empty in principle, but name + streamUrl are required for
        # the picker to work.
        assert preset["name"].strip()
        assert preset["streamUrl"].strip()

    @pytest.mark.parametrize("idx", range(len(POPULAR_STATIONS)))
    def test_stream_url_is_http(self, idx):
        url = POPULAR_STATIONS[idx]["streamUrl"]
        assert url.startswith("http://") or url.startswith("https://")

    def test_stream_urls_are_unique(self):
        urls = [p["streamUrl"] for p in POPULAR_STATIONS]
        assert len(urls) == len(set(urls)), "duplicate streamUrl in POPULAR_STATIONS"

    def test_category_order_is_a_list_of_strings(self):
        order = category_order()
        assert isinstance(order, list)
        assert all(isinstance(c, str) for c in order)


# ── Picker dialog ──────────────────────────────────────────────────────────


class TestPopularPickerDialog:
    def test_dialog_constructs_with_no_already_added(self, qapp):
        from jellytoast.radio_view import _PopularPickerDialog

        dlg = _PopularPickerDialog(already_added_urls=set())
        # Every preset has a row keyed by its stream URL.
        assert len(dlg._rows_by_url) == len(POPULAR_STATIONS)
        for preset in POPULAR_STATIONS:
            row = dlg._rows_by_url[preset["streamUrl"]]
            assert row._add_btn.isEnabled()
            assert row._add_btn.text() == "Add"

    def test_already_added_urls_render_as_added_disabled(self, qapp):
        from jellytoast.radio_view import _PopularPickerDialog

        url = POPULAR_STATIONS[0]["streamUrl"]
        dlg = _PopularPickerDialog(already_added_urls={url})
        row = dlg._rows_by_url[url]
        assert not row._add_btn.isEnabled()
        assert row._add_btn.text() == "Added"
        # Other rows stay clickable.
        other = POPULAR_STATIONS[1]
        assert dlg._rows_by_url[other["streamUrl"]]._add_btn.isEnabled()

    def test_mark_added_flips_row(self, qapp):
        from jellytoast.radio_view import _PopularPickerDialog

        url = POPULAR_STATIONS[0]["streamUrl"]
        dlg = _PopularPickerDialog(already_added_urls=set())
        row = dlg._rows_by_url[url]
        assert row._add_btn.isEnabled()
        dlg.mark_added(url)
        assert not row._add_btn.isEnabled()
        assert row._add_btn.text() == "Added"

    def test_mark_added_for_unknown_url_is_silent(self, qapp):
        from jellytoast.radio_view import _PopularPickerDialog

        dlg = _PopularPickerDialog(already_added_urls=set())
        # Must not raise.
        dlg.mark_added("https://not-in-list.example.com/stream")

    def test_add_requested_fires_with_preset_dict(self, qapp):
        from jellytoast.radio_view import _PopularPickerDialog

        dlg = _PopularPickerDialog(already_added_urls=set())
        captured = []
        dlg.add_requested.connect(lambda p: captured.append(p))

        preset = POPULAR_STATIONS[0]
        row = dlg._rows_by_url[preset["streamUrl"]]
        row._add_btn.click()

        assert len(captured) == 1
        assert captured[0]["streamUrl"] == preset["streamUrl"]
        assert captured[0]["name"] == preset["name"]
