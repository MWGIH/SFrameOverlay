#!/usr/bin/env python3
"""
Frame FPS Overlay - a head-locked SteamVR performance overlay for the Steam Frame.

Shows FPS, frame time, CPU %, GPU %, battery level (with charge icon or current
draw) and resolution in a corner of your view on a semi-opaque background.

FPS/frame time come from the SteamVR compositor for VR games, and from gamescope
(the compositor that shows flat games on a virtual screen) for flat games.

Everything is controlled from one place: the "FPS Overlay" tab in the SteamVR
dashboard (on/off, position, size, background, update rate, reset, update,
uninstall).
"""

import collections
import configparser
import ctypes
import errno
import glob
import os
import signal
import struct
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
    # How often the numbers change. The panel itself is redrawn in sync with the display.
    "update_ms": "500",
    "show_resolution": "0",
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

    def head_transform(self):
        m = openvr.HmdMatrix34_t()
        m[0][0] = m[1][1] = m[2][2] = 1.0
        m[0][3] = -self.margin_x if "left" in self.corner else self.margin_x
        m[1][3] = self.margin_y if "top" in self.corner else -self.margin_y
        m[2][3] = -self.distance
        return m


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


class GamescopeFrameStats:
    """
    FPS, frame time and output resolution of a flat game, read from gamescope.

    gamescope publishes per-frame stats for MangoHud's "mangoapp" on a SysV message
    queue (see gamescope src/mangoapp.cpp). We read the same messages. If Steam's own
    performance overlay is also reading them we only see some of the frames, which is
    fine because each message carries its own frame time.
    """

    # struct mangoapp_msg_v1 after the `long msg_type` header field, packed.
    MSG = struct.Struct("<IIQBBQQIIHB40sBBI")
    NO_VALUE = 2 ** 64 - 1
    STEAM_FOCUSED_BIT = 0x02
    # Data streams: 1 is the legacy stream, per-connector streams are even numbers from 100.
    # Odd types are control messages meant for gamescope and must never be consumed.
    STREAM_TYPES = [1] + list(range(100, 116, 2))
    IPC_NOWAIT = 0o4000
    MSG_NOERROR = 0o10000
    BUF_SIZE = 1016

    def __init__(self):
        self.libc = ctypes.CDLL(None, use_errno=True)
        self.libc.ftok.argtypes = [ctypes.c_char_p, ctypes.c_int]
        self.libc.msgget.argtypes = [ctypes.c_int, ctypes.c_int]
        self.libc.msgrcv.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_long, ctypes.c_int]
        self.libc.msgrcv.restype = ctypes.c_ssize_t
        self.buf = ctypes.create_string_buffer(ctypes.sizeof(ctypes.c_long) + self.BUF_SIZE)
        self.msgid = -1
        self.next_lookup = 0.0
        self.discard_backlog = True
        self.app_frames = collections.deque()      # (received_at, frametime_ns) from the game itself
        self.visible_frames = collections.deque()  # (received_at, frametime_ns) as shown on screen
        self.last_seen = 0.0
        self.steam_focused = True
        self.resolution = None

    def _queue(self):
        if self.msgid >= 0:
            return self.msgid
        now = time.monotonic()
        if now < self.next_lookup:
            return -1
        self.next_lookup = now + 5.0
        # gamescope uses ftok("mangoapp", 65) relative to its working directory. That file normally doesn't
        # exist, so ftok returns -1 and that becomes the key; try what we compute too in case it does.
        for key in dict.fromkeys([self.libc.ftok(b"mangoapp", 65), -1]):
            msgid = self.libc.msgget(key, 0)
            if msgid >= 0:
                self.msgid = msgid
                self.discard_backlog = True
                break
        return self.msgid

    def poll(self):
        """Drains new gamescope messages. Call often so the queue never fills up."""
        msgid = self._queue()
        if msgid < 0:
            return
        now = time.monotonic()
        offset = ctypes.sizeof(ctypes.c_long)
        for msg_type in self.STREAM_TYPES:
            while True:
                received = self.libc.msgrcv(msgid, self.buf, self.BUF_SIZE, msg_type,
                                            self.IPC_NOWAIT | self.MSG_NOERROR)
                if received < 0:
                    if ctypes.get_errno() in (errno.EIDRM, errno.EINVAL):
                        self.msgid = -1
                        return
                    break
                if received < self.MSG.size or self.discard_backlog:
                    continue
                (version, _pid, app_ns, _fsr, _sharp, visible_ns, _latency, width, height,
                 _refresh, flags, _engine, _up, _wanted, _scanout) = self.MSG.unpack_from(self.buf.raw, offset)
                if version != 1:
                    continue
                self.last_seen = now
                self.steam_focused = bool(flags & self.STEAM_FOCUSED_BIT)
                if width and height:
                    self.resolution = (width, height)
                if app_ns != self.NO_VALUE and 0 < app_ns < 1_000_000_000:
                    self.app_frames.append((now, app_ns))
                if visible_ns != self.NO_VALUE and 0 < visible_ns < 1_000_000_000:
                    self.visible_frames.append((now, visible_ns))
        # Whatever was queued before we attached is old; only count frames from here on.
        self.discard_backlog = False

    def active(self):
        """True while a flat game (not the Steam UI) is presenting frames through gamescope."""
        return time.monotonic() - self.last_seen < STALE_SECONDS and not self.steam_focused

    def sample(self, window):
        if not self.active():
            return None
        cutoff = time.monotonic() - window
        for frames in (self.app_frames, self.visible_frames):
            while frames and frames[0][0] < cutoff:
                frames.popleft()
        frames = self.app_frames or self.visible_frames
        if not frames:
            return None
        mean_ns = sum(ns for _, ns in frames) / len(frames)
        return 1e9 / mean_ns, mean_ns / 1e6


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
    GPU busy %. The Frame's Adreno GPU (msm driver) reports per-client busy time in
    /proc/<pid>/fdinfo, which is what tools like nvtop and MangoHud use. Falls back to
    the busy-percent files exposed by other drivers.
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
        """Busy nanoseconds per DRM client, summed over all GPU engines."""
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
            delta = sum(ns - prev_busy[c] for c, ns in busy.items() if c in prev_busy and ns >= prev_busy[c])
            return max(0.0, min(100.0, 100.0 * delta / ((now - prev_time) * 1e9)))
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


