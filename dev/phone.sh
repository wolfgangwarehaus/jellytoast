#!/usr/bin/env bash
# Run this checkout on the phone (OnePlus 6T, Arch + Plasma Mobile) without
# a release: the source goes to ~/.local/share/jellytoast-dev on the phone,
# and a user-level launcher with the same id as the AUR one makes the
# home-screen icon start it. The AUR install stays as it is; `undeploy`
# brings it back. No root on the phone needed.
#
#   dev/phone.sh deploy [--restart] [--bridge]   copy this tree over (+ relaunch)
#   dev/phone.sh restart [--bridge]              stop the app, start it again
#   dev/phone.sh stop                            stop it
#   dev/phone.sh status                          what's installed / running
#   dev/phone.sh log [N]                         app log (+ the dev unit's stderr)
#   dev/phone.sh shot [PATH]                     screenshot → PATH (prints it)
#   dev/phone.sh ctl ping|eval EXPR|exec CODE    test bridge (needs --bridge)
#   dev/phone.sh undeploy                        back to the AUR build
#   dev/phone.sh ssh [CMD…]                      a shell on the phone
#
# JT_PHONE=user@host overrides the target (default arch@fajita.local over
# Wi-Fi; arch@172.16.42.1 is the USB link). Restarting the app stops whatever
# it was playing — the phone is in daily use, so say so before doing it.
set -euo pipefail

ROOT="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
PHONE="${JT_PHONE:-arch@fajita.local}"
APP_ID=io.github.wolfgangwarehaus.jellytoast
REMOTE_DIR=.local/share/jellytoast-dev
UNIT=jellytoast-dev
SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=8)

on_phone() { ssh "${SSH_OPTS[@]}" "$PHONE" "$@"; }

remote_home() { on_phone 'printf %s "$HOME"'; }

# Every jellytoast process (AUR or dev build), never this ssh session's own
# shell (its command line mentions jellytoast too).
PIDS_CMD='pgrep -f "^(/usr/bin/)?python[0-9.]* (/usr/bin/jellytoast|-m jellytoast)" || true'

stop_app() {
    on_phone "systemctl --user stop $UNIT 2>/dev/null || true
        pids=\$($PIDS_CMD)
        [ -n \"\$pids\" ] && kill \$pids 2>/dev/null || true
        for _ in \$(seq 1 30); do [ -z \"\$($PIDS_CMD)\" ] && exit 0; sleep 0.2; done
        pids=\$($PIDS_CMD); [ -n \"\$pids\" ] && kill -9 \$pids 2>/dev/null || true"
}

start_app() {
    local bridge="${1:-0}" home env_args
    home="$(remote_home)"
    env_args="PYTHONPATH=$home/$REMOTE_DIR/src"
    [ "$bridge" = 1 ] && env_args="$env_args JT_TEST_BRIDGE=1"
    # A user service, so it lives in the Plasma session (WAYLAND_DISPLAY etc.
    # come from the user manager) and its stderr — QML warnings — lands in
    # the journal for `log`.
    on_phone "systemctl --user reset-failed $UNIT 2>/dev/null || true
        systemd-run --user --quiet --unit=$UNIT --collect -- env $env_args python3 -m jellytoast"
    echo "started the dev build on $PHONE$([ "$bridge" = 1 ] && echo ' (test bridge on)')"
}

cmd="${1:-}"; shift || true
case "$cmd" in
deploy)
    restart=0 bridge=0
    for a in "$@"; do
        case "$a" in
            --restart) restart=1 ;;
            --bridge) bridge=1; restart=1 ;;
            *) echo "unknown option $a" >&2; exit 2 ;;
        esac
    done
    home="$(remote_home)"
    on_phone "mkdir -p $REMOTE_DIR/src $REMOTE_DIR/dev .local/share/applications"
    rsync -az --delete --exclude __pycache__ --exclude '*.pyc' --exclude DEV_BUILD \
        -e "ssh ${SSH_OPTS[*]}" "$ROOT/jellytoast" "$ROOT/pyproject.toml" "$PHONE:$REMOTE_DIR/src/"
    rsync -az -e "ssh ${SSH_OPTS[*]}" "$ROOT/dev/jt_ctl.py" "$PHONE:$REMOTE_DIR/dev/"
    build="$(git -C "$ROOT" describe --always --dirty --abbrev=7) $(date '+%Y-%m-%d %H:%M')"
    on_phone "printf '%s\n' '$build' > $REMOTE_DIR/src/jellytoast/DEV_BUILD"
    # The launcher: same id as the packaged one, so the home screen's icon
    # (and the app's own window matching) use it; user-level entries win.
    sed -e "s|^Exec=.*|Exec=env PYTHONPATH=$home/$REMOTE_DIR/src python3 -m jellytoast|" \
        -e "s|^Name=.*|Name=jellytoast dev|" \
        "$ROOT/packaging/$APP_ID.desktop" |
        on_phone "cat > .local/share/applications/$APP_ID.desktop"
    on_phone "kbuildsycoca6 >/dev/null 2>&1 || true"
    echo "deployed $build to $PHONE"
    if [ "$restart" = 1 ]; then
        stop_app
        start_app "$bridge"
    fi
    ;;
restart)
    bridge=0; [ "${1:-}" = "--bridge" ] && bridge=1
    stop_app
    start_app "$bridge"
    ;;
stop)
    stop_app
    echo "stopped"
    ;;
status)
    on_phone "echo -n 'launcher: '; if [ -f .local/share/applications/$APP_ID.desktop ]; then echo dev; else echo AUR; fi
        echo -n 'dev build: '; cat $REMOTE_DIR/src/jellytoast/DEV_BUILD 2>/dev/null || echo none
        echo -n 'AUR: '; pacman -Q jellytoast 2>/dev/null || echo 'not installed'
        echo 'running:'; for p in \$($PIDS_CMD); do echo \"  \$p \$(tr '\0' ' ' </proc/\$p/cmdline)\"; done"
    ;;
log)
    n="${1:-60}"
    on_phone "tail -n $n .local/share/jellytoast/jellytoast/logs/jellytoast.log 2>/dev/null
        echo '--- $UNIT (stderr) ---'
        journalctl --user -u $UNIT -n $n --no-pager -o cat 2>/dev/null || true"
    ;;
shot)
    out="${1:-$ROOT/phone-shot.png}"
    on_phone "rm -f /tmp/jt-phone-shot.png
        systemd-run --user --quiet --wait --collect -- spectacle -b -n -f -o /tmp/jt-phone-shot.png >/dev/null 2>&1 || true
        test -s /tmp/jt-phone-shot.png" || { echo "screenshot failed" >&2; exit 1; }
    scp -q "${SSH_OPTS[@]}" "$PHONE:/tmp/jt-phone-shot.png" "$out"
    echo "$out"
    ;;
ctl)
    # Quote each argument for the remote shell.
    args=""
    for a in "$@"; do args="$args $(printf '%q' "$a")"; done
    on_phone "PYTHONPATH=$REMOTE_DIR/src python3 $REMOTE_DIR/dev/jt_ctl.py$args"
    ;;
undeploy)
    on_phone "rm -f .local/share/applications/$APP_ID.desktop; kbuildsycoca6 >/dev/null 2>&1 || true"
    echo "the home-screen icon starts the AUR build again (source left in ~/$REMOTE_DIR)"
    ;;
ssh)
    if [ $# -gt 0 ]; then on_phone "$@"; else ssh "${SSH_OPTS[@]/BatchMode=yes/BatchMode=no}" -t "$PHONE"; fi
    ;;
*)
    sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
    exit 2
    ;;
esac
