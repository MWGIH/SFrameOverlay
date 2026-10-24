#!/usr/bin/env python3
"""
Frame FPS Overlay - a head-locked SteamVR performance overlay for the Steam Frame.

Shows FPS, frame time, CPU %, GPU %, battery level (with charge icon or current
draw) and, optionally, resolution in a corner of your view on a semi-opaque
background.

For VR games only: FPS and frame time come from the SteamVR compositor, which
only sees games rendering in VR. Flat games on the virtual screen aren't measured.

Everything is controlled from one place: the "VR FPS Overlay" tab in the SteamVR
dashboard.
"""

import configparser
import ctypes
import glob
import json
import os
import signal
import subprocess
import sys
import time

import openvr
from PIL import Image, ImageDraw, ImageFont

APP_KEY = "gamesinhand.framefpsoverlay"
INSTALL_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_DIR = os.path.join(os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")), "frame-fps-overlay")
STATE_FILE = os.path.join(CONFIG_DIR, "enabled")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.ini")
SOURCE_DIR_FILE = os.path.join(CONFIG_DIR, "source_dir")
# Counts starts that ended before running STABLE_SECONDS; see main().
START_GUARD_FILE = os.path.join(CONFIG_DIR, "failed_starts")
STABLE_SECONDS = 60.0
# Written by SFRecorder while it records: the recorded view and whether the HUD should be in it.
RECORDER_VIEW_FILE = os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "sfrecorder", "view.json")
RECORDER_POLL_SECONDS = 0.5
# Fraction of the recorded view the HUD may use, so it never touches the video's edge.
RECORDER_VIEW_MARGIN = 0.92
MAX_FAILED_STARTS = 2

TIMING_HISTORY = 256    # frame timings to fetch per refresh (enough for 1 s at 240 Hz)
CONFIRM_SECONDS = 5.0   # window for the second "Uninstall" click
STALE_SECONDS = 1.0     # a frame source with nothing newer than this is treated as idle
REFRESH_POLL_SECONDS = 1.0

CORNERS = ["top-left", "top-right", "bottom-left", "bottom-right"]
SIZE_STEP, SIZE_MIN, SIZE_MAX = 0.03, 0.12, 0.48
ALPHA_STEP = 32
UPDATE_RATES_MS = [100, 250, 500, 1000]

DEFAULTS = {
    "corner": "top-left",
    # Distance from the centre of view to the panel centre, in metres.
    "margin_x": "0.30",
    "margin_y": "0.20",
    "distance": "1.0",
    "width_m": "0.24",
    # Background opacity 0-255.
    "background_alpha": "190",
    # How often the numbers change.
    "update_ms": "500",
    "show_resolution": "0",
    # Pull the HUD in so it stays inside SFRecorder's recording.
    "keep_in_recording": "1",
}

FONT_CANDIDATES = [
    "/usr/share/fonts/TTF/DejaVuSansMono-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
    "/usr/share/fonts/noto/NotoSansMono-Bold.ttf",
    "/usr/share/fonts/liberation/LiberationMono-Bold.ttf",
    "/usr/share/fonts/TTF/LiberationMono-Bold.ttf",
]

WHITE = (255, 255, 255, 255)
GREY = (170, 170, 170, 255)
GREEN = (80, 230, 110, 255)
YELLOW = (255, 210, 60, 255)
RED = (255, 80, 80, 255)
OUTLINE = (0, 0, 0, 255)


def log(message):
    print(message, flush=True)


def load_font(size):
    for path in FONT_CANDIDATES + sorted(glob.glob("/usr/share/fonts/**/*Mono*Bold*.ttf", recursive=True)):
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    return ImageFont.load_default(size=size)


