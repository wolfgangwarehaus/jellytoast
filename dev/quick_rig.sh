#!/usr/bin/env bash
# Qt Quick front end dev rig — run the phone UI under remote control.
#
#   dev/quick_rig.sh start [--window] [--isolated] [--fresh] [--audible] [--size WxH]
#   dev/quick_rig.sh stop | status | log [N]
#
# start     JT_UI=quick with the test bridge on its own socket, in the
#           background. Drive it with dev/qctl.py (state, tree, tap, swipe,
#           shot, …).
#   --window    a real window on your desktop (default: offscreen, GPU path,
#               so screenshots show real covers + rounded corners).
#   --isolated  empty profile: not signed in. Default: a COPY of your
#               signed-in profile (~/.config/jellytoast) — the rig never
#               writes your real settings / queue. --fresh re-copies it.
#   --audible   real audio. Default is SILENT: mpv's null output, paced in
#               real time, so playback, positions and track changes are real.
#   --report    tell the server about plays (now playing, play counts).
#               Default: off, so rig runs never scrobble.
#   --size      window size, default 402x874 (a phone at 1×).
#
# State lives in $JT_RIG_DIR (default ${XDG_RUNTIME_DIR:-/tmp}/jt-quick-rig).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RIG="${JT_RIG_DIR:-${XDG_RUNTIME_DIR:-/tmp}/jt-quick-rig}"
PY="${ROOT}/.venv/bin/python"
[ -x "$PY" ] || PY=python3
SOCK="jt-quick-rig-$(id -un)"

running() { [ -f "$RIG/pid" ] && kill -0 "$(cat "$RIG/pid")" 2>/dev/null; }

cmd="${1:-}"; shift || true
case "$cmd" in
start)
    window=0 isolated=0 fresh=0 audible=0 report=0 size=402x874
    while [ $# -gt 0 ]; do
        case "$1" in
            --window) window=1 ;;
            --isolated) isolated=1 ;;
            --fresh) fresh=1 ;;
            --audible) audible=1 ;;
            --report) report=1 ;;
            --size) size="$2"; shift ;;
            *) echo "unknown option $1" >&2; exit 2 ;;
        esac
        shift
    done
    if running; then echo "rig already running (pid $(cat "$RIG/pid")) — dev/quick_rig.sh stop"; exit 1; fi
    mkdir -p "$RIG/tmp" "$RIG/shots"
    home="$RIG/home"
    [ "$isolated" = 1 ] && home="$RIG/home-isolated"
    [ "$fresh" = 1 ] && rm -rf "$home"
    mkdir -p "$home/.config" "$home/.cache" "$home/.local/share"
    if [ "$isolated" = 0 ] && [ ! -d "$home/.config/jellytoast" ] && [ -d "$HOME/.config/jellytoast" ]; then
        cp -r "$HOME/.config/jellytoast" "$home/.config/"
        echo "copied your profile into $home (your real one is untouched)"
    fi
    env=(
        JT_UI=quick JT_TEST_BRIDGE=1 JT_BRIDGE_SOCKET="$SOCK" JT_INSTANCE_KEY=jt-quick-rig
        JT_QUICK_SIZE="$size" TMPDIR="$RIG/tmp"
        XDG_CONFIG_HOME="$home/.config" XDG_CACHE_HOME="$home/.cache" XDG_DATA_HOME="$home/.local/share"
    )
    [ "$audible" = 1 ] || env+=(JT_AUDIO_NULL=1)
    [ "$report" = 1 ] || env+=(JT_NO_PLAY_REPORTS=1)
    [ "$window" = 1 ] || env+=(QT_QPA_PLATFORM=offscreen QSG_RHI_BACKEND=opengl QT_QUICK_BACKEND=rhi)
    cd "$ROOT"
    nohup env "${env[@]}" "$PY" -m jellytoast >"$RIG/log" 2>&1 &
    echo $! >"$RIG/pid"
    for _ in $(seq 1 60); do
        if "$PY" "$ROOT/dev/qctl.py" ping >/dev/null 2>&1; then
            echo "rig up (pid $(cat "$RIG/pid"); $([ "$window" = 1 ] && echo window || echo offscreen);" \
                 "$([ "$audible" = 1 ] && echo AUDIBLE || echo silent);" \
                 "$([ "$isolated" = 1 ] && echo isolated || echo 'copied profile')) — try: dev/qctl.py state"
            exit 0
        fi
        running || { echo "rig exited — log:"; tail -20 "$RIG/log"; exit 1; }
        sleep 0.5
    done
    echo "rig started but the bridge didn't answer — dev/quick_rig.sh log"; exit 1
    ;;
stop)
    if running; then kill "$(cat "$RIG/pid")"; sleep 0.5; running && kill -9 "$(cat "$RIG/pid")"; fi
    rm -f "$RIG/pid"; echo "rig stopped"
    ;;
status)
    if running; then echo "running (pid $(cat "$RIG/pid"))"; else echo "not running"; exit 1; fi
    ;;
log)
    tail -n "${1:-40}" "$RIG/log"
    ;;
*)
    sed -n '2,23p' "$0" | sed 's/^# \{0,1\}//'; exit 2
    ;;
esac
