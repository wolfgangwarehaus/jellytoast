#!/usr/bin/env bash
# Regenerate + compile translation catalogs (#232).
#
#   dev/update_translations.sh           # update every .ts, compile every .qm
#   dev/update_translations.sh es        # also CREATE jellytoast/i18n/jellytoast_es.ts
#
# pyside6-lupdate scans jellytoast/*.py for self.tr()/translate() strings and
# merges them into the .ts catalogs (existing translations are preserved;
# removed strings are marked obsolete, not deleted). pyside6-lrelease compiles
# each .ts into the .qm the app ships (package data in jellytoast/i18n/).
#
# Workflow for translators: edit the .ts in Qt Linguist (pyside6-linguist) or
# any text editor, then re-run this script to compile.
set -euo pipefail
cd "$(dirname "$0")/.."

I18N_DIR="jellytoast/i18n"

# Prefer the project venv's tools; fall back to PATH.
LUPDATE="$(command -v .venv/bin/pyside6-lupdate || command -v pyside6-lupdate)"
LRELEASE="$(command -v .venv/bin/pyside6-lrelease || command -v pyside6-lrelease)"

# Every app source file — the i18n package itself has no UI strings.
mapfile -t SOURCES < <(find jellytoast -name '*.py' -not -path "$I18N_DIR/*" | sort)

# New-language bootstrap: seed an empty catalog so lupdate fills it below.
for lang in "$@"; do
    ts="$I18N_DIR/jellytoast_${lang}.ts"
    if [ ! -f "$ts" ]; then
        printf '<?xml version="1.0" encoding="utf-8"?>\n<!DOCTYPE TS>\n<TS version="2.1" language="%s">\n</TS>\n' "$lang" > "$ts"
        echo "created $ts"
    fi
done

shopt -s nullglob
TS_FILES=("$I18N_DIR"/jellytoast_*.ts)
if [ ${#TS_FILES[@]} -eq 0 ]; then
    echo "no .ts catalogs in $I18N_DIR — pass a language code to create one" >&2
    exit 1
fi

"$LUPDATE" "${SOURCES[@]}" -ts "${TS_FILES[@]}"

for ts in "${TS_FILES[@]}"; do
    "$LRELEASE" "$ts" -qm "${ts%.ts}.qm"
done

echo
echo "Reminder: .qm files ship as package data — commit both .ts and .qm."
