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

Coverage note: the string sweep is landing surface-by-surface — the sign-in
screen shipped first; the rest of the app is being wrapped in follow-up passes.
