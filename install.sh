#!/usr/bin/env bash
# Installs (or updates) Frame FPS Overlay for the current user. No root, no Decky Loader needed.
# All options are in the "FPS Overlay" tab of the SteamVR dashboard once installed.
set -euo pipefail

PYOPENVR_VERSION="2.12.1401"
OPENVR_TAG="v2.12.14"   # matches the pyopenvr version above

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/frame-fps-overlay"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/frame-fps-overlay"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
VENV="$INSTALL_DIR/venv"

case "$(uname -m)" in
    aarch64) OPENVR_ARCH="linuxarm64" ;;
    x86_64)  OPENVR_ARCH="linux64" ;;
    *) echo "Unsupported architecture: $(uname -m)" >&2; exit 1 ;;
esac

echo "==> Installing to $INSTALL_DIR"
mkdir -p "$INSTALL_DIR" "$CONFIG_DIR" "$UNIT_DIR"
install -m 755 "$SRC_DIR/frame_fps_overlay.py" "$INSTALL_DIR/frame_fps_overlay.py"
install -m 755 "$SRC_DIR/uninstall.sh" "$INSTALL_DIR/uninstall.sh"
[ -f "$CONFIG_DIR/enabled" ] || echo 1 > "$CONFIG_DIR/enabled"
# Lets the menu's Update button find this checkout.
echo "$SRC_DIR" > "$CONFIG_DIR/source_dir"
# Clears the boot-loop guard so a fresh install always gets to start.
rm -f "$CONFIG_DIR/failed_starts"

echo "==> Setting up Python environment"
[ -x "$VENV/bin/python" ] || python3 -m venv "$VENV"
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet "openvr==$PYOPENVR_VERSION" "pillow>=10.1"

# pyopenvr only bundles an x86_64 loader library; swap in Valve's own build for this CPU.
echo "==> Fetching Valve's OpenVR loader ($OPENVR_ARCH)"
PKG_DIR="$("$VENV/bin/python" -c 'import importlib.util, os; print(os.path.dirname(importlib.util.find_spec("openvr").origin))')"
curl -fsSL -o "$PKG_DIR/libopenvr_api_64.so" \
    "https://raw.githubusercontent.com/ValveSoftware/openvr/$OPENVR_TAG/bin/$OPENVR_ARCH/libopenvr_api.so"

echo "==> Installing background service"
cat > "$UNIT_DIR/frame-fps-overlay.service" <<EOF
[Unit]
Description=Frame FPS Overlay (SteamVR performance HUD)

[Service]
ExecStart=$VENV/bin/python $INSTALL_DIR/frame_fps_overlay.py
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
EOF
systemctl --user daemon-reload
systemctl --user enable frame-fps-overlay.service
systemctl --user restart frame-fps-overlay.service

echo
echo "Done. Put on the headset, open the SteamVR dashboard and pick the 'VR FPS Overlay' tab"
echo "to turn the overlay on/off, move it, resize it, change the background, update or uninstall."