class HudRenderer:
    WIDTH = 480
    LINE = 52
    PADDING = 14

    def __init__(self):
        self.font = load_font(40)
        self.tag_font = load_font(22)

    @staticmethod
    def _percent_color(value):
        return GREEN if value < 70 else YELLOW if value < 90 else RED

    def _bolt(self, draw, x, y):
        """Charging icon: a lightning bolt roughly one text line tall."""
        s = self.LINE / 52
        points = [(14, 0), (2, 24), (12, 24), (6, 44), (24, 16), (14, 16), (20, 0)]
        draw.polygon([(x + px * s, y + py * s) for px, py in points], fill=GREEN, outline=OUTLINE, width=2)

    def render(self, background_alpha, source, frame, refresh_hz, cpu, gpu, battery, resolution, show_resolution):
        if frame is None:
            fps_text, fps_color, ft_text = "--", GREY, "--"
        else:
            fps, frame_ms = frame
            target = refresh_hz or 90.0
            fps_text = f"{fps:.0f}"
            fps_color = GREEN if fps >= target * 0.95 else YELLOW if fps >= target * 0.75 else RED
            ft_text = f"{frame_ms:.1f} ms"

        percent, charging, draw_ma = battery
        if percent is None:
            bat_text, bat_color = "--", GREY
        else:
            bat_text = f"{percent}%"
            bat_color = GREEN if charging or percent > 30 else YELLOW if percent > 15 else RED

        rows = [
            ("FPS", fps_text, fps_color),
            ("FT", ft_text, WHITE),
            ("CPU", "--" if cpu is None else f"{cpu:.0f}%", GREY if cpu is None else self._percent_color(cpu)),
            ("GPU", "--" if gpu is None else f"{gpu:.0f}%", GREY if gpu is None else self._percent_color(gpu)),
            ("BAT", bat_text, bat_color),
        ]
        if show_resolution:
            rows.append(("RES", "--" if resolution is None else f"{resolution[0]}x{resolution[1]}", WHITE))

        height = len(rows) * self.LINE + 2 * self.PADDING
        img = Image.new("RGBA", (self.WIDTH, height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        draw.rounded_rectangle((0, 0, self.WIDTH - 1, height - 1), radius=22, fill=(0, 0, 0, background_alpha))
        if source:
            draw.text((self.WIDTH - 18, 16), source, font=self.tag_font, fill=GREY, anchor="ra")

        x_label, x_value, y = 24, 124, self.PADDING
        for label, value, color in rows:
            # Thin dark stroke keeps the text readable even against bright scenes.
            draw.text((x_label, y), label, font=self.font, fill=GREY, stroke_width=2, stroke_fill=OUTLINE)
            draw.text((x_value, y), value, font=self.font, fill=color, stroke_width=2, stroke_fill=OUTLINE)
            if label == "BAT" and percent is not None:
                x_after = x_value + draw.textlength(value, font=self.font) + 14
                if charging:
                    self._bolt(draw, x_after, y + 2)
                elif draw_ma is not None:
                    draw.text((x_after, y), f"{draw_ma:.0f} mA", font=self.font, fill=WHITE,
                              stroke_width=2, stroke_fill=OUTLINE)
            y += self.LINE
        return img


class DoubleBufferedOverlay:
    """
    Two identical overlays that take turns, so the panel never flickers on update.

    Replacing an overlay's texture in place can leave it blank for a frame while the
    new texture loads. Instead, a new image goes to the hidden overlay, and on the next
    display frame (once it has loaded) that overlay is shown and the old one hidden.
    """

    def __init__(self, overlay, key, name):
        self.overlay = overlay
        self.handles = [overlay.createOverlay(f"{key}.{i}", f"{name} {i}") for i in range(2)]
        self.front = 0
        self.front_loaded = False
        self.pending = None      # image waiting to go to the back buffer
        self.back_loaded = False  # back buffer has a new image, ready to swap in
        self.visible = False

    def place(self, width_m, transform):
        for handle in self.handles:
            self.overlay.setOverlayWidthInMeters(handle, width_m)
            self.overlay.setOverlayTransformTrackedDeviceRelative(handle, openvr.k_unTrackedDeviceIndex_Hmd, transform)

    def submit(self, img):
        self.pending = img

    def set_visible(self, visible):
        self.visible = visible
        if visible and self.front_loaded:
            self.overlay.showOverlay(self.handles[self.front])
        elif not visible:
            for handle in self.handles:
                self.overlay.hideOverlay(handle)

    def step(self):
        """Advance one display frame."""
        back = 1 - self.front
        if self.back_loaded:
            # Show the new one before hiding the old one so there is never a frame with neither.
            if self.visible:
                self.overlay.showOverlay(self.handles[back])
            self.overlay.hideOverlay(self.handles[self.front])
            self.front, self.front_loaded, self.back_loaded = back, True, False
        elif self.pending is not None:
            img, self.pending = self.pending, None
            self.overlay.setOverlayRaw(self.handles[back], to_texture(img), img.width, img.height, 4)
            self.back_loaded = True


class Menu:
    """The 'FPS Overlay' tab in the SteamVR dashboard: every option for the overlay in one place."""
    WIDTH, HEIGHT = 800, 840
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

    def _row(self, draw, y, label, value, minus_action, plus_action):
        draw.text((60, y + 35), label, font=self.font, fill=GREY, anchor="lm")
        self._button(draw, (400, y, 480, y + 70), "-", minus_action)
        draw.text((580, y + 35), value, font=self.font, fill=WHITE, anchor="mm")
        self._button(draw, (680, y, 760, y + 70), "+", plus_action)

    def render(self, enabled, settings, status, confirm_uninstall):
        self.buttons = []
        img = Image.new("RGBA", (self.WIDTH, self.HEIGHT), self.BG)
        draw = ImageDraw.Draw(img)
        draw.text((self.WIDTH // 2, 50), "FPS Overlay", font=self.title_font, fill=WHITE, anchor="mm")

        self._button(draw, (60, 100, 740, 200), "ON" if enabled else "OFF", "toggle",
                     fill=self.ON if enabled else self.OFF, font=self.big_font)

        draw.text((60, 265), "Position", font=self.font, fill=GREY, anchor="lm")
        self._button(draw, (400, 230, 760, 300), settings.corner.replace("-", " ").capitalize(), "corner")

        self._row(draw, 320, "Size", f"{round(settings.width_m * 100)} cm", "size-", "size+")
        self._row(draw, 410, "Background", f"{round(settings.background_alpha / 255 * 100)}%", "bg-", "bg+")
        self._row(draw, 500, "Update every", f"{settings.update_ms} ms", "rate-", "rate+")

        draw.text((60, 625), "Resolution", font=self.font, fill=GREY, anchor="lm")
        self._button(draw, (400, 590, 760, 660), "Shown" if settings.show_resolution else "Hidden", "resolution",
                     fill=self.ON if settings.show_resolution else self.BUTTON)

        self._button(draw, (60, 690, 280, 760), "Reset", "reset")
        self._button(draw, (290, 690, 510, 760), "Update", "update")
        self._button(draw, (520, 690, 740, 760), "Confirm?" if confirm_uninstall else "Uninstall", "uninstall",
                     fill=self.DANGER)

        if status:
            draw.text((self.WIDTH // 2, 800), status, font=self.small_font, fill=YELLOW, anchor="mm")
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


def display_refresh_hz(vr_system):
    try:
        hz = vr_system.getFloatTrackedDeviceProperty(openvr.k_unTrackedDeviceIndex_Hmd,
                                                     openvr.Prop_DisplayFrequency_Float)
        return hz if hz > 0 else None
    except openvr.error_code.TrackedPropertyError:
        return None


def scene_is_vr_game(applications):
    """True when the app rendering in VR is a Steam VR game rather than the flat-game screen or SteamVR home."""
    try:
        pid = applications.getCurrentSceneProcessId()
        return bool(pid) and applications.getApplicationKeyByProcessId(pid).startswith("steam.app.")
    except openvr.error_code.ApplicationError:
        return False


def run():
    settings = Settings()
    if not os.path.exists(STATE_FILE):
        write_enabled(True)

    vr_system = openvr.init(openvr.VRApplication_Overlay)
    overlay = openvr.VROverlay()
    applications = openvr.VRApplications()
    vr_frames = VRFrameStats(openvr.VRCompositor())
    flat_frames = GamescopeFrameStats()
    cpu_stats, gpu_stats, battery_stats = CpuStats(), GpuStats(), BatteryStats(vr_system)
    hud_renderer = HudRenderer()
    hud = DoubleBufferedOverlay(overlay, APP_KEY + ".hud", "FPS Overlay HUD")
    menu = Menu()

    menu_handle, thumb_handle = overlay.createDashboardOverlay(APP_KEY + ".dashboard", "FPS Overlay")
    overlay.setOverlayWidthInMeters(menu_handle, 1.5)
    overlay.setOverlayInputMethod(menu_handle, openvr.VROverlayInputMethod_Mouse)
    mouse_scale = openvr.HmdVector2_t()
    mouse_scale[0], mouse_scale[1] = Menu.WIDTH, Menu.HEIGHT
    overlay.setOverlayMouseScale(menu_handle, mouse_scale)
    thumb = Menu.thumbnail()
    overlay.setOverlayRaw(thumb_handle, to_texture(thumb), thumb.width, thumb.height, 4)

    enabled = None
    state_mtime = None
    next_update = 0.0
    status = ""
    confirm_until = 0.0
    shown_confirm = False
    refresh_hz = display_refresh_hz(vr_system) or 90.0
    next_refresh_poll = 0.0
    event = openvr.VREvent_t()

    def apply_placement():
        hud.place(settings.width_m, settings.head_transform())

    def redraw_menu():
        img = menu.render(enabled, settings, status, time.monotonic() < confirm_until)
        overlay.setOverlayRaw(menu_handle, to_texture(img), img.width, img.height, 4)

    def handle(action):
        nonlocal status, confirm_until
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
        elif action in ("rate-", "rate+"):
            index = UPDATE_RATES_MS.index(settings.update_ms) + (1 if action == "rate+" else -1)
            settings.update_ms = UPDATE_RATES_MS[max(0, min(len(UPDATE_RATES_MS) - 1, index))]
        elif action == "reset":
            settings.reset()
            status = "Settings reset to defaults"
        elif action == "update":
            source = read_source_dir()
            if source and os.path.isdir(os.path.join(source, ".git")):
                status = "Updating... the overlay will restart"
                run_detached(f'cd "{source}" && git pull --ff-only && ./install.sh')
            else:
                status = "Update needs the original git checkout"
        elif action == "uninstall":
            if time.monotonic() < confirm_until:
                status = "Uninstalling..."
                run_detached(f'"{INSTALL_DIR}/uninstall.sh"')
            else:
                confirm_until = time.monotonic() + CONFIRM_SECONDS
                status = "Click again to uninstall"
        if action not in ("toggle", "update", "uninstall"):
            settings.save()
            apply_placement()
        redraw_menu()

    apply_placement()

    while True:
        frame_start = time.monotonic()

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
                hud.set_visible(enabled)
                redraw_menu()
                next_update = 0.0

        # Let the uninstall confirmation lapse on its own.
        confirming = time.monotonic() < confirm_until
        if shown_confirm and not confirming:
            status = ""
            redraw_menu()
        shown_confirm = confirming

        # Follow refresh-rate changes so the loop stays in step with the display.
        if frame_start >= next_refresh_poll:
            next_refresh_poll = frame_start + REFRESH_POLL_SECONDS
            refresh_hz = display_refresh_hz(vr_system) or refresh_hz

        flat_frames.poll()

        if enabled and frame_start >= next_update:
            window = settings.update_ms / 1000
            next_update = frame_start + window
            # A VR game is measured by the SteamVR compositor; a flat game by gamescope, which shows it on
            # the virtual screen (the compositor would only see the screen itself).
            flat = None if scene_is_vr_game(applications) else flat_frames.sample(window)
            if flat is not None:
                source, frame, resolution = "FLAT", flat, flat_frames.resolution
            else:
                source, frame = "VR", vr_frames.sample(window)
                width, height = vr_system.getRecommendedRenderTargetSize()
                resolution = (width, height) if width and height else None
            hud.submit(hud_renderer.render(settings.background_alpha, source, frame, refresh_hz,
                                           cpu_stats.percent(), gpu_stats.percent(),
                                           battery_stats.read(), resolution, settings.show_resolution))

        hud.step()

        # One pass per display frame.
        time.sleep(max(0.0, 1.0 / refresh_hz - (time.monotonic() - frame_start)))


def main():
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    # Wait for SteamVR rather than launching it; systemd restarts us after SteamVR quits.
    while not steamvr_running():
        time.sleep(5)
    try:
        run()
    finally:
        openvr.shutdown()


if __name__ == "__main__":
    main()
