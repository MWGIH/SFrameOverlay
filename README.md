# Frame FPS Overlay

A small performance HUD for the Steam Frame that stays in a corner of your view (upper-left by default):

```
FPS  90                    VR
FT   11.2 ms
CPU  42%
GPU  77%
BAT  78% ⚡          (charging)
RES  2016x2240
```

```
FPS  59                  FLAT
FT   17.0 ms
CPU  93%
GPU  61%
BAT  24% 1450 mA     (on battery: current draw)
RES  1920x1080
```

- Works for both **VR games** and **flat games** on the virtual screen; the corner tag shows which one is being measured
- Semi-opaque black rounded background with outlined text, readable on bright scenes
- Flicker-free: the panel is redrawn in step with the display (it follows refresh-rate changes) and new numbers are swapped in without a blank frame
- Numbers update every 500 ms by default (adjustable in the menu)
- FPS is green at or above 95% of the headset refresh rate, yellow at or above 75%, red below that
- CPU / GPU are green below 70%, yellow below 90%, red above
- Battery shows a charge icon while charging, and the current draw in mA while on battery; it turns yellow/red at 30% / 15%
- No Decky Loader needed: it runs as a SteamVR overlay app

## Install

Open Konsole in Desktop Mode on the Frame (or SSH into it), paste this line and press Enter:

```bash
cd ~ && (git clone https://github.com/MWGIH/SFrameOverlay.git || git -C SFrameOverlay pull) && SFrameOverlay/install.sh
```

That's it. The overlay starts automatically whenever SteamVR is running. Running the same line again later updates it.

If this repo is private, `git clone` will ask you to sign in to GitHub. Use a [personal access token](https://github.com/settings/tokens) as the password.

## The menu

Everything else is done in the headset. Open the **SteamVR dashboard** and select the **FPS Overlay** tab:

| Option | What it does |
|---|---|
| **ON / OFF** | Shows or hides the overlay |
| **Position** | Cycles through top left, top right, bottom left and bottom right |
| **Size** − / + | Makes the panel smaller or larger |
| **Background** − / + | Changes how dark the background behind the text is |
| **Update every** − / + | How often the numbers change: 100, 250, 500 or 1000 ms |
| **Reset** | Puts every option back to its default |
| **Update** | Downloads the latest version and restarts the overlay |
| **Uninstall** | Removes the overlay and its settings (click twice to confirm) |

Changes apply straight away and are remembered between sessions.

## What the numbers mean

| Field | Source |
|---|---|
| FPS / FT (VR) | Frames the VR game submitted to the SteamVR compositor, averaged over the update interval |
| FPS / FT (FLAT) | Frame times gamescope reports for the flat game it is showing on the virtual screen (the same feed Steam's own performance overlay uses) |
| CPU | Overall CPU use from `/proc/stat` |
| GPU | GPU busy time from the graphics driver's per-process stats (`/proc/*/fdinfo`) |
| BAT | Headset battery %; while on battery, current draw from `/sys/class/power_supply` |
| RES | VR: SteamVR's per-eye render size. Flat: the resolution gamescope outputs the game at |

## Troubleshooting

If the overlay or its tab doesn't show up, or a value stays at `--`, this prints its log:

```bash
journalctl --user -u frame-fps-overlay -n 50
```

## Limitations

- Flat-game stats share gamescope's feed with Steam's built-in performance overlay. If you turn that on as well, both still work, but each only sees some of the frames, so Steam's own overlay may read a little off.
- Battery draw is current (mA). mAh is battery capacity, not draw, so it isn't shown.
- Update from the menu needs the repo to be public, or GitHub sign-in saved on the headset. Otherwise, re-run the install line.
- Uninstall leaves the downloaded `~/SFrameOverlay` folder; delete it yourself if you like.
