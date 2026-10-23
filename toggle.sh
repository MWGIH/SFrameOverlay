#!/usr/bin/env bash
# Flips the overlay on/off. The running overlay picks the change up within 50 ms.
# Usage: toggle.sh [on|off]
set -euo pipefail

STATE_FILE="${XDG_CONFIG_HOME:-$HOME/.config}/frame-fps-overlay/enabled"
mkdir -p "$(dirname "$STATE_FILE")"

case "${1:-}" in
    on)  new=1 ;;
    off) new=0 ;;
    *)   [ "$(cat "$STATE_FILE" 2>/dev/null || echo 1)" = "0" ] && new=1 || new=0 ;;
esac

echo "$new" > "$STATE_FILE"
[ "$new" = "1" ] && echo "FPS overlay: ON" || echo "FPS overlay: OFF"
