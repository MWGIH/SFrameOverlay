#!/usr/bin/env python3
"""
Frame FPS Overlay - a head-locked SteamVR performance overlay for the Steam Frame.

Shows FPS, frame time, battery level and render resolution in a corner of your
view on a semi-opaque background. Updates every 500 ms.

Everything is controlled from one place: the "FPS Overlay" tab in the SteamVR
dashboard (on/off, position, size, background, reset, update, uninstall).
"""

import configparser
import ctypes
import glob
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

UPDATE_INTERVAL = 0.5   # seconds between stat/texture refreshes
EVENT_INTERVAL = 0.05   # seconds between event polls (keeps the menu responsive)
TIMING_HISTORY = 256    # frame timings to fetch per refresh (enough for 500 ms at 240 Hz)
CONFIRM_SECONDS = 5.0   # window for the second "Uninstall" click

CORNERS = ["top-left", "top-right", "bottom-left", "bottom-right"]
SIZE_STEP, SIZE_MIN, SIZE_MAX = 0.03, 0.12, 0.48
ALPHA_STEP = 32

DEFAULTS = {
    "corner": "top-left",
    # Distance from the centre of view to the panel centre, in metres.
    "margin_x": "0.30",
    "margin_y": "0.20",
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
OUTLINE = (0, 0, 0, 255)


def load_font(size):
    for path in FONT_CANDIDATES + sorted(glob.glob("/usr/share/fonts/**/*Mono*Bold*.ttf", recursive=True)):
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    return ImageFont.load_default(size=size)


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

    def save(self):
        cfg = configparser.ConfigParser()
        cfg["overlay"] = {
            "corner": self.corner,
            "margin_x": f"{self.margin_x:.2f}",
            "margin_y": f"{self.margin_y:.2f}",
            "distance": f"{self.distance:.2f}",
            "width_m": f"{self.width_m:.2f}",
            "background_alpha": str(self.background_alpha),
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
    try:
        with open(STATE_FILE) as f:
            return f.read().strip() != "0"
    except OSError:
        return True


def write_enabled(enabled):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(STATE_FILE, "w") as f:
        f.write("1\n" if enabled else "0\n")


def read_source_dir():
    """The git checkout install.sh was run from; the menu's Update button pulls into it."""
    try:
        with open(SOURCE_DIR_FILE) as f:
            return f.read().strip()
    except OSError:
        return None


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


def run_detached(command):
    """Runs a shell command in its own systemd scope so it survives this service being restarted or removed."""
    subprocess.Popen(["systemd-run", "--user", "--collect", "/bin/bash", "-c", command],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


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

    def __init__(self):
        self.font = load_font(40)

    def render(self, background_alpha, fps, frame_ms, refresh_hz, battery, charging, resolution):
        img = Image.new("RGBA", (self.WIDTH, self.HEIGHT), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        draw.rounded_rectangle((0, 0, self.WIDTH - 1, self.HEIGHT - 1), radius=22, fill=(0, 0, 0, background_alpha))

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
            draw.text((x_label, y), label, font=self.font, fill=GREY, stroke_width=2, stroke_fill=OUTLINE)
            draw.text((x_value, y), value, font=self.font, fill=color, stroke_width=2, stroke_fill=OUTLINE)
            y += line
        return img


class Menu:
    """The 'FPS Overlay' tab in the SteamVR dashboard: every option for the overlay in one place."""
    WIDTH, HEIGHT = 800, 660
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

        self._button(draw, (60, 510, 280, 580), "Reset", "reset")
        self._button(draw, (290, 510, 510, 580), "Update", "update")
        self._button(draw, (520, 510, 740, 580), "Confirm?" if confirm_uninstall else "Uninstall", "uninstall",
                     fill=self.DANGER)

        if status:
            draw.text((self.WIDTH // 2, 620), status, font=self.small_font, fill=YELLOW, anchor="mm")
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


def run():
    settings = Settings()
    if not os.path.exists(STATE_FILE):
        write_enabled(True)

    vr_system = openvr.init(openvr.VRApplication_Overlay)
    overlay = openvr.VROverlay()
    stats = Stats(vr_system, openvr.VRCompositor())
    hud = HudRenderer()
    menu = Menu()

    hud_handle = overlay.createOverlay(APP_KEY + ".hud", "FPS Overlay HUD")
    overlay.setOverlaySortOrder(hud_handle, 100)

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
    event = openvr.VREvent_t()

    def apply_placement():
        overlay.setOverlayWidthInMeters(hud_handle, settings.width_m)
        overlay.setOverlayTransformTrackedDeviceRelative(
            hud_handle, openvr.k_unTrackedDeviceIndex_Hmd, settings.head_transform())

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
                status = "Click Confirm? to uninstall"
        if action not in ("toggle", "update", "uninstall"):
            settings.save()
            apply_placement()
        redraw_menu()

    apply_placement()
    shown_confirm = False

    while True:
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

        # The state file is the single source of truth for on/off so toggle.sh and the menu stay in sync.
        try:
            mtime = os.stat(STATE_FILE).st_mtime_ns
        except OSError:
            mtime = None
        if mtime != state_mtime or enabled is None:
            state_mtime = mtime
            new_state = read_enabled()
            if new_state != enabled:
                enabled = new_state
                if enabled:
                    overlay.showOverlay(hud_handle)
                else:
                    overlay.hideOverlay(hud_handle)
                redraw_menu()
                next_update = 0.0

        # Let the uninstall confirmation lapse on its own.
        confirming = time.monotonic() < confirm_until
        if shown_confirm and not confirming:
            status = ""
            redraw_menu()
        shown_confirm = confirming

        now = time.monotonic()
        if enabled and now >= next_update:
            next_update = now + UPDATE_INTERVAL
            fps, frame_ms = stats.frame_stats()
            battery, charging = stats.battery()
            img = hud.render(settings.background_alpha, fps, frame_ms, stats.refresh_rate(),
                             battery, charging, stats.resolution())
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
