#!/usr/bin/env bash
# Collects information for tracking down Flat-mode problems. Read-only: it changes nothing.
# Run it while a flat game is running with the overlay set to "Flat game" and Steam's
# performance overlay showing FPS, after ~30 seconds. Takes about 10 seconds.
# The report is printed and also saved to ~/frame-overlay-diagnostics.txt.
OUT="$HOME/frame-overlay-diagnostics.txt"

describe_pid() {
    local pid="$1"
    if [ -r "/proc/$pid/cmdline" ]; then
        echo "  $pid: $(tr '\0' ' ' < "/proc/$pid/cmdline" | cut -c1-300)"
    else
        echo "  $pid: (no longer running)"
    fi
}

{
    echo "=== Frame FPS Overlay diagnostics, $(date)"
    echo "--- overlay log (flat mode)"
    journalctl --user -u frame-fps-overlay -n 300 --no-pager | grep -E "Flat mode|Not starting|Traceback|Error" | tail -3

    echo "--- message queues, twice 5 s apart (does gamescope keep sending?)"
    cat /proc/sysvipc/msg
    sleep 5
    cat /proc/sysvipc/msg
    echo "--- last sender / receiver of each queue"
    tail -n +2 /proc/sysvipc/msg | while read -r _ _ _ _ _ lspid lrpid _; do
        describe_pid "$lspid"
        describe_pid "$lrpid"
    done

    echo "--- gamescope stats pipe: who has it open"
    for pipe in /run/user/"$(id -u)"/gamescope.*/stats.pipe; do
        echo "pipe: $pipe"
        for fd in /proc/[0-9]*/fd/*; do
            [ "$(readlink "$fd" 2>/dev/null)" = "$pipe" ] && describe_pid "$(echo "$fd" | cut -d/ -f3)"
        done
    done
    echo "--- gamescope runtime folder"
    ls -la /run/user/"$(id -u)"/gamescope.*/ 2>/dev/null

    echo "--- overlay / stats related processes"
    ps -eo pid,args | grep -E "gamescope|mangoapp|mangohud|gameoverlay|overlayui" | grep -v -E "grep|diagnose" | cut -c1-300
    echo "--- game processes"
    ps -eo pid,args | grep -E "SteamLaunch|reaper" | grep -v grep | cut -c1-300
    echo "--- Steam overlay library loaded into the game"
    for pid in $(pgrep -f "SteamLaunch"); do
        for child in $(pgrep -P "$pid") $pid; do
            grep -l -E "gameoverlayrenderer|mangohud" "/proc/$child/maps" 2>/dev/null | sed "s|/maps||"
        done
    done
    echo "--- mangohud config"
    ls -la /run/user/"$(id -u)"/gamescope-mangoapp-* 2>/dev/null && cat /run/user/"$(id -u)"/gamescope-mangoapp-* 2>/dev/null
} 2>&1 | tee "$OUT"

echo
echo "Saved to $OUT"
