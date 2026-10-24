# Frame FPS Overlay

A small performance HUD **for VR games** on the Steam Frame. It stays in a corner of your view (upper-left by default):

```
FPS  90
FT   11.2 ms
CPU  42%
GPU  77%
BAT  78% ⚡          (charging)
```

```
FPS  72
FT   13.9 ms
CPU  93%
GPU  61%
BAT  24% 1450mA      (on battery: current draw)
RES  2016x2240       (only when Resolution is turned on in the menu)
```

- **VR games only.** FPS and frame time come from SteamVR, which only sees games rendering in VR. For flat games on the virtual screen, use Steam's own performance overlay instead.
- Semi-opaque black rounded background with outlined text, readable on bright scenes
- Flicker-free updates, synced to the display's refresh rate: the background never changes during play, and new numbers are drawn on a hidden layer, given 2 frames to load, then swapped in right after a vsync
- Uses just 3 SteamVR overlays for the panel, staying well within SteamVR's shared overlay limit
- Boot-loop guard: if the overlay fails to keep running twice in a row, it stays off until you reinstall it
- Numbers update every 500 ms by default (adjustable in the menu)
- FPS is green at or above 95% of the headset refresh rate, yellow at or above 75%, red below that
- CPU / GPU are green below 70%, yellow below 90%, red above
- Battery shows a charge icon while charging, and the current draw in mA while on battery; it turns yellow/red at 30% / 15%
- No Decky Loader needed: it runs as a SteamVR overlay app

## Install

You can install it straight from the headset. You don't need Desktop Mode.

1. **Turn on Developer Mode:** on the Frame, open **Settings → General** and turn on **Developer Mode**. This lets you open the web browser and Konsole (the terminal) directly.
2. **Open this page in the browser** so you can copy the install line below.
3. **Open Konsole,** paste the line and press Enter:

```bash
cd ~ && (git clone https://github.com/MWGIH/SFrameOverlay.git || git -C SFrameOverlay pull) && SFrameOverlay/install.sh
```

That's it. The overlay starts automatically whenever SteamVR is running. Running the same line again later updates it.

You can also run the install line from Konsole in Desktop Mode, or over SSH.

If this repo is private, `git clone` will ask you to sign in to GitHub. Use a [personal access token](https://github.com/settings/tokens) as the password.

## The menu

Everything else is done in the headset. Open the **SteamVR dashboard** and select the **VR FPS Overlay** tab:

| Option | What it does |
|---|---|
| **ON / OFF** | Shows or hides the overlay |
| **Position** | Cycles through top left, top right, bottom left and bottom right |
| **Size** − / + | Makes the panel smaller or larger |
| **Background** − / + | Changes how dark the background behind the text is |
| **Update every** − / + | How often the numbers change: 100, 250, 500 or 1000 ms |
| **Resolution** | Shows or hides the RES row (hidden by default) |
| **Reset** | Puts every option back to its default |
| **Update** | Downloads the latest version of the overlay (never your games) and restarts it. To avoid problems it won't run while a game is running: exit the game first |
| **Uninstall** | Removes the overlay and its settings (click twice to confirm) |

Changes apply straight away and are remembered between sessions.

## What the numbers mean

| Field | Source |
|---|---|
| FPS / FT | Frames the VR game submitted to the SteamVR compositor, averaged over the update interval |
| CPU | Overall CPU use from `/proc/stat` |
| GPU | GPU busy time from the graphics driver's per-process stats (`/proc/*/fdinfo`), taken from the busiest process |
| BAT | Headset battery %; while on battery, current draw from `/sys/class/power_supply` |
| RES (optional) | SteamVR's per-eye render size, which includes your render-resolution setting |

## Getting recordings onto your PC (preview)

`share_server.py` lets any browser on the same Wi-Fi play, download and delete the Frame's recordings. Nothing needs installing on the PC. It will become a **Share recordings** switch in the dashboard tab once the recorder is in. Until then you can try it from Konsole:

```bash
python3 ~/SFrameOverlay/share_server.py
```

It prints two addresses and a 4-digit PIN. Open either address on your PC or phone and enter the PIN. You can play a recording in the browser, download it, tick several to download as one zip, or delete them.

- It shares `~/Videos/framecorder` and its `clips` folder by default; use `--dir` for another folder.
- Recordings still being written (`.part` files) are hidden until they finish.
- It stops after 30 minutes without use, or when you press Ctrl+C.
- If the `.local` address doesn't open, use the one with the number (IP address).

## Troubleshooting

If the overlay or its tab doesn't show up, or a value stays at `--`, this prints its log:

```bash
journalctl --user -u frame-fps-overlay -n 50
```

### If the headset keeps crashing or restarting

Stop the overlay and keep it from starting on boot. Run this over SSH, or in Konsole as soon as you can open it:

```bash
systemctl --user disable --now frame-fps-overlay.service
```

The overlay also has its own guard. If it fails to keep running for a minute twice in a row, it stays off, and the log says `Not starting`. Running the install line again clears this and turns it back on.

## Limitations

- VR games only. With a flat game on the virtual screen, FPS / FT show the virtual screen itself, not the game; use Steam's performance overlay for flat games.
- RES is the resolution SteamVR asks VR games to render at. A game that picks its own resolution can differ from it.
- GPU % is an estimate. The Frame's driver only reports busy time per process, and that time includes moments when another job interrupted it, so the busiest process is shown rather than a true total.
- Battery draw is current (mA). mAh is battery capacity, not draw, so it isn't shown.
- Update from the menu needs the repo to be public, or GitHub sign-in saved on the headset. Otherwise, re-run the install line.
- Uninstall leaves the downloaded `~/SFrameOverlay` folder; delete it yourself if you like.
