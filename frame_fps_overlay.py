#!/usr/bin/env python3
"""
Frame FPS Overlay - a head-locked SteamVR performance overlay for the Steam Frame.

Shows FPS, frame time, battery level and render resolution in the upper-left of
your view on a semi-opaque background. Updates every 500 ms.

Toggle it:
  * In the headset: SteamVR dashboard -> "FPS Overlay" tab -> click the button
  * From a shell:   ~/.local/share/frame-fps-overlay/toggle.sh
"""

import configparser
import ctypes
import glob
import os
import signal
import sys
import time

import openvr
from PIL import Image, ImageDraw, ImageFont

APP_KEY = "gamesinhand.framefpsoverlay"
CONFIG_DIR = os.path.join(os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")), "frame-fps-overlay")
STATE_FILE = os.path.join(CONFIG_DIR, "enabled")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.ini")

UPDATE_INTERVAL = 0.5   # seconds between stat/texture refreshes
EVENT_INTERVAL = 0.05   # seconds between event polls (keeps the dashboard button responsive)
TIMING_HISTORY = 256    # frame timings to fetch per refresh (enough for 500 ms at 240 Hz)

DEFAULTS = {
    # Position relative to the headset, in metres. -z is forward, +x right, +y up.
    "offset_x": "-0.30",
    "offset_y": "0.20",
    "distance": "1.0",
    "width_m": "0.24",
    # Background opacity 0-255.
    "background_alpha": "190",
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


def load_font(size):
    for path in FONT_CANDIDATES + sorted(glob.glob("/usr/share/fonts/**/*Mono*Bold*.ttf", recursive=True)):
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    return ImageFont.load_default(size=size)


def load_config():
    cfg = configparser.ConfigParser()
    cfg["overlay"] = DEFAULTS
    cfg.read(CONFIG_FILE)
    return cfg["overlay"]


def read_enabled():
    try:
        with open(STATE_FILE) as f:
            return f.read().strip() != "0"
    except OSError:
        return True


def write_enabled(enabled):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(STATE_FILE, "w") as f:
        f.write("1\n" if enabled else "0\n")


def sysfs_battery():
    """Fallback battery source: (percent, charging) from the first system battery."""
    for supply in sorted(glob.glob("/sys/class/power_supply/*")):
        try:
            with open(os.path.join(supply, "type")) as f:
                if f.read().strip() != "Battery":
                    continue
            with open(os.path.join(supply, "capacity")) as f:
                percent = int(f.read().strip())
            status = ""
            if os.path.exists(os.path.join(supply, "status")):
                with open(os.path.join(supply, "status")) as f:
                    status = f.read().strip()
            return percent, status in ("Charging", "Full")
        except (OSError, ValueError):
            continue
    return None, False


def to_texture(img):
    data = img.tobytes()
    return (ctypes.c_ubyte * len(data)).from_buffer_copy(data)


class Stats:
    def __init__(self, vr_system, compositor):
        self.vr_system = vr_system
        self.compositor = compositor
        self.timings = (openvr.Compositor_FrameTiming * TIMING_HISTORY)()
        self.newest_time = None
        self.newest_seen_at = 0.0

    def frame_stats(self):
        """Returns (fps, frame_time_ms) for the scene app over the last UPDATE_INTERVAL, or (None, None)."""
        self.timings[0].m_nSize = ctypes.sizeof(openvr.Compositor_FrameTiming)
        count = self.compositor.function_table.getFrameTimings(ctypes.byref(self.timings[0]), TIMING_HISTORY)
        if count < 2:
            return None, None
        frames = self.timings[:count]
        newest = frames[-1].m_flSystemTimeInSeconds
        if newest != self.newest_time:
            self.newest_time = newest
            self.newest_seen_at = time.monotonic()
        # Stale data means no app is submitting frames right now.
        if time.monotonic() - self.newest_seen_at > 1.0:
            return None, None
        recent = [f for f in frames if newest - f.m_flSystemTimeInSeconds <= UPDATE_INTERVAL]
        if len(recent) < 2:
            return None, None
        span = recent[-1].m_flSystemTimeInSeconds - recent[0].m_flSystemTimeInSeconds
        if span <= 0:
            return None, None
        fps = (len(recent) - 1) / span
        intervals = [f.m_flClientFrameIntervalMs for f in recent if f.m_flClientFrameIntervalMs > 0]
        frame_ms = sum(intervals) / len(intervals) if intervals else 1000.0 / fps
        return fps, frame_ms

    def refresh_rate(self):
        try:
            hz = self.vr_system.getFloatTrackedDeviceProperty(
                openvr.k_unTrackedDeviceIndex_Hmd, openvr.Prop_DisplayFrequency_Float)
            return hz if hz > 0 else None
        except openvr.error_code.TrackedPropertyError:
            return None

    def battery(self):
        """Returns (percent, charging). Prefers the headset's own battery as reported by SteamVR."""
        percent, charging = sysfs_battery()
        try:
            hmd = self.vr_system.getFloatTrackedDeviceProperty(
                openvr.k_unTrackedDeviceIndex_Hmd, openvr.Prop_DeviceBatteryPercentage_Float)
            if hmd > 0:
                percent = round(hmd * 100)
            charging = bool(self.vr_system.getBoolTrackedDeviceProperty(
                openvr.k_unTrackedDeviceIndex_Hmd, openvr.Prop_DeviceIsCharging_Bool))
        except openvr.error_code.TrackedPropertyError:
            pass
        return percent, charging

    def resolution(self):
        width, height = self.vr_system.getRecommendedRenderTargetSize()
        return (width, height) if width and height else None


class HudRenderer:
    WIDTH, HEIGHT = 480, 236

    def __init__(self, background_alpha):
        self.background = (0, 0, 0, max(0, min(255, background_alpha)))
        self.font = load_font(40)

    def render(self, fps, frame_ms, refresh_hz, battery, charging, resolution):
        img = Image.new("RGBA", (self.WIDTH, self.HEIGHT), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        draw.rounded_rectangle((0, 0, self.WIDTH - 1, self.HEIGHT - 1), radius=22, fill=self.background)

        if fps is None:
            fps_text, fps_color = "--", GREY
        else:
            fps_text = f"{fps:.0f}"
            target = refresh_hz or 90.0
            fps_color = GREEN if fps >= target * 0.95 else YELLOW if fps >= target * 0.75 else RED
        ft_text = "--" if frame_ms is None else f"{frame_ms:.1f} ms"

        if battery is None:
            bat_text, bat_color = "--", GREY
        else:
            bat_text = f"{battery}%" + (" +" if charging else "")
            bat_color = GREEN if charging or battery > 30 else YELLOW if battery > 15 else RED
        res_text = "--" if resolution is None else f"{resolution[0]}x{resolution[1]}"

        rows = [
            ("FPS", fps_text, fps_color),
            ("FT", ft_text, WHITE),
            ("BAT", bat_text, bat_color),
            ("RES", res_text, WHITE),
        ]
        x_label, x_value, y, line = 24, 124, 14, 52
        for label, value, color in rows:
            # Thin dark stroke keeps the text readable even against bright scenes.
            draw.text((x_label, y), label, font=self.font, fill=GREY, stroke_width=2, stroke_fill=(0, 0, 0, 255))
            draw.text((x_value, y), value, font=self.font, fill=color, stroke_width=2, stroke_fill=(0, 0, 0, 255))
            y += line
        return img


class DashboardPanel:
    """A tab in the SteamVR dashboard with a single big on/off button."""
    WIDTH, HEIGHT = 800, 400

    def __init__(self):
        self.title_font = load_font(56)
        self.button_font = load_font(96)
        self.hint_font = load_font(32)

    def render(self, enabled):
        img = Image.new("RGBA", (self.WIDTH, self.HEIGHT), (24, 26, 32, 255))
        draw = ImageDraw.Draw(img)
        draw.text((self.WIDTH // 2, 60), "FPS Overlay", font=self.title_font, fill=WHITE, anchor="mm")
        colour = (40, 170, 80, 255) if enabled else (120, 40, 40, 255)
        draw.rounded_rectangle((200, 120, 600, 300), radius=40, fill=colour)
        draw.text((self.WIDTH // 2, 210), "ON" if enabled else "OFF", font=self.button_font, fill=WHITE, anchor="mm")
        draw.text((self.WIDTH // 2, 350), "Click to toggle", font=self.hint_font, fill=GREY, anchor="mm")
        return img

    @staticmethod
    def thumbnail():
        img = Image.new("RGBA", (256, 256), (24, 26, 32, 255))
        draw = ImageDraw.Draw(img)
        draw.text((128, 128), "FPS", font=load_font(96), fill=GREEN, anchor="mm")
        return img


def head_transform(cfg):
    m = openvr.HmdMatrix34_t()
    m[0][0] = m[1][1] = m[2][2] = 1.0
    m[0][3] = cfg.getfloat("offset_x")
    m[1][3] = cfg.getfloat("offset_y")
    m[2][3] = -cfg.getfloat("distance")
    return m


def steamvr_running():
    """True if a SteamVR server is up. Background init never launches SteamVR itself."""
    try:
        openvr.init(openvr.VRApplication_Background)
    except openvr.error_code.InitError:
        return False
    openvr.shutdown()
    return True


def run():
    cfg = load_config()
    if not os.path.exists(STATE_FILE):
        write_enabled(True)

    vr_system = openvr.init(openvr.VRApplication_Overlay)
    overlay = openvr.VROverlay()
    stats = Stats(vr_system, openvr.VRCompositor())
    hud = HudRenderer(cfg.getint("background_alpha"))
    panel = DashboardPanel()

    hud_handle = overlay.createOverlay(APP_KEY + ".hud", "FPS Overlay HUD")
    overlay.setOverlayWidthInMeters(hud_handle, cfg.getfloat("width_m"))
    overlay.setOverlayTransformTrackedDeviceRelative(hud_handle, openvr.k_unTrackedDeviceIndex_Hmd, head_transform(cfg))
    overlay.setOverlaySortOrder(hud_handle, 100)

    dash_handle, thumb_handle = overlay.createDashboardOverlay(APP_KEY + ".dashboard", "FPS Overlay")
    overlay.setOverlayWidthInMeters(dash_handle, 1.5)
    overlay.setOverlayInputMethod(dash_handle, openvr.VROverlayInputMethod_Mouse)
    mouse_scale = openvr.HmdVector2_t()
    mouse_scale[0], mouse_scale[1] = DashboardPanel.WIDTH, DashboardPanel.HEIGHT
    overlay.setOverlayMouseScale(dash_handle, mouse_scale)
    thumb = DashboardPanel.thumbnail()
    overlay.setOverlayRaw(thumb_handle, to_texture(thumb), thumb.width, thumb.height, 4)

    enabled = None
    state_mtime = None
    next_update = 0.0
    event = openvr.VREvent_t()

    def set_enabled(value):
        nonlocal enabled
        enabled = value
        img = panel.render(enabled)
        overlay.setOverlayRaw(dash_handle, to_texture(img), img.width, img.height, 4)
        if enabled:
            overlay.showOverlay(hud_handle)
        else:
            overlay.hideOverlay(hud_handle)

    while True:
        while vr_system.pollNextEvent(event):
            if event.eventType == openvr.VREvent_Quit:
                vr_system.acknowledgeQuit_Exiting()
                return

        while overlay.pollNextOverlayEvent(dash_handle, event)[0]:
            if event.eventType == openvr.VREvent_MouseButtonUp:
                write_enabled(not enabled)

        # The state file is the single source of truth so toggle.sh and the dashboard stay in sync.
        try:
            mtime = os.stat(STATE_FILE).st_mtime_ns
        except OSError:
            mtime = None
        if mtime != state_mtime or enabled is None:
            state_mtime = mtime
            new_state = read_enabled()
            if new_state != enabled:
                set_enabled(new_state)
                next_update = 0.0

        now = time.monotonic()
        if enabled and now >= next_update:
            next_update = now + UPDATE_INTERVAL
            fps, frame_ms = stats.frame_stats()
            battery, charging = stats.battery()
            img = hud.render(fps, frame_ms, stats.refresh_rate(), battery, charging, stats.resolution())
            overlay.setOverlayRaw(hud_handle, to_texture(img), img.width, img.height, 4)

        time.sleep(EVENT_INTERVAL)


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
