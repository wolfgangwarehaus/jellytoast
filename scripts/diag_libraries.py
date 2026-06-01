"""One-off diagnostic: print what the active provider's get_libraries()
returns, and what the multi-library selection layer makes of it.

Run with:  python scripts/diag_libraries.py

Reads your persisted credentials (the same ones the app uses) and makes a
single read-only call to your server. Tells us whether the "Music" title
should show a dropdown (needs 2+ music libraries) and, if not, exactly what
the server reported — so we know if it's a data problem (the server only
exposes one music folder over the Subsonic API) vs a UI wiring problem.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from modules.providers import get_provider  # noqa: E402


def main() -> int:
    prov = get_provider()
    print(f"provider kind          : {getattr(prov, 'kind', '?')}")
    print(f"authenticated          : {prov.is_authenticated}")
    print(f"scopes_music_by_library: {getattr(prov, 'scopes_music_by_library', '?')}")
    try:
        libs = prov.get_libraries()
    except Exception as e:
        print(f"get_libraries() RAISED : {e!r}")
        return 1
    print(f"get_libraries() count  : {len(libs)}")
    for i, lib in enumerate(libs):
        print(
            f"  [{i}] Id={lib.get('Id')!r}  "
            f"Name={lib.get('Name')!r}  "
            f"CollectionType={lib.get('CollectionType')!r}"
        )
    from modules import library_selection as ls

    music = ls.music_libraries(libs)
    print(f"music libraries         : {len(music)}  "
          f"-> dropdown {'SHOWN' if len(music) >= 2 else 'HIDDEN'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
