#!/usr/bin/env bash
# Removes Frame FPS Overlay and its settings. The downloaded source folder is left in place.
set -euo pipefail

INSTALL_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/frame-fps-overlay"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/frame-fps-overlay"
UNIT="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/frame-fps-overlay.service"
CACHE_DIR="${XDG_CACHE_HOME:-$HOME/.cache}/frame-fps-overlay"

systemctl --user disable --now frame-fps-overlay.service 2>/dev/null || true
rm -f "$UNIT"
systemctl --user daemon-reload
# Stop sharing recordings if share_server.py is still running, and drop its thumbnails.
pkill -f "python[0-9.]* .*share_server\.py" 2>/dev/null || true
rm -rf "$INSTALL_DIR" "$CONFIG_DIR" "$CACHE_DIR"

echo "Frame FPS Overlay removed."
