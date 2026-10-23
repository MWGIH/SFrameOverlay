#!/usr/bin/env bash
# Installs Frame FPS Overlay for the current user (no root, no Decky Loader needed).
# Usage: ./install.sh [--add-steam-shortcut]
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
install -m 755 "$SRC_DIR/toggle.sh" "$INSTALL_DIR/toggle.sh"
install -m 755 "$SRC_DIR/uninstall.sh" "$INSTALL_DIR/uninstall.sh"
[ -f "$CONFIG_DIR/config.ini" ] || install -m 644 "$SRC_DIR/config.ini" "$CONFIG_DIR/config.ini"
[ -f "$CONFIG_DIR/enabled" ] || echo 1 > "$CONFIG_DIR/enabled"

echo "==> Creating Python environment"
python3 -m venv "$VENV"
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet "openvr==$PYOPENVR_VERSION" "pillow>=10.1"

# pyopenvr only bundles an x86_64 loader library; swap in Valve's own build for this CPU.
echo "==> Fetching Valve's OpenVR loader ($OPENVR_ARCH)"
PKG_DIR="$("$VENV/bin/python" -c 'import importlib.util, os; print(os.path.dirname(importlib.util.find_spec("openvr").origin))')"
curl -fsSL -o "$PKG_DIR/libopenvr_api_64.so" \
    "https://raw.githubusercontent.com/ValveSoftware/openvr/$OPENVR_TAG/bin/$OPENVR_ARCH/libopenvr_api.so"

echo "==> Installing systemd user service"
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

if [ "${1:-}" = "--add-steam-shortcut" ]; then
    if command -v steamos-add-to-steam >/dev/null; then
        steamos-add-to-steam "$INSTALL_DIR/toggle.sh"
        echo "==> Added 'toggle.sh' to your Steam library (rename it to 'Toggle FPS Overlay' if you like)"
    else
        echo "!! steamos-add-to-steam not found; add $INSTALL_DIR/toggle.sh as a non-Steam game manually" >&2
    fi
fi

echo
echo "Done. The overlay starts automatically whenever SteamVR is running."
echo "Toggle it from the SteamVR dashboard ('FPS Overlay' tab) or run: $INSTALL_DIR/toggle.sh"
echo "Adjust position/size/opacity in $CONFIG_DIR/config.ini, then: systemctl --user restart frame-fps-overlay"
