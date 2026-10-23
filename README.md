# Frame FPS Overlay

A small performance HUD for the Steam Frame that stays in the upper-left of your view:

```
FPS  90
FT   11.1 ms
BAT  78%
RES  2016x2240
```

- Semi-opaque black rounded background with outlined text, readable on bright scenes
- Refreshes every 500 ms
- FPS is green at or above 95% of the headset refresh rate, yellow at or above 75%, red below that
- Battery shows `+` while charging and turns yellow/red at 30% / 15%
- Toggle it on/off **inside the headset** from the SteamVR dashboard: a tab named **FPS Overlay** has a big ON/OFF button
- No Decky Loader needed: it runs as a SteamVR overlay app

## Install (on the headset)

From Desktop Mode (Konsole) on the Frame, or over SSH:

```bash
git clone <this repo> ~/FrameFpsOverlay   # or copy the folder over
cd ~/FrameFpsOverlay
chmod +x *.sh
./install.sh
```

Pass `--add-steam-shortcut` to also add a "toggle" entry to your Steam library.

The installer only writes to your home directory. It does not need root and does not touch the read-only SteamOS system files. It:

1. Creates a Python venv in `~/.local/share/frame-fps-overlay` with `openvr` (pyopenvr) and `pillow`
2. Downloads Valve's ARM64 `libopenvr_api.so` from the official ValveSoftware/openvr GitHub repo
3. Installs a systemd user service that waits for SteamVR and attaches to it automatically

## Use

- **Toggle in VR:** open the SteamVR dashboard, select the **FPS Overlay** tab, and click the button
- **Toggle from a shell:** `~/.local/share/frame-fps-overlay/toggle.sh` (or `toggle.sh on` / `toggle.sh off`)
- **Move or resize it:** edit `~/.config/frame-fps-overlay/config.ini`, then run `systemctl --user restart frame-fps-overlay`
- **Logs:** `journalctl --user -u frame-fps-overlay -f`
- **Uninstall:** `~/.local/share/frame-fps-overlay/uninstall.sh` (add `--purge` to also delete settings)

## What the numbers mean

| Field | Source |
|---|---|
| FPS | Frames the current VR app submitted to the SteamVR compositor in the last 500 ms |
| FT | Average app frame interval (`m_flClientFrameIntervalMs`) over the same window |
| BAT | Headset battery reported by SteamVR, falling back to the system battery in `/sys/class/power_supply` |
| RES | SteamVR's per-eye render target size, which includes your render-resolution setting |

## Limitations

- The stats come from the SteamVR compositor. With a VR game they show that game. With a flat (2D) game on a virtual screen, they show the screen/theater app, not the game's own frame rate. Use Steam's built-in performance overlay for flat games.
- RES is the resolution SteamVR asks apps to render at. A game that picks its own resolution can differ from it.
- This has not yet been tested on real Frame hardware.
