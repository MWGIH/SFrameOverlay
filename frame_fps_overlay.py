#!/usr/bin/env python3
"""
Frame FPS Overlay - a head-locked SteamVR performance overlay for the Steam Frame.

Shows FPS, frame time, CPU %, GPU %, battery level (with charge icon or current
draw) and, optionally, resolution in a corner of your view on a semi-opaque
background.

Mode is chosen in the menu: VR measures the game rendering in VR through the
SteamVR compositor; Flat measures the flat game shown on the virtual screen
through gamescope.

Everything is controlled from one place: the "FPS Overlay" tab in the SteamVR
dashboard.
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
# Counts starts that ended before running STABLE_SECONDS; see main().
START_GUARD_FILE = os.path.join(CONFIG_DIR, "failed_starts")
STABLE_SECONDS = 60.0
MAX_FAILED_STARTS = 2

TIMING_HISTORY = 256    # frame timings to fetch per refresh (enough for 1 s at 240 Hz)
CONFIRM_SECONDS = 5.0   # window for the second "Uninstall" click
STALE_SECONDS = 1.0     # a frame source with nothing newer than this is treated as idle
REFRESH_POLL_SECONDS = 1.0
FLAT_POLL_SECONDS = 0.1  # how often gamescope's stats queue is drained
DIAGNOSE_SECONDS = 30.0  # minimum gap between "no flat stats" log reports

MODES = ["vr", "flat"]
CORNERS = ["top-left", "top-right", "bottom-left", "bottom-right"]
SIZE_STEP, SIZE_MIN, SIZE_MAX = 0.03, 0.12, 0.48
ALPHA_STEP = 32
UPDATE_RATES_MS = [100, 250, 500, 1000]

DEFAULTS = {
    "mode": "vr",
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
        self.mode = section.get("mode") if section.get("mode") in MODES else DEFAULTS["mode"]
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
            "mode": self.mode,
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


class GamescopeStream:
    """Frames reported by gamescope for one of its windows (one message stream)."""

    def __init__(self):
        self.app_frames = collections.deque()      # (received_at, frametime_ns) from the game itself
        self.visible_frames = collections.deque()  # (received_at, frametime_ns) as shown on screen
        self.last_seen = 0.0
        self.steam_focused = True
        self.resolution = None

    def prune(self, cutoff):
        for frames in (self.app_frames, self.visible_frames):
            while frames and frames[0][0] < cutoff:
                frames.popleft()

    def frames(self):
        return self.app_frames or self.visible_frames


class GamescopeFrameStats:
    """
    FPS, frame time and output resolution of a flat game, read from gamescope.

    On the Frame, gamescope shows each flat window as a SteamVR overlay and publishes
    per-frame stats for MangoHud's "mangoapp" on a SysV message queue, one stream per
    window (see gamescope src/mangoapp.cpp). We read the same messages. If Steam's own
    performance overlay is also reading them we only see some of the frames, which is
    fine because each message carries its own frame time.
    """

    # struct mangoapp_msg_v1 after the `long msg_type` header field, packed.
    MSG = struct.Struct("<IIQBBQQIIHB40sBBI")
    NO_VALUE = 2 ** 64 - 1
    STEAM_FOCUSED_BIT = 0x02
    # Stream 1 is the legacy single stream; per-window streams are even numbers from 100 up.
    # Every stream's control channel is its number + 1 and belongs to gamescope, so odd
    # numbers (other than 1) are never consumed.
    LEGACY_TYPE = 1
    FIRST_CONNECTOR_TYPE = 100
    FALLBACK_TYPES = [1] + list(range(100, 400, 2))
    IPC_NOWAIT = 0o4000
    MSG_NOERROR = 0o10000
    MSG_COPY = 0o40000
    BUF_SIZE = 1016

    def __init__(self):
        self.libc = ctypes.CDLL(None, use_errno=True)
        self.libc.msgget.argtypes = [ctypes.c_int, ctypes.c_int]
        self.libc.msgrcv.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_long, ctypes.c_int]
        self.libc.msgrcv.restype = ctypes.c_ssize_t
        self.buf = ctypes.create_string_buffer(ctypes.sizeof(ctypes.c_long) + self.BUF_SIZE)
        self.msgid = -1
        self.next_lookup = 0.0
        self.next_poll = 0.0
        self.discard_backlog = True
        self.can_peek = True
        self.streams = {}
        self.last_diagnosis = 0.0

    @staticmethod
    def _queues():
        """Rows of /proc/sysvipc/msg as dicts."""
        text = read_text("/proc/sysvipc/msg")
        if not text:
            return []
        lines = text.splitlines()
        header = lines[0].split()
        return [dict(zip(header, line.split())) for line in lines[1:]]

    @staticmethod
    def _is_gamescope(pid):
        cmdline = read_text(f"/proc/{pid}/cmdline") or ""
        return "gamescope" in cmdline.split("\0")[0]

    def _queue(self):
        if self.msgid >= 0:
            return self.msgid
        now = time.monotonic()
        if now < self.next_lookup:
            return -1
        self.next_lookup = now + 5.0
        # Prefer the queue gamescope last sent to; gamescope keys it with ftok("mangoapp", 65), which is
        # normally -1 because that file doesn't exist in its working directory.
        queues = self._queues()
        chosen = next((q for q in queues if self._is_gamescope(q.get("lspid", "0"))), None)
        chosen = chosen or next((q for q in queues if q.get("key") == "-1"), None)
        if chosen:
            self.msgid = int(chosen["msqid"])
            self.discard_backlog = True
        return self.msgid

    def _stream_types(self, msgid):
        """The data stream numbers currently waiting in the queue, found by peeking without consuming."""
        if not self.can_peek:
            return self.FALLBACK_TYPES
        types = set()
        index = 0
        while True:
            if self.libc.msgrcv(msgid, self.buf, self.BUF_SIZE, index,
                                self.IPC_NOWAIT | self.MSG_COPY | self.MSG_NOERROR) < 0:
                if ctypes.get_errno() == errno.ENOSYS:
                    self.can_peek = False
                    return self.FALLBACK_TYPES
                break
            msg_type = ctypes.c_long.from_buffer(self.buf).value
            if msg_type == self.LEGACY_TYPE or (msg_type >= self.FIRST_CONNECTOR_TYPE and msg_type % 2 == 0):
                types.add(msg_type)
            index += 1
        return sorted(types)

    def poll(self):
        """Drains new gamescope messages. Call often so the queue never fills up."""
        now = time.monotonic()
        if now < self.next_poll:
            return
        # After a gap (start-up, or switching to Flat mode) the queue holds old frames; skip them.
        if now - self.next_poll > 1.0:
            self.discard_backlog = True
        self.next_poll = now + FLAT_POLL_SECONDS
        msgid = self._queue()
        if msgid < 0:
            return
        offset = ctypes.sizeof(ctypes.c_long)
        for msg_type in self._stream_types(msgid):
            stream = self.streams.setdefault(msg_type, GamescopeStream())
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
                stream.last_seen = now
                stream.steam_focused = bool(flags & self.STEAM_FOCUSED_BIT)
                if width and height:
                    stream.resolution = (width, height)
                if app_ns != self.NO_VALUE and 0 < app_ns < 1_000_000_000:
                    stream.app_frames.append((now, app_ns))
                if visible_ns != self.NO_VALUE and 0 < visible_ns < 1_000_000_000:
                    stream.visible_frames.append((now, visible_ns))
        # Whatever was queued before we attached is old; only count frames from here on.
        self.discard_backlog = False

    def sample(self, window):
        """Returns ((fps, frame_time_ms), resolution) for the busiest game window, or (None, None)."""
        now = time.monotonic()
        live = []
        for stream in self.streams.values():
            stream.prune(now - window)
            if now - stream.last_seen < STALE_SECONDS and stream.frames():
                live.append(stream)
        if not live:
            self._diagnose()
            return None, None
        # The game is the window that isn't Steam's own UI and is presenting the most frames.
        best = max(live, key=lambda s: (not s.steam_focused, len(s.frames())))
        frames = best.frames()
        mean_ns = sum(ns for _, ns in frames) / len(frames)
        return (1e9 / mean_ns, mean_ns / 1e6), best.resolution

    def _diagnose(self):
        """Logs what is available so a missing flat-game feed can be tracked down from the journal."""
        now = time.monotonic()
        if now - self.last_diagnosis < DIAGNOSE_SECONDS:
            return
        self.last_diagnosis = now
        queues = [f"msqid={q.get('msqid')} key={q.get('key')} qnum={q.get('qnum')} "
                  f"lspid={q.get('lspid')} lrpid={q.get('lrpid')}" for q in self._queues()]
        gamescopes = []
        for path in glob.glob("/proc/[0-9]*/cmdline"):
            args = (read_text(path) or "").split("\0")
            if args and "gamescope" in args[0]:
                gamescopes.append(" ".join(args)[:300])
        log(f"Flat mode: no gamescope frame stats. attached queue={self.msgid} peek={self.can_peek} "
            f"streams={sorted(self.streams)} queues={queues or 'none'} gamescope={gamescopes or 'not running'}")


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
    The head-locked panel: a single overlay.

    SteamVR has a small shared limit on overlays (128 for everything on the headset,
    including gamescope's flat-game windows and the dashboard), so the HUD must only
    ever use one. The texture is re-sent only when something on it actually changes.
    """

    WIDTH = 480
    LINE = 52
    PADDING = 14
    X_LABEL, X_VALUE = 24, 124
    ROWS = ["FPS", "FT", "CPU", "GPU", "BAT", "RES"]

    def __init__(self, overlay):
        self.overlay = overlay
        self.font = load_font(40)
        self.tag_font = load_font(22)
        self.advance = self.font.getlength("0")
        self.handle = overlay.createOverlay(APP_KEY + ".hud", "FPS Overlay HUD")
        overlay.setOverlaySortOrder(self.handle, 100)
        self.settings_key = None
        self.values = None
        self.shown_key = None

    def configure(self, settings):
        """Applies placement, size, background and row layout."""
        self.settings_key = (settings.background_alpha, settings.mode, settings.show_resolution)
        self.overlay.setOverlayWidthInMeters(self.handle, settings.width_m)
        self.overlay.setOverlayTransformTrackedDeviceRelative(
            self.handle, openvr.k_unTrackedDeviceIndex_Hmd, settings.head_transform())
        self._redraw()

    def set_visible(self, visible):
        if visible:
            self.overlay.showOverlay(self.handle)
        else:
            self.overlay.hideOverlay(self.handle)

    def show_values(self, values):
        """values: label -> list of (char, color)."""
        self.values = values
        self._redraw()

    def _redraw(self):
        if self.settings_key is None or self.values is None:
            return
        key = (self.settings_key, repr(self.values))
        if key == self.shown_key:
            return
        self.shown_key = key
        img = self.render(*self.settings_key, self.values)
        self.overlay.setOverlayRaw(self.handle, to_texture(img), img.width, img.height, 4)

    def render(self, alpha, mode, show_resolution, values):
        rows = [label for label in self.ROWS if show_resolution or label != "RES"]
        height = len(rows) * self.LINE + 2 * self.PADDING
        img = Image.new("RGBA", (self.WIDTH, height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        draw.rounded_rectangle((0, 0, self.WIDTH - 1, height - 1), radius=22, fill=(0, 0, 0, alpha))
        draw.text((self.WIDTH - 18, 16), mode.upper(), font=self.tag_font, fill=GREY, anchor="ra")
        for row, label in enumerate(rows):
            cy = self.PADDING + self.LINE * row + self.LINE / 2
            # Thin dark stroke keeps the text readable even against bright scenes.
            draw.text((self.X_LABEL, cy), label, font=self.font, fill=GREY, anchor="lm",
                      stroke_width=2, stroke_fill=OUTLINE)
            for i, (char, color) in enumerate(values.get(label, [])):
                cx = self.X_VALUE + self.advance * i + self.advance / 2
                if char == BOLT:
                    self._bolt(draw, cx, cy)
                elif char != SPACE:
                    draw.text((cx, cy), char, font=self.font, fill=color, anchor="mm",
                              stroke_width=2, stroke_fill=OUTLINE)
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
        draw.text((self.WIDTH // 2, 50), "FPS Overlay", font=self.title_font, fill=WHITE, anchor="mm")

        self._button(draw, (60, 100, 740, 200), "ON" if enabled else "OFF", "toggle",
                     fill=self.ON if enabled else self.OFF, font=self.big_font)

        self._choice(draw, 230, "Measure", "VR game" if settings.mode == "vr" else "Flat game", "mode")
        self._choice(draw, 320, "Position", settings.corner.replace("-", " ").capitalize(), "corner")
        self._row(draw, 410, "Size", f"{round(settings.width_m * 100)} cm", "size-", "size+")
        self._row(draw, 500, "Background", f"{round(settings.background_alpha / 255 * 100)}%", "bg-", "bg+")
        self._row(draw, 590, "Update every", f"{settings.update_ms} ms", "rate-", "rate+")
        self._choice(draw, 680, "Resolution", "Shown" if settings.show_resolution else "Hidden", "resolution",
                     fill=self.ON if settings.show_resolution else None)

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
    flat_frames = GamescopeFrameStats()
    cpu_stats, gpu_stats, battery_stats = CpuStats(), GpuStats(), BatteryStats(vr_system)
    hud = Hud(overlay)
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
        elif action == "mode":
            settings.mode = MODES[(MODES.index(settings.mode) + 1) % len(MODES)]
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
            hud.configure(settings)
        redraw_menu()

    hud.configure(settings)

    while True:
        frame_start = time.monotonic()

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
                hud.set_visible(enabled)
                redraw_menu()
                next_update = 0.0

        # Let an unanswered confirmation lapse on its own.
        if confirm_action and time.monotonic() >= confirm_until:
            confirm_action = None
            status = ""
            redraw_menu()

        if frame_start >= next_refresh_poll:
            next_refresh_poll = frame_start + REFRESH_POLL_SECONDS
            refresh_hz = display_refresh_hz(vr_system) or refresh_hz

        if settings.mode == "flat":
            flat_frames.poll()

        if enabled and frame_start >= next_update:
            window = settings.update_ms / 1000
            next_update = frame_start + window
            if settings.mode == "flat":
                frame, resolution = flat_frames.sample(window)
            else:
                frame = vr_frames.sample(window)
                width, height = vr_system.getRecommendedRenderTargetSize()
                resolution = (width, height) if width and height else None
            hud.show_values(format_values(frame, refresh_hz, cpu_stats.percent(), gpu_stats.percent(),
                                          battery_stats.read(), resolution))

        # Pace the loop to the display refresh rate.
        time.sleep(max(0.0, 1.0 / refresh_hz - (time.monotonic() - frame_start)))


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
