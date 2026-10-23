#!/usr/bin/env bash
# Removes Frame FPS Overlay. Keeps ~/.config/frame-fps-overlay unless --purge is given.
set -euo pipefail

INSTALL_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/frame-fps-overlay"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/frame-fps-overlay"
UNIT="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/frame-fps-overlay.service"

systemctl --user disable --now frame-fps-overlay.service 2>/dev/null || true
rm -f "$UNIT"
systemctl --user daemon-reload
rm -rf "$INSTALL_DIR"
[ "${1:-}" = "--purge" ] && rm -rf "$CONFIG_DIR"

echo "Frame FPS Overlay removed."
