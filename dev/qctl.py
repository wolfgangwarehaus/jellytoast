#!/usr/bin/env python3
"""Drive the Qt Quick rig (dev/quick_rig.sh) from the shell.

    dev/qctl.py state                    where the app is (page, sheet, player)
    dev/qctl.py tree [--all]             on-screen items: names, text, geometry
    dev/qctl.py tap TARGET               TARGET: objectName | text:Some text | x,y
    dev/qctl.py long TARGET              long-press
    dev/qctl.py drag TARGET DX DY [MS]   press, move by DX,DY over MS, release
    dev/qctl.py swipe up|down|left|right [TARGET]
    dev/qctl.py edge                     back gesture from the left edge
    dev/qctl.py key esc|back|enter|…     a key press
    dev/qctl.py type TEXT                type text into the focused field
    dev/qctl.py wait TARGET [SECS]       until TARGET is on screen
    dev/qctl.py idle                     until loading / transitions settle
    dev/qctl.py shot [PATH]              screenshot (prints the path)
    dev/qctl.py fake-np [TITLE [ARTIST [ALBUM]]]   fill Now Playing, no audio
    dev/qctl.py js EXPR                  QML/JS in the window's scope
    dev/qctl.py py EXPR | exec CODE      Python in the app (rig, player, albums,
                                         bus, qm, mpv, get_now_playing, …)
    dev/qctl.py ping

Talks to the rig's own bridge socket (JT_BRIDGE_SOCKET=jt-quick-rig-$USER,
TMPDIR=$JT_RIG_DIR/tmp), so a bridged desktop app can run alongside.
"""

from __future__ import annotations

import getpass
import json
import os
import sys
import time

RIG = os.environ.get("JT_RIG_DIR") or os.path.join(
    os.environ.get("XDG_RUNTIME_DIR") or "/tmp", "jt-quick-rig"
)
os.environ["TMPDIR"] = os.path.join(RIG, "tmp")
os.environ.setdefault("JT_BRIDGE_SOCKET", f"jt-quick-rig-{getpass.getuser()}")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from jt_ctl import call  # noqa: E402


def _target(s: str) -> str:
    """CLI target → Python literal for the rig."""
    if "," in s and not s.startswith("text:"):
        x, y = s.split(",", 1)
        return f"({float(x)}, {float(y)})"
    return repr(s)


def _code(argv: list[str]) -> tuple[str, str]:
    cmd, args = argv[0], argv[1:]
    a = lambda i, d=None: args[i] if len(args) > i else d  # noqa: E731
    if cmd == "ping":
        return "ping", ""
    if cmd == "state":
        return "eval", "rig.state()"
    if cmd == "tree":
        return "eval", f"rig.tree(all_items={'--all' in args})"
    if cmd == "tap":
        return "exec", f"rig.tap({_target(a(0))})"
    if cmd == "long":
        return "exec", f"rig.long_press({_target(a(0))})"
    if cmd == "drag":
        return (
            "exec",
            f"rig.drag({_target(a(0))}, dx={float(a(1, 0))}, dy={float(a(2, 0))}, ms={int(a(3, 300))})",
        )
    if cmd == "swipe":
        on = f", on={_target(a(1))}" if a(1) else ""
        return "exec", f"rig.swipe({a(0)!r}{on})"
    if cmd == "edge":
        return "exec", "rig.edge_swipe()"
    if cmd == "key":
        return "exec", f"rig.key({a(0)!r})"
    if cmd == "type":
        return "exec", f"rig.type({' '.join(args)!r})"
    if cmd == "wait":
        return "exec", f"rig.wait_for({a(0)!r}, timeout={float(a(1, 10))})"
    if cmd == "idle":
        return "exec", "rig.wait_idle()"
    if cmd == "shot":
        path = a(0) or os.path.join(RIG, "shots", time.strftime("%H%M%S") + ".png")
        return "eval", f"rig.shot({os.path.abspath(path)!r})"
    if cmd == "fake-np":
        return "exec", "rig.fake_now_playing(*{!r})".format(tuple(args[:3]))
    if cmd == "js":
        return "eval", f"rig.eval({' '.join(args)!r})"
    if cmd == "py":
        return "eval", " ".join(args)
    if cmd == "exec":
        return "exec", " ".join(args)
    raise SystemExit(f"unknown command {cmd!r}\n\n{__doc__}")


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        print(__doc__)
        return 2
    op, code = _code(sys.argv[1:])
    resp = call(op, code)
    if not resp.get("ok"):
        print(resp.get("error", resp), file=sys.stderr)
        tb = resp.get("traceback")
        if tb:
            print(tb.rstrip().splitlines()[-1], file=sys.stderr)
        return 1
    result = resp.get("result")
    if isinstance(result, str):
        print(result)
    elif result is not None:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