def read_text(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


class Settings:
    def __init__(self):
        self.load()

    def load(self):
        cfg = configparser.ConfigParser()
        cfg["overlay"] = DEFAULTS
        cfg.read(CONFIG_FILE)
        section = cfg["overlay"]
        self.corner = section.get("corner") if section.get("corner") in CORNERS else DEFAULTS["corner"]
        self.margin_x = section.getfloat("margin_x")
        self.margin_y = section.getfloat("margin_y")
        self.distance = section.getfloat("distance")
        self.width_m = section.getfloat("width_m")
        self.background_alpha = max(0, min(255, section.getint("background_alpha")))
        update_ms = section.getint("update_ms")
        self.update_ms = update_ms if update_ms in UPDATE_RATES_MS else int(DEFAULTS["update_ms"])
        self.show_resolution = section.getboolean("show_resolution")
        self.keep_in_recording = section.getboolean("keep_in_recording")

    def save(self):
        cfg = configparser.ConfigParser()
        cfg["overlay"] = {
            "corner": self.corner,
            "margin_x": f"{self.margin_x:.2f}",
            "margin_y": f"{self.margin_y:.2f}",
            "distance": f"{self.distance:.2f}",
            "width_m": f"{self.width_m:.2f}",
            "background_alpha": str(self.background_alpha),
            "update_ms": str(self.update_ms),
            "show_resolution": "1" if self.show_resolution else "0",
            "keep_in_recording": "1" if self.keep_in_recording else "0",
        }
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(CONFIG_FILE, "w") as f:
            cfg.write(f)

    def reset(self):
        try:
            os.remove(CONFIG_FILE)
        except OSError:
            pass
        self.load()

    def head_transform(self, height_m, view=None):
        """Placement in front of the head. With a recorder `view`, the panel is pulled in to fit inside it."""
        margin_x, margin_y = self.margin_x, self.margin_y
        if view and self.keep_in_recording:
            margin_x, margin_y = fit_in_view(self, height_m, view)
        m = openvr.HmdMatrix34_t()
        m[0][0] = m[1][1] = m[2][2] = 1.0
        m[0][3] = -margin_x if "left" in self.corner else margin_x
        m[1][3] = margin_y if "top" in self.corner else -margin_y
        m[2][3] = -self.distance
        return m


def fit_in_view(settings, height_m, view):
    """Margins that keep the whole panel inside the recorded view (tangents left/right/up/down).

    Margins only ever shrink: a HUD that already fits stays where the user put it.
    """
    half_w, half_h = settings.width_m / 2, height_m / 2
    side = view["left"] if "left" in settings.corner else view["right"]
    vertical = view["up"] if "top" in settings.corner else view["down"]
    max_x = side * settings.distance * RECORDER_VIEW_MARGIN - half_w
    max_y = vertical * settings.distance * RECORDER_VIEW_MARGIN - half_h
    return max(0.0, min(settings.margin_x, max_x)), max(0.0, min(settings.margin_y, max_y))


def read_recorder_view():
    """SFRecorder's view while it is recording, or None. A file left behind by a recorder that died is ignored."""
    try:
        with open(RECORDER_VIEW_FILE) as f:
            view = json.load(f)
        os.kill(int(view["pid"]), 0)
        for key in ("left", "right", "up", "down"):
            view[key] = float(view[key])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return view


def read_enabled():
    return read_text(STATE_FILE) != "0"


def write_enabled(enabled):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(STATE_FILE, "w") as f:
        f.write("1\n" if enabled else "0\n")


def read_source_dir():
    """The git checkout install.sh was run from; the menu's Update button pulls into it."""
    return read_text(SOURCE_DIR_FILE)


def to_texture(img):
    data = img.tobytes()
    return (ctypes.c_ubyte * len(data)).from_buffer_copy(data)


def run_detached(command):
    """Runs a shell command in its own systemd scope so it survives this service being restarted or removed."""
    subprocess.Popen(["systemd-run", "--user", "--collect", "/bin/bash", "-c", command],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class VRFrameStats:
    """FPS and frame time of the VR app currently submitting frames to the SteamVR compositor."""

    def __init__(self, compositor):
        self.compositor = compositor
        self.timings = (openvr.Compositor_FrameTiming * TIMING_HISTORY)()
        self.newest_time = None
        self.newest_seen_at = 0.0

    def sample(self, window):
        """Returns (fps, frame_time_ms) over the last `window` seconds, or None when no app is rendering."""
        self.timings[0].m_nSize = ctypes.sizeof(openvr.Compositor_FrameTiming)
        count = self.compositor.function_table.getFrameTimings(ctypes.byref(self.timings[0]), TIMING_HISTORY)
        if count < 2:
            return None
        frames = self.timings[:count]
        newest = frames[-1].m_flSystemTimeInSeconds
        if newest != self.newest_time:
            self.newest_time = newest
            self.newest_seen_at = time.monotonic()
        if time.monotonic() - self.newest_seen_at > STALE_SECONDS:
            return None
        recent = [f for f in frames if newest - f.m_flSystemTimeInSeconds <= window]
        if len(recent) < 2:
            return None
        span = recent[-1].m_flSystemTimeInSeconds - recent[0].m_flSystemTimeInSeconds
        if span <= 0:
            return None
        fps = (len(recent) - 1) / span
        intervals = [f.m_flClientFrameIntervalMs for f in recent if f.m_flClientFrameIntervalMs > 0]
        frame_ms = sum(intervals) / len(intervals) if intervals else 1000.0 / fps
        return fps, frame_ms


class CpuStats:
    def __init__(self):
        self.prev = None

    def percent(self):
        line = read_text("/proc/stat")
        if not line:
            return None
        values = [int(v) for v in line.splitlines()[0].split()[1:]]
        idle = values[3] + (values[4] if len(values) > 4 else 0)  # idle + iowait
        total = sum(values[:8])  # guest time is already counted in user/nice
        prev, self.prev = self.prev, (idle, total)
        if prev is None or total == prev[1]:
            return None
        return max(0.0, min(100.0, 100.0 * (1 - (idle - prev[0]) / (total - prev[1]))))


class GpuStats:
    """
    GPU busy %. The Frame's Adreno GPU (msm driver) reports per-process busy time in
    /proc/<pid>/fdinfo, which is what tools like nvtop and MangoHud use. Falls back to
    the busy-percent files exposed by other drivers.

    The busy time of each process is measured from when the GPU starts its work to
    when it finishes, including any time a higher-priority job (such as the VR
    compositor) pre-empted it. Adding processes together therefore counts the same
    time more than once, so the busiest single process is used instead.
    """

    SYSFS_BUSY = [
        "/sys/class/kgsl/kgsl-3d0/gpu_busy_percentage",
        *sorted(glob.glob("/sys/class/drm/card*/device/gpu_busy_percent")),
    ]
    RESCAN_SECONDS = 2.0

    def __init__(self):
        self.drm_fdinfo = []
        self.next_scan = 0.0
        self.prev_busy = {}
        self.prev_time = None

    def _scan(self):
        paths = []
        for fd_dir in glob.glob("/proc/[0-9]*/fd"):
            try:
                for fd in os.listdir(fd_dir):
                    if os.readlink(os.path.join(fd_dir, fd)).startswith("/dev/dri/"):
                        paths.append(os.path.join(os.path.dirname(fd_dir), "fdinfo", fd))
            except OSError:
                continue
        self.drm_fdinfo = paths

    def _fdinfo_busy(self):
        """Busy nanoseconds per DRM client, summed over that client's GPU engines."""
        busy = {}
        for path in self.drm_fdinfo:
            text = read_text(path)
            if not text or "drm-client-id" not in text:
                continue
            client, ns = None, 0
            for line in text.splitlines():
                key, _, value = line.partition(":")
                if key == "drm-client-id":
                    client = value.strip()
                elif key.startswith("drm-engine-") and not key.startswith("drm-engine-capacity"):
                    ns += int(value.split()[0])
            if client is not None:
                busy[client] = max(busy.get(client, 0), ns)
        return busy

    def percent(self):
        now = time.monotonic()
        if now >= self.next_scan:
            self.next_scan = now + self.RESCAN_SECONDS
            self._scan()
        busy = self._fdinfo_busy()
        prev_busy, prev_time = self.prev_busy, self.prev_time
        self.prev_busy, self.prev_time = busy, now
        if busy and prev_time is not None:
            deltas = [ns - prev_busy[c] for c, ns in busy.items() if c in prev_busy and ns >= prev_busy[c]]
            if deltas:
                return max(0.0, min(100.0, 100.0 * max(deltas) / ((now - prev_time) * 1e9)))
        if not busy:
            for path in self.SYSFS_BUSY:
                value = read_text(path)
                if value:
                    try:
                        return float(value.split()[0].rstrip("%"))
                    except ValueError:
                        continue
        return None


class BatteryStats:
    """Headset battery: percent, charging state and, when on battery, current draw in mA."""

    def __init__(self, vr_system):
        self.vr_system = vr_system

    @staticmethod
    def _supply():
        for supply in sorted(glob.glob("/sys/class/power_supply/*")):
            if read_text(os.path.join(supply, "type")) == "Battery" and read_text(os.path.join(supply, "capacity")):
                return supply
        return None

    @staticmethod
    def _current_ma(supply):
        current = read_text(os.path.join(supply, "current_now"))  # microamps
        if current:
            return abs(int(current)) / 1000
        power = read_text(os.path.join(supply, "power_now"))      # microwatts
        voltage = read_text(os.path.join(supply, "voltage_now"))  # microvolts
        if power and voltage and int(voltage):
            return abs(int(power)) / int(voltage) * 1000
        return None

    def read(self):
        """Returns (percent, charging, draw_ma); any of them may be None."""
        percent, charging, draw_ma = None, False, None
        supply = self._supply()
        if supply:
            try:
                percent = int(read_text(os.path.join(supply, "capacity")))
                charging = read_text(os.path.join(supply, "status")) in ("Charging", "Full")
                draw_ma = self._current_ma(supply)
            except (TypeError, ValueError):
                pass
        hmd = openvr.k_unTrackedDeviceIndex_Hmd
        try:
            level = self.vr_system.getFloatTrackedDeviceProperty(hmd, openvr.Prop_DeviceBatteryPercentage_Float)
            if level > 0:
                percent = round(level * 100)
            charging = charging or bool(
                self.vr_system.getBoolTrackedDeviceProperty(hmd, openvr.Prop_DeviceIsCharging_Bool))
        except openvr.error_code.TrackedPropertyError:
            pass
        return percent, charging, None if charging else draw_ma


BOLT = "bolt"
SPACE = " "


class Hud:
    """
    The head-locked panel, made of three overlays at the same spot:

    * a background layer (panel and labels), only redrawn when a menu setting changes;
    * two text layers that take turns showing the values.

    Replacing an overlay's texture in place can leave it blank for a frame while the
    new texture loads, which shows up as a flicker. So new values are drawn onto the
    hidden text layer, given LOAD_FRAMES display frames to load, and then swapped in at
    the start of a frame. The background never changes during play, so it can't flicker.

    SteamVR has a small shared limit on overlays (128 for everything on the headset,
    including Steam's own windows and the dashboard), so the HUD deliberately
    stays at three.
    """

    WIDTH = 480
    LINE = 52
    PADDING = 14
    X_LABEL, X_VALUE = 24, 124
    ROWS = ["FPS", "FT", "CPU", "GPU", "BAT", "RES"]
    LOAD_FRAMES = 2

    def __init__(self, overlay):
        self.overlay = overlay
        self.font = load_font(40)
        self.advance = self.font.getlength("0")
        self.background = overlay.createOverlay(APP_KEY + ".hud", "FPS Overlay HUD")
        overlay.setOverlaySortOrder(self.background, 100)
        self.text = [overlay.createOverlay(f"{APP_KEY}.hud.text{i}", f"FPS Overlay HUD text {i}") for i in range(2)]
        for handle in self.text:
            overlay.setOverlaySortOrder(handle, 101)
        self.front = 0              # index of the text layer currently shown
        self.front_loaded = False
        self.loading_since = None   # frame counter when the back layer's texture was sent
        self.visible = False
        self.layout_key = None      # (alpha, rows) the background was drawn for
        self.rows = []
        self.values = None
        self.text_key = None        # (rows, values) last sent to a text layer
        self.pending = None         # text image waiting to go to the back layer

    def configure(self, settings, view=None):
        """Applies placement, size and background. The background is only redrawn if its look changed.

        `view` is SFRecorder's recorded view while it records (see read_recorder_view).
        """
        self.rows = [label for label in self.ROWS if settings.show_resolution or label != "RES"]
        layout_key = (settings.background_alpha, tuple(self.rows))
        if layout_key != self.layout_key:
            self.layout_key = layout_key
            img = self._render_background(settings.background_alpha)
            self.overlay.setOverlayRaw(self.background, to_texture(img), img.width, img.height, 4)
        height_m = settings.width_m * self._height() / self.WIDTH
        for handle in [self.background, *self.text]:
            self.overlay.setOverlayWidthInMeters(handle, settings.width_m)
            self.overlay.setOverlayTransformTrackedDeviceRelative(
                handle, openvr.k_unTrackedDeviceIndex_Hmd, settings.head_transform(height_m, view))
        self._queue_text()

    def set_visible(self, visible):
        self.visible = visible
        if visible:
            self.overlay.showOverlay(self.background)
            if self.front_loaded:
                self.overlay.showOverlay(self.text[self.front])
        else:
            for handle in [self.background, *self.text]:
                self.overlay.hideOverlay(handle)

    def show_values(self, values):
        """values: label -> list of (char, color)."""
        self.values = values
        self._queue_text()

    def _queue_text(self):
        if self.values is None:
            return
        key = (tuple(self.rows), repr(self.values))
        if key != self.text_key:
            self.text_key = key
            self.pending = self._render_text(self.values)

    def step(self, frame_counter):
        """Call once per display frame, just after vsync."""
        back = 1 - self.front
        if self.loading_since is not None:
            if frame_counter - self.loading_since >= self.LOAD_FRAMES:
                # Show the new layer before hiding the old one so there is never a frame with neither.
                if self.visible:
                    self.overlay.showOverlay(self.text[back])
                self.overlay.hideOverlay(self.text[self.front])
                self.front, self.front_loaded, self.loading_since = back, True, None
        elif self.pending is not None:
            img, self.pending = self.pending, None
            self.overlay.setOverlayRaw(self.text[back], to_texture(img), img.width, img.height, 4)
            self.loading_since = frame_counter

    def _height(self):
        return len(self.rows) * self.LINE + 2 * self.PADDING

    def _row_centre(self, row):
        return self.PADDING + self.LINE * row + self.LINE / 2

    def _render_background(self, alpha):
        img = Image.new("RGBA", (self.WIDTH, self._height()), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        draw.rounded_rectangle((0, 0, self.WIDTH - 1, img.height - 1), radius=22, fill=(0, 0, 0, alpha))
        for row, label in enumerate(self.rows):
            # Thin dark stroke keeps the text readable even against bright scenes.
            draw.text((self.X_LABEL, self._row_centre(row)), label, font=self.font, fill=GREY, anchor="lm",
                      stroke_width=2, stroke_fill=OUTLINE)
        return img

    def _render_text(self, values):
        """The values on a transparent canvas the same size as the background, so the layers line up."""
        img = Image.new("RGBA", (self.WIDTH, self._height()), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        for row, label in enumerate(self.rows):
            cy = self._row_centre(row)
            for i, (char, color) in enumerate(values.get(label, [])):
                cx = self.X_VALUE + self.advance * i + self.advance / 2
                if char == BOLT:
                    self._bolt(draw, cx, cy)
                elif char != SPACE:
                    draw.text((cx, cy), char, font=self.font, fill=color, anchor="mm",
                              stroke_width=2, stroke_fill=OUTLINE)
        return img

    def render(self, alpha, values):
        """The full panel as one image (background + text), for previews and tests."""
        img = self._render_background(alpha)
        img.alpha_composite(self._render_text(values))
        return img

    def _bolt(self, draw, cx, cy):
        """Charging icon: a lightning bolt roughly one text line tall."""
        s = self.LINE / 52
        points = [(14, 0), (2, 24), (12, 24), (6, 44), (24, 16), (14, 16), (20, 0)]
        draw.polygon([(cx + (px - 13) * s, cy + (py - 22) * s) for px, py in points],
                     fill=GREEN, outline=OUTLINE, width=2)


def colored(text, color):
    return [(c, color) for c in text]


def percent_color(value):
    return GREEN if value < 70 else YELLOW if value < 90 else RED


def format_values(frame, refresh_hz, cpu, gpu, battery, resolution):
    """The HUD's values as label -> list of (char, color)."""
    missing = colored("--", GREY)
    if frame is None:
        fps_cells = ft_cells = missing
    else:
        fps, frame_ms = frame
        target = refresh_hz or 90.0
        fps_color = GREEN if fps >= target * 0.95 else YELLOW if fps >= target * 0.75 else RED
        fps_cells = colored(f"{min(fps, 999):.0f}", fps_color)
        ft_cells = colored(f"{min(frame_ms, 999.9):.1f} ms", WHITE)

    percent, charging, draw_ma = battery
    if percent is None:
        bat_cells = missing
    else:
        bat_color = GREEN if charging or percent > 30 else YELLOW if percent > 15 else RED
        bat_cells = colored(f"{percent}%", bat_color)
        if charging:
            bat_cells += [(SPACE, WHITE), (BOLT, GREEN)]
        elif draw_ma is not None:
            bat_cells += colored(f" {min(draw_ma, 9999):.0f}mA", WHITE)

    return {
        "FPS": fps_cells,
        "FT": ft_cells,
        "CPU": missing if cpu is None else colored(f"{cpu:.0f}%", percent_color(cpu)),
        "GPU": missing if gpu is None else colored(f"{gpu:.0f}%", percent_color(gpu)),
        "BAT": bat_cells,
        "RES": missing if resolution is None else colored(f"{resolution[0]}x{resolution[1]}", WHITE),
    }


class Menu:
    """The 'FPS Overlay' tab in the SteamVR dashboard: every option for the overlay in one place."""
    WIDTH, HEIGHT = 800, 930
    BG = (24, 26, 32, 255)
    BUTTON = (58, 62, 74, 255)
    ON = (40, 170, 80, 255)
    OFF = (120, 40, 40, 255)
    DANGER = (150, 50, 50, 255)

    def __init__(self):
        self.title_font = load_font(52)
        self.big_font = load_font(64)
        self.font = load_font(34)
        self.small_font = load_font(26)
        self.buttons = []  # (x0, y0, x1, y1, action)

    def _button(self, draw, rect, text, action, fill=None, font=None):
        draw.rounded_rectangle(rect, radius=18, fill=fill or self.BUTTON)
        cx, cy = (rect[0] + rect[2]) // 2, (rect[1] + rect[3]) // 2
        draw.text((cx, cy), text, font=font or self.font, fill=WHITE, anchor="mm")
        self.buttons.append((*rect, action))

    def _choice(self, draw, y, label, value, action, fill=None):
        draw.text((60, y + 35), label, font=self.font, fill=GREY, anchor="lm")
        self._button(draw, (400, y, 760, y + 70), value, action, fill=fill)

    def _row(self, draw, y, label, value, minus_action, plus_action):
        draw.text((60, y + 35), label, font=self.font, fill=GREY, anchor="lm")
        self._button(draw, (400, y, 480, y + 70), "-", minus_action)
        draw.text((580, y + 35), value, font=self.font, fill=WHITE, anchor="mm")
        self._button(draw, (680, y, 760, y + 70), "+", plus_action)

    def render(self, enabled, settings, status, confirming):
        """`confirming` is the action waiting for a second click ("uninstall"), or None."""
        self.buttons = []
        img = Image.new("RGBA", (self.WIDTH, self.HEIGHT), self.BG)
        draw = ImageDraw.Draw(img)
        draw.text((self.WIDTH // 2, 50), "VR FPS Overlay", font=self.title_font, fill=WHITE, anchor="mm")

        self._button(draw, (60, 100, 740, 200), "ON" if enabled else "OFF", "toggle",
                     fill=self.ON if enabled else self.OFF, font=self.big_font)

        self._choice(draw, 230, "Position", settings.corner.replace("-", " ").capitalize(), "corner")
        self._row(draw, 320, "Size", f"{round(settings.width_m * 100)} cm", "size-", "size+")
        self._row(draw, 410, "Background", f"{round(settings.background_alpha / 255 * 100)}%", "bg-", "bg+")
        self._row(draw, 500, "Update every", f"{settings.update_ms} ms", "rate-", "rate+")
        self._choice(draw, 590, "Resolution", "Shown" if settings.show_resolution else "Hidden", "resolution",
                     fill=self.ON if settings.show_resolution else None)
        self._choice(draw, 680, "Fit recordings", "On" if settings.keep_in_recording else "Off", "keep-in-recording",
                     fill=self.ON if settings.keep_in_recording else None)

        self._button(draw, (60, 780, 280, 850), "Reset", "reset")
        self._button(draw, (290, 780, 510, 850), "Update", "update")
        self._button(draw, (520, 780, 740, 850), "Confirm?" if confirming == "uninstall" else "Uninstall",
                     "uninstall", fill=self.DANGER)

        if status:
            draw.text((self.WIDTH // 2, 890), status, font=self.small_font, fill=YELLOW, anchor="mm")
        return img

    def hit(self, x, y):
        for x0, y0, x1, y1, action in self.buttons:
            if x0 <= x <= x1 and y0 <= y <= y1:
                return action
        return None

    @staticmethod
    def thumbnail():
        img = Image.new("RGBA", (256, 256), Menu.BG)
        draw = ImageDraw.Draw(img)
        draw.text((128, 128), "FPS", font=load_font(96), fill=GREEN, anchor="mm")
        return img


def steamvr_running():
    """True if a SteamVR server is up. Background init never launches SteamVR itself."""
    try:
        openvr.init(openvr.VRApplication_Background)
    except openvr.error_code.InitError:
        return False
    openvr.shutdown()
    return True


def game_running():
    """True while Steam has a game running. Steam starts every game under `reaper SteamLaunch AppId=<id>`."""
    for path in glob.glob("/proc/[0-9]*/cmdline"):
        args = (read_text(path) or "").split("\0")
        if "SteamLaunch" in args and any(a.startswith("AppId=") and a != "AppId=0" for a in args):
            return True
    return False


def display_refresh_hz(vr_system):
    try:
        hz = vr_system.getFloatTrackedDeviceProperty(openvr.k_unTrackedDeviceIndex_Hmd,
                                                     openvr.Prop_DisplayFrequency_Float)
        return hz if hz > 0 else None
    except openvr.error_code.TrackedPropertyError:
        return None


VSYNC_MARGIN = 0.001  # wake this long after vsync, so the frame counter has moved on


def seconds_until_next_frame(vr_system, refresh_hz, frame_start):
    """Sleep time that wakes the loop just after the display's next vsync."""
    period = 1.0 / refresh_hz
    has_vsync, since_vsync, _ = vr_system.getTimeSinceLastVsync()
    if not has_vsync:
        return max(0.0, period - (time.monotonic() - frame_start))
    return (period - since_vsync % period) + VSYNC_MARGIN


def write_failed_starts(count):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(START_GUARD_FILE, "w") as f:
        f.write(f"{count}\n")


def run(on_stable):
    """Runs the overlay until SteamVR quits. Calls on_stable() once it has run for STABLE_SECONDS."""
    started = time.monotonic()
    stable_reported = False
    settings = Settings()
    if not os.path.exists(STATE_FILE):
        write_enabled(True)

    vr_system = openvr.init(openvr.VRApplication_Overlay)
    overlay = openvr.VROverlay()
    vr_frames = VRFrameStats(openvr.VRCompositor())
    cpu_stats, gpu_stats, battery_stats = CpuStats(), GpuStats(), BatteryStats(vr_system)
    hud = Hud(overlay)
    menu = Menu()

    menu_handle, thumb_handle = overlay.createDashboardOverlay(APP_KEY + ".dashboard", "VR FPS Overlay")
    overlay.setOverlayWidthInMeters(menu_handle, 1.5)
    overlay.setOverlayInputMethod(menu_handle, openvr.VROverlayInputMethod_Mouse)
    mouse_scale = openvr.HmdVector2_t()
    mouse_scale[0], mouse_scale[1] = Menu.WIDTH, Menu.HEIGHT
    overlay.setOverlayMouseScale(menu_handle, mouse_scale)
    thumb = Menu.thumbnail()
    overlay.setOverlayRaw(thumb_handle, to_texture(thumb), thumb.width, thumb.height, 4)

    enabled = None
    state_mtime = None
    recorder_view = None        # SFRecorder's view while it records
    next_recorder_poll = 0.0
    next_update = 0.0
    status = ""
    confirm_action = None  # "uninstall" while waiting for its second click
    confirm_until = 0.0
    refresh_hz = display_refresh_hz(vr_system) or 90.0
    next_refresh_poll = 0.0
    event = openvr.VREvent_t()

    def redraw_menu():
        img = menu.render(enabled, settings, status, confirm_action)
        overlay.setOverlayRaw(menu_handle, to_texture(img), img.width, img.height, 4)

    def ask_to_confirm(action, message):
        nonlocal status, confirm_action, confirm_until
        confirm_action, confirm_until = action, time.monotonic() + CONFIRM_SECONDS
        status = message

    def handle(action):
        nonlocal status, confirm_action
        confirmed = action == confirm_action and time.monotonic() < confirm_until
        confirm_action = None
        status = ""
        if action == "toggle":
            write_enabled(not enabled)
        elif action == "corner":
            settings.corner = CORNERS[(CORNERS.index(settings.corner) + 1) % len(CORNERS)]
        elif action in ("size-", "size+"):
            step = SIZE_STEP if action == "size+" else -SIZE_STEP
            settings.width_m = round(max(SIZE_MIN, min(SIZE_MAX, settings.width_m + step)), 2)
        elif action in ("bg-", "bg+"):
            step = ALPHA_STEP if action == "bg+" else -ALPHA_STEP
            settings.background_alpha = max(0, min(255, settings.background_alpha + step))
        elif action == "resolution":
            settings.show_resolution = not settings.show_resolution
        elif action == "keep-in-recording":
            settings.keep_in_recording = not settings.keep_in_recording
        elif action in ("rate-", "rate+"):
            index = UPDATE_RATES_MS.index(settings.update_ms) + (1 if action == "rate+" else -1)
            settings.update_ms = UPDATE_RATES_MS[max(0, min(len(UPDATE_RATES_MS) - 1, index))]
        elif action == "reset":
            settings.reset()
            status = "Settings reset to defaults"
        elif action == "update":
            source = read_source_dir()
            if not (source and os.path.isdir(os.path.join(source, ".git"))):
                status = "Update needs the original git checkout"
            elif game_running():
                # Updating restarts the overlay and re-runs the installer; never do that mid-game.
                status = "Exit your game first to update"
            else:
                status = "Updating the overlay... it will restart"
                run_detached(f'cd "{source}" && git pull --ff-only && ./install.sh')
        elif action == "uninstall":
            if confirmed:
                status = "Uninstalling..."
                run_detached(f'"{INSTALL_DIR}/uninstall.sh"')
            else:
                ask_to_confirm("uninstall", "Click again to uninstall")
        if action not in ("toggle", "update", "uninstall"):
            settings.save()
            hud.configure(settings, recorder_view)
        redraw_menu()

    def hud_visible():
        # SFRecorder can ask for the HUD to be left out of a recording.
        return enabled and not (recorder_view and recorder_view.get("hud") == "hide")

    hud.configure(settings)
    ticks = 0

    while True:
        frame_start = time.monotonic()
        ticks += 1

        # Swap HUD layers first thing after vsync, so the change lands at the start of a frame.
        has_vsync, _, frame_counter = vr_system.getTimeSinceLastVsync()
        hud.step(frame_counter if has_vsync else ticks)

        if not stable_reported and frame_start - started >= STABLE_SECONDS:
            stable_reported = True
            on_stable()

        while vr_system.pollNextEvent(event):
            if event.eventType == openvr.VREvent_Quit:
                vr_system.acknowledgeQuit_Exiting()
                return

        while overlay.pollNextOverlayEvent(menu_handle, event)[0]:
            if event.eventType == openvr.VREvent_MouseButtonUp:
                # OpenVR mouse coordinates have their origin at the bottom-left of the texture.
                action = menu.hit(event.data.mouse.x, Menu.HEIGHT - event.data.mouse.y)
                if action:
                    handle(action)
                    next_update = 0.0

        # The state file is the single source of truth for on/off.
        try:
            mtime = os.stat(STATE_FILE).st_mtime_ns
        except OSError:
            mtime = None
        if mtime != state_mtime or enabled is None:
            state_mtime = mtime
            new_state = read_enabled()
            if new_state != enabled:
                enabled = new_state
                hud.set_visible(hud_visible())
                redraw_menu()
                next_update = 0.0

        # Let an unanswered confirmation lapse on its own.
        if confirm_action and time.monotonic() >= confirm_until:
            confirm_action = None
            status = ""
            redraw_menu()

        if frame_start >= next_recorder_poll:
            next_recorder_poll = frame_start + RECORDER_POLL_SECONDS
            view = read_recorder_view()
            if view != recorder_view:
                recorder_view = view
                hud.configure(settings, recorder_view)
                hud.set_visible(hud_visible())

        if frame_start >= next_refresh_poll:
            next_refresh_poll = frame_start + REFRESH_POLL_SECONDS
            refresh_hz = display_refresh_hz(vr_system) or refresh_hz

        if enabled and frame_start >= next_update:
            window = settings.update_ms / 1000
            next_update = frame_start + window
            frame = vr_frames.sample(window)
            width, height = vr_system.getRecommendedRenderTargetSize()
            resolution = (width, height) if width and height else None
            hud.show_values(format_values(frame, refresh_hz, cpu_stats.percent(), gpu_stats.percent(),
                                          battery_stats.read(), resolution))

        time.sleep(seconds_until_next_frame(vr_system, refresh_hz, frame_start))


def main():
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    # Wait for SteamVR rather than launching it; systemd restarts us after SteamVR quits.
    while not steamvr_running():
        time.sleep(5)

    # Boot-loop guard: if the overlay ever takes the headset session down with it, the
    # next start would do the same. Every start that doesn't survive STABLE_SECONDS (or
    # end with a normal SteamVR quit) is counted; after MAX_FAILED_STARTS the overlay
    # stays off until it is reinstalled, which clears the count.
    try:
        failed_starts = int(read_text(START_GUARD_FILE) or 0)
    except ValueError:
        failed_starts = 0
    if failed_starts >= MAX_FAILED_STARTS:
        log(f"Not starting: the last {failed_starts} starts ended within {STABLE_SECONDS:.0f} s. "
            "Re-run the install line to clear this and try again.")
        while True:
            time.sleep(3600)  # stay idle so systemd doesn't keep restarting us
    write_failed_starts(failed_starts + 1)

    try:
        run(on_stable=lambda: write_failed_starts(0))
        write_failed_starts(0)  # SteamVR quit normally
    finally:
        openvr.shutdown()


if __name__ == "__main__":
    main()
