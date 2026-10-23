# Frame FPS Overlay

A small performance HUD for the Steam Frame that stays in a corner of your view (upper-left by default):

```
FPS  90                    VR
FT   11.2 ms
CPU  42%
GPU  77%
BAT  78% ⚡          (charging)
```

```
FPS  59                  FLAT
FT   17.0 ms
CPU  93%
GPU  61%
BAT  24% 1450mA      (on battery: current draw)
RES  1920x1080       (only when Resolution is turned on in the menu)
```

- Measures **VR games** or **flat games** on the virtual screen; pick which in the menu, and the corner tag shows the current choice
- Semi-opaque black rounded background with outlined text, readable on bright scenes
- No flicker or stutter on updates: every character is drawn once at start-up, and updating a number only switches which pre-drawn character is shown, so nothing new is ever sent to SteamVR
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

Everything else is done in the headset. Open the **SteamVR dashboard** and select the **FPS Overlay** tab:

| Option | What it does |
|---|---|
| **ON / OFF** | Shows or hides the overlay |
| **Measure** | **VR game** (default) or **Flat game**: switch to Flat game while playing a flat game on the virtual screen |
| **Position** | Cycles through top left, top right, bottom left and bottom right |
| **Size** − / + | Makes the panel smaller or larger |
| **Background** − / + | Changes how dark the background behind the text is |
| **Update every** − / + | How often the numbers change: 100, 250, 500 or 1000 ms |
| **Resolution** | Shows or hides the RES row (hidden by default) |
| **Reset** | Puts every option back to its default |
| **Update** | Downloads the latest version of the overlay (never your games) and restarts it. If a game is running, it warns you first and you click again to go ahead |
| **Uninstall** | Removes the overlay and its settings (click twice to confirm) |

Changes apply straight away and are remembered between sessions.

## What the numbers mean

| Field | Source |
|---|---|
| FPS / FT (VR) | Frames the VR game submitted to the SteamVR compositor, averaged over the update interval |
| FPS / FT (FLAT) | Frame times gamescope reports for the flat game it is showing on the virtual screen (the same feed Steam's own performance overlay uses) |
| CPU | Overall CPU use from `/proc/stat` |
| GPU | GPU busy time from the graphics driver's per-process stats (`/proc/*/fdinfo`), taken from the busiest process |
| BAT | Headset battery %; while on battery, current draw from `/sys/class/power_supply` |
| RES (optional) | VR: SteamVR's per-eye render size. Flat: the resolution gamescope outputs the game at |

## Troubleshooting

If the overlay or its tab doesn't show up, or a value stays at `--`, this prints its log:

```bash
journalctl --user -u frame-fps-overlay -n 50
```

If FPS stays at `--` in **Flat game** mode, the log has a line starting `Flat mode: no gamescope frame stats`. It lists what the overlay could find, which shows why the flat-game feed is missing.

## Limitations

- Flat-game stats share gamescope's feed with Steam's built-in performance overlay. If you turn that on as well, both still work, but each only sees some of the frames, so Steam's own overlay may read a little off.
- GPU % is an estimate. The Frame's driver only reports busy time per process, and that time includes moments when another job interrupted it, so the busiest process is shown rather than a true total.
- Battery draw is current (mA). mAh is battery capacity, not draw, so it isn't shown.
- Update from the menu needs the repo to be public, or GitHub sign-in saved on the headset. Otherwise, re-run the install line.
- Uninstall leaves the downloaded `~/SFrameOverlay` folder; delete it yourself if you like.
