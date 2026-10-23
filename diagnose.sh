#!/usr/bin/env bash
# Collects information for tracking down Flat-mode problems. Read-only: it changes nothing.
# Run it while a flat game is running with the overlay set to "Flat game", after ~30 seconds.
# The report is printed and also saved to ~/frame-overlay-diagnostics.txt.
OUT="$HOME/frame-overlay-diagnostics.txt"

{
    echo "=== Frame FPS Overlay diagnostics, $(date)"
    echo "--- overlay log (flat mode)"
    journalctl --user -u frame-fps-overlay -n 300 --no-pager | grep -E "Flat mode|Not starting|Traceback|Error" | tail -5
    echo "--- message queues"
    cat /proc/sysvipc/msg
    echo "--- gamescope / mangoapp processes"
    ps -eo pid,args | grep -E "gamescope|mangoapp|mangohud" | grep -v grep | cut -c1-400
    echo "--- mangohud config files"
    ls -la /tmp/mangohud* 2>/dev/null || echo "none"
    echo "--- mangohud environment of running processes"
    for pid in $(pgrep -f "gamescope|mangoapp"); do
        tr '\0' '\n' < "/proc/$pid/environ" 2>/dev/null | grep -i mango | sed "s/^/$pid: /"
    done
} 2>&1 | tee "$OUT"

echo
echo "Saved to $OUT"
