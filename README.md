# Frame FPS Overlay

A small performance HUD for the Steam Frame that stays in a corner of your view (upper-left by default):

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
| **Reset** | Puts every option back to its default |
| **Update** | Downloads the latest version and restarts the overlay |
| **Uninstall** | Removes the overlay and its settings (click twice to confirm) |

Changes apply straight away and are remembered between sessions.

## What the numbers mean

| Field | Source |
|---|---|
| FPS | Frames the current VR app submitted to the SteamVR compositor in the last 500 ms |
| FT | Average app frame interval (`m_flClientFrameIntervalMs`) over the same window |
| BAT | Headset battery reported by SteamVR, falling back to the system battery in `/sys/class/power_supply` |
| RES | SteamVR's per-eye render target size, which includes your render-resolution setting |

## Troubleshooting

If the overlay or its tab doesn't show up, this prints its log:

```bash
journalctl --user -u frame-fps-overlay -n 50
```

## Limitations

- The stats come from the SteamVR compositor. With a VR game they show that game. With a flat (2D) game on a virtual screen, they show the screen/theater app, not the game's own frame rate. Use Steam's built-in performance overlay for flat games.
- RES is the resolution SteamVR asks apps to render at. A game that picks its own resolution can differ from it.
- Update from the menu needs the repo to be public, or GitHub sign-in saved on the headset. Otherwise, re-run the install line.
- Uninstall leaves the downloaded `~/SFrameOverlay` folder; delete it yourself if you like.
- This has not yet been tested on real Frame hardware.
