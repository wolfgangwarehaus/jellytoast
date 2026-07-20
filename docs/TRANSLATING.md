# Translating jellytoast

jellytoast uses Qt's standard translation layer (#232). English is the source
language; every other language is a catalog pair in `jellytoast/i18n/`:

- `jellytoast_<code>.ts` — the editable XML catalog translators work on
- `jellytoast_<code>.qm` — the compiled binary the app actually loads

The app picks a language at startup: the **Settings → Display → Language**
override if set, otherwise the system locale. English needs no catalog —
untranslated strings always fall back to the English source text, so a
partially-translated catalog is fine to ship.

## Improving an existing language

1. Open `jellytoast/i18n/jellytoast_<code>.ts` in **Qt Linguist**
   (`pyside6-linguist`, installed with the dev venv) or any text editor.
   Each `<message>` pairs a `<source>` English string with a `<translation>`.
2. Keep `{0}`-style placeholders exactly as they appear — they're filled at
   runtime (`.format(...)`).
3. Compile + verify:

   ```bash
   dev/update_translations.sh          # recompiles every .qm
   .venv/bin/python -m pytest tests/test_i18n.py -q
   ```

4. Run the app in your language to eyeball it:
   pick the language under Settings → Display, restart.
5. Open a PR with **both** the `.ts` and the `.qm`.

## Starting a new language

```bash
dev/update_translations.sh fr     # bootstraps jellytoast_fr.ts + fills it
```

Then translate as above, and add the language to `SHIPPED_LANGUAGES` in
`jellytoast/i18n/__init__.py` (code, English name, native name) — the Settings
dropdown builds itself from that list.

## For developers: keeping strings translatable

- Wrap user-facing strings in `self.tr("...")` (QObject subclasses) or
  `QCoreApplication.translate("Context", "...")`.
- No f-strings inside `tr()` — use placeholders:
  `self.tr("Couldn't reach the server: {0}").format(msg)`.
- Don't translate product names (jellytoast, Jellyfin, Navidrome…) or
  URL/technical placeholders.
- After adding or changing strings, run `dev/update_translations.sh` so every
  catalog picks up the new sources (existing translations are preserved;
  changed strings show as "unfinished" until retranslated).

## Known not-yet-translatable strings

The full-app sweep (2026-07) wrapped ~740 strings. What remains English needs
small refactors (tracked on #232) because translating it naively would break
behavior — each is either evaluated at module import time (before translators
install) or doubles as an identity/persisted value:

- Settings nav page titles ("General", "Playback", "Casting"…) — compared by
  `show_page()` and used for deep-links; needs a key/label split.
- Import-time constant label lists: `AUDIO_QUALITIES`, `REPLAYGAIN_MODES`,
  `HOME_DESTINATIONS`, `LYRICS_FONT_SIZES`, `CAST_ROUTING_MODES`,
  `_THEME_MODE_CHOICES`, `ACCENT_PRESETS` tooltips, `_LINEAR_PHASE_INFO`,
  smart-playlist `_FIELD_LABELS`/`_SORT_LABELS` + preset names +
  `validate_rules` messages, `download_button._TIPS`, `downloads_view._KIND_LABEL`,
  top-bar `_LIBRARY_TABS`/`LIBRARY_SORT_OPTIONS`, `frosted_dialog` default
  button texts, radio `POPULAR_STATIONS` category headers.
- Identity strings: EQ preset "Custom" (persisted), session "Primary" fallback,
  cast section headers (product names anyway), taskbar overlay "play"/"pause"
  (doubles as a cache key), macOS native-menu lookups ("Services", "About Qt").
