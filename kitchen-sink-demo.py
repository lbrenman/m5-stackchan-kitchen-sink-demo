# =============================================================================
# Kitchen Sink Demo - multi-app launcher for the M5Stack StackChan
# (UiFlow2 / MicroPython, m5ui + LVGL 9)
#
# Tested on: M5Stack StackChan (CoreS3-based) running UIFlow2.0 for StackChan v2.5.3
#
#   Swipe LEFT  -> next app
#   Swipe RIGHT -> previous app   (wraps around)
#
#   App 1: Controls      - LED green / red / off, sound, nod, take photo
#   App 2: Clock+Weather - time, date, current weather with drawn icons
#   App 3: Battery       - voltage, current, power, charge estimate, power graph
#   App 4: Audio         - microphone level meter / color spectrum
#   App 5: Photo Frame   - random Unsplash photos every 30 s / 1 min / 5 min
#
# To add a new app: write a class that extends App (see TEMPLATE at the bottom
# of the "APPS" section) and add it to the APPS list near the end of the file.
# =============================================================================

import os, sys, io
import M5
from M5 import *
import m5ui
import lvgl as lv
import time
import math
import array
import json
import gc
import camera
from hardware.stackchan import StackChan

# =============================================================================
# CONFIG
# =============================================================================
# --- Head / servos ---
HOME_X = 0          # pan: 0 = facing straight ahead
HOME_Y = 20         # tilt where YOUR head looks upright (use your measured value)
NOD_AMOUNT = 15     # degrees up/down from HOME_Y
NOD_MOVE_MS = 300   # time per leg of the nod
Y_MIN = 5           # safe tilt limits: never command outside 5..85
Y_MAX = 85

# --- Sound ---
VOLUME = 0.6        # 0.0 .. 1.0

# --- Photo ---
PHOTO_SHOW_MS = 5000
WARMUP_FRAMES = 3

# --- Clock / weather ---
CITY_NAME = "Boston"
LATITUDE = 42.36
LONGITUDE = -71.06
USE_24H = False
TEMP_UNIT = "fahrenheit"        # or "celsius"
WIND_UNIT = "mph"               # or "kmh", "ms", "kn"
WEATHER_REFRESH_MS = 15 * 60 * 1000
WEATHER_RETRY_MS = 60 * 1000
NTP_HOST = "pool.ntp.org"

# --- Battery ---
BATTERY_UPDATE_MS = 1000
POWER_HISTORY_POINTS = 60      # one point per update -> 60 s of history

# --- Audio monitor ---
AUDIO_RATE = 16000             # Hz
AUDIO_CHUNK = 512              # samples per frame (32 ms at 16 kHz)
MIC_GAIN_DB = 0                # add to meter readings if your mic reads low/high
METER_FLOOR_DB = -60           # left end of the level meter
# Minimum time between redraws per mode (Meter, Colors). The app also waits at
# least as long as the last redraw took, so touch/swipes always get time.
AUDIO_MIN_FRAME_MS = (0, 60)
SPECTRUM_SAMPLES = 256         # samples used for the frequency bands
SPECTRUM_LOW_HZ = 80
SPECTRUM_HIGH_HZ = 6000
SPECTRUM_RANGE = 3.5           # visible dynamic range (log10 power units, ~35 dB)
SPECTRUM_MIN_REF = 9.5         # raise if bars dance in a quiet room
LED_BRIGHTNESS = 0.5           # 0.0 .. 1.0 for the Colors mode LEDs

# --- Photo frame (Unsplash) ---
# EDIT THIS before running: your free Unsplash Access Key
# (unsplash.com/developers -> Your apps -> New Application).
# Don't commit your real key to a public repo.
UNSPLASH_ACCESS_KEY = "YOUR-UNSPLASH-ACCESS-KEY"
UNSPLASH_QUERY = ""                 # e.g. "nature" or "boston"; "" = any photo
UNSPLASH_ORIENTATION = "landscape"  # "landscape", "portrait", "squarish" or ""
UNSPLASH_CONTENT_FILTER = "high"    # "high" = strictest safe-content filter
# Photos fetched per API call (1..30). Demo keys allow 50 calls/hour; with 10
# per call, even the 30 s interval uses only 12 calls/hour.
PHOTO_BATCH = 10
# Which URL in each photo record to show, and how to size it. Unsplash image
# URLs accept resize parameters. PNG, because Unsplash's JPEGs are progressive,
# which the device can't decode.
PHOTO_URL_FIELD = ("urls", "raw")
PHOTO_SIZE_PARAMS = "w=320&h=240&fit=crop&crop=entropy&fm=png"
PHOTO_INTERVALS = (("30 s", 30 * 1000), ("1 min", 60 * 1000), ("5 min", 5 * 60 * 1000))
PHOTO_DEFAULT_INTERVAL = 1          # index into PHOTO_INTERVALS (1 = "1 min")
PHOTO_RETRY_MS = 60 * 1000          # retry delay after an error
PHOTO_RATE_LIMIT_RETRY_MS = 10 * 60 * 1000
PHOTO_REPUSH_MS = 5000              # re-copy the photo to the screen this often

# --- StackChan body ---
# At power-up (Run Always / Download) the body can be slow to appear on I2C.
BODY_INIT_TRIES = 6
BODY_INIT_WAIT_MS = 500

# --- Navigation ---
SWIPE_ANIM_MS = 250

SCREEN_W = 320
SCREEN_H = 240


# =============================================================================
# SMALL LVGL HELPERS (work around minor v8/v9 binding differences)
# =============================================================================
CIRCLE = getattr(lv, "RADIUS_CIRCLE", 0x7FFF)


def flag_off(obj, flag):
    fn = getattr(obj, "remove_flag", None) or getattr(obj, "clear_flag", None)
    if fn:
        try:
            fn(flag)
        except Exception:
            pass


def flag_on(obj, flag):
    try:
        obj.add_flag(flag)
    except Exception:
        pass


def event_code(e):
    try:
        return e.code
    except AttributeError:
        return e.get_code()


def active_indev():
    fn = getattr(lv, "indev_active", None) or getattr(lv, "indev_get_act", None)
    return fn() if fn else None


def refresh_now():
    """Force LVGL to draw right away (used before a blocking network call)."""
    try:
        lv.refr_now(None)
    except Exception:
        pass


def shape(parent, x, y, w, h, color, radius=0, opa=255):
    """A plain filled rectangle / rounded rectangle / circle. Not clickable,
    so touches and swipes pass straight through to the page."""
    o = lv.obj(parent)
    o.set_pos(int(x), int(y))
    o.set_size(int(w), int(h))
    o.set_style_radius(radius, 0)
    o.set_style_bg_color(lv.color_hex(color), 0)
    o.set_style_bg_opa(opa, 0)
    o.set_style_border_width(0, 0)
    o.set_style_pad_all(0, 0)
    o.set_style_shadow_width(0, 0)
    flag_off(o, lv.obj.FLAG.SCROLLABLE)
    flag_off(o, lv.obj.FLAG.CLICKABLE)
    return o


def circle(parent, cx, cy, d, color, opa=255):
    return shape(parent, cx - d // 2, cy - d // 2, d, d, color, CIRCLE, opa)


def label(parent, text, x, y, color, font):
    return m5ui.M5Label(
        text, x=x, y=y, text_c=color, bg_c=0x000000, bg_opa=0, font=font, parent=parent
    )


def set_page_gradient(page, top, bottom):
    try:
        page.set_style_bg_color(lv.color_hex(top), 0)
        page.set_style_bg_grad_color(lv.color_hex(bottom), 0)
        page.set_style_bg_grad_dir(lv.GRAD_DIR.VER, 0)
    except Exception:
        pass


def clamp_y(angle):
    return max(Y_MIN, min(Y_MAX, angle))


def clamp(v, lo, hi):
    return lo if v < lo else hi if v > hi else v


def container(parent, x, y, w, h):
    """Invisible, non-clickable box used to group widgets (so they can be
    shown/hidden together). Non-clickable means swipes pass through."""
    c = lv.obj(parent)
    c.set_pos(x, y)
    c.set_size(w, h)
    c.set_style_bg_opa(0, 0)
    c.set_style_border_width(0, 0)
    c.set_style_pad_all(0, 0)
    c.set_style_radius(0, 0)
    flag_off(c, lv.obj.FLAG.SCROLLABLE)
    flag_off(c, lv.obj.FLAG.CLICKABLE)
    return c


def show(obj, visible):
    if visible:
        flag_off(obj, lv.obj.FLAG.HIDDEN)
    else:
        flag_on(obj, lv.obj.FLAG.HIDDEN)


def hsv(h, s, v):
    """Hue 0-360, saturation 0-1, value 0-1 -> 0xRRGGBB."""
    h = h % 360
    c = v * s
    x = c * (1 - abs((h / 60.0) % 2 - 1))
    m = v - c
    if h < 60:
        r, g, b = c, x, 0
    elif h < 120:
        r, g, b = x, c, 0
    elif h < 180:
        r, g, b = 0, c, x
    elif h < 240:
        r, g, b = 0, x, c
    elif h < 300:
        r, g, b = x, 0, c
    else:
        r, g, b = c, 0, x
    return (int((r + m) * 255) << 16) | (int((g + m) * 255) << 8) | int((b + m) * 255)


# --- LVGL line chart helpers (names differ slightly between LVGL versions) ---
def make_chart(parent, x, y, w, h, points, color, lo, hi):
    ch = lv.chart(parent)
    ch.set_pos(x, y)
    ch.set_size(w, h)
    ch.set_type(lv.chart.TYPE.LINE)
    ch.set_point_count(points)
    ser = ch.add_series(lv.color_hex(color), lv.chart.AXIS.PRIMARY_Y)
    chart_range(ch, lo, hi)
    try:
        ch.set_div_line_count(5, 8)
    except Exception:
        pass
    # hide the point markers, keep only the line
    for name in ("set_style_width", "set_style_height"):
        try:
            getattr(ch, name)(0, lv.PART.INDICATOR)
        except Exception:
            pass
    try:
        ch.set_style_size(0, 0, lv.PART.INDICATOR)
    except Exception:
        try:
            ch.set_style_size(0, lv.PART.INDICATOR)
        except Exception:
            pass
    ch.set_style_line_width(2, lv.PART.ITEMS)
    ch.set_style_bg_color(lv.color_hex(0x07130D), 0)
    ch.set_style_bg_opa(255, 0)
    ch.set_style_border_color(lv.color_hex(0x1F4D33), 0)
    ch.set_style_border_width(1, 0)
    ch.set_style_line_color(lv.color_hex(0x16301F), 0)   # grid lines
    ch.set_style_radius(6, 0)
    ch.set_style_pad_all(4, 0)
    flag_off(ch, lv.obj.FLAG.CLICKABLE)
    flag_off(ch, lv.obj.FLAG.SCROLLABLE)
    return ch, ser


def chart_range(ch, lo, hi):
    fn = getattr(ch, "set_axis_range", None) or getattr(ch, "set_range", None)
    try:
        fn(lv.chart.AXIS.PRIMARY_Y, int(lo), int(hi))
    except Exception:
        pass


_chart_by_id = True


def chart_set_all(ch, ser, values):
    """Replace every point in a series."""
    global _chart_by_id
    if _chart_by_id:
        fn = getattr(ch, "set_series_value_by_id", None) or getattr(ch, "set_value_by_id", None)
        try:
            for i, v in enumerate(values):
                fn(ser, i, v)
            ch.refresh()
            return
        except Exception:
            _chart_by_id = False
    for v in values:                 # fallback: shifting in N values replaces all N
        ch.set_next_value(ser, v)


# =============================================================================
# APP BASE CLASS
# =============================================================================
class App:
    """Base class for every screen/mini app.

    Lifecycle (called by AppManager):
      build(page)  once at startup; create your widgets on `page`
      on_enter()   every time the app becomes visible
      tick()       every loop pass (~10 ms) while visible; keep it fast
      on_exit()    every time the user swipes away

    Long/blocking work (sleeps, servo moves, network) should be requested from
    button callbacks with self.mgr.run_later(fn) so it runs in the main loop,
    not inside an LVGL event callback.
    """

    NAME = "App"
    BG = 0x000000

    def __init__(self, mgr):
        self.mgr = mgr
        self.page = None

    @property
    def sc(self):
        return self.mgr.stackchan

    def build(self, page):
        pass

    def on_enter(self):
        pass

    def on_exit(self):
        pass

    def tick(self):
        pass


# =============================================================================
# APP 1: CONTROLS (the six-button program)
# =============================================================================
GREEN = 0x00FF00
RED = 0xFF0000
OFF = 0x000000


class ControlsApp(App):
    NAME = "Controls"
    BG = 0x000000

    BTN_W = 100
    BTN_H = 75
    COL_X = (5, 110, 215)
    ROW_Y = (50, 142)

    def build(self, page):
        label(page, "StackChan Controls", 70, 5, 0x0DC9F4, lv.font_montserrat_18)
        self.status = label(page, "Ready - tap a button", 10, 28, 0xD2E711, lv.font_montserrat_14)

        self.button("LED Green", 0, 0, 0x2E7D32, self.led_green)
        self.button("LED Red", 1, 0, 0xC62828, self.led_red)
        self.button("LED Off", 2, 0, 0x424242, self.led_off)
        self.button("Play Sound", 0, 1, 0x1565C0, self.play_sound)
        self.button("Nod", 1, 1, 0x6A1B9A, self.nod)
        self.button("Take Photo", 2, 1, 0xEF6C00, self.take_photo)

        self.camera_ok = False
        try:
            camera.init(pixformat=camera.RGB565, framesize=camera.QVGA)  # 320 x 240
            self.camera_ok = True
        except Exception as e:
            print("Camera init failed:", e)

    def button(self, text, col, row, color, action):
        btn = m5ui.M5Button(
            text=text,
            x=self.COL_X[col],
            y=self.ROW_Y[row],
            bg_c=color,
            text_c=0xFFFFFF,
            font=lv.font_montserrat_16,
            parent=self.page,
        )
        btn.set_size(self.BTN_W, self.BTN_H)
        flag_on(btn, lv.obj.FLAG.GESTURE_BUBBLE)   # let swipes that start on a button reach the page

        def handler(e):
            if event_code(e) == lv.EVENT.SHORT_CLICKED:
                self.mgr.run_later(action)

        btn.add_event_cb(handler, lv.EVENT.ALL, None)
        return btn

    def set_status(self, text):
        self.status.set_text(text)

    # --- actions ---------------------------------------------------------
    def led_green(self):
        self.sc.set_rgb_color(GREEN)
        self.set_status("LEDs: green")

    def led_red(self):
        self.sc.set_rgb_color(RED)
        self.set_status("LEDs: red")

    def led_off(self):
        self.sc.set_rgb_color(OFF)
        self.set_status("LEDs: off")

    def play_sound(self):
        self.set_status("Playing sound...")
        refresh_now()
        for freq, dur in [(523, 120), (659, 120), (784, 120), (1047, 300)]:
            Speaker.tone(freq, dur)
            time.sleep_ms(dur + 30)
        self.set_status("Sound done")

    def nod(self):
        self.set_status("Nodding...")
        refresh_now()
        up = clamp_y(HOME_Y + NOD_AMOUNT)     # higher Y tilts the head up
        down = clamp_y(HOME_Y - NOD_AMOUNT)
        for target in (up, down, HOME_Y):
            self.sc.set_servo_angle(self.sc.SERVO_ID_Y, target, NOD_MOVE_MS, 0)
            time.sleep_ms(NOD_MOVE_MS + 50)
        self.set_status("Nod done")

    def take_photo(self):
        if not self.camera_ok:
            self.set_status("Camera not available")
            return
        for _ in range(WARMUP_FRAMES):
            camera.snapshot()
        img = camera.snapshot()
        Speaker.tone(2000, 60)
        M5.Lcd.show(img, 0, 0, SCREEN_W, SCREEN_H)   # draws over the LVGL page
        time.sleep_ms(PHOTO_SHOW_MS)
        self.page.invalidate()                       # make LVGL repaint the page
        self.set_status("Photo done")


# =============================================================================
# APP 2: CLOCK + WEATHER
# =============================================================================
DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

# Background gradients (top, bottom)
DAY_GRADIENT = (0x1E5AA8, 0x0B2545)
NIGHT_GRADIENT = (0x0B1026, 0x1C2350)

TEXT_MAIN = 0xFFFFFF
TEXT_SOFT = 0xB3C7E6


def classify_weather(code):
    """WMO weather code (Open-Meteo) -> (icon kind, description)."""
    if code == 0:
        return "clear", "Clear"
    if code == 1:
        return "partly", "Mainly clear"
    if code == 2:
        return "partly", "Partly cloudy"
    if code == 3:
        return "cloudy", "Overcast"
    if code in (45, 48):
        return "fog", "Fog"
    if code in (51, 53, 55):
        return "rain", "Drizzle"
    if code in (56, 57):
        return "rain", "Freezing drizzle"
    if code == 61:
        return "rain", "Light rain"
    if code == 63:
        return "rain", "Rain"
    if code == 65:
        return "rain", "Heavy rain"
    if code in (66, 67):
        return "rain", "Freezing rain"
    if code in (71, 73, 75):
        return "snow", "Snow"
    if code == 77:
        return "snow", "Snow grains"
    if code in (80, 81, 82):
        return "rain", "Rain showers"
    if code in (85, 86):
        return "snow", "Snow showers"
    if code == 95:
        return "storm", "Thunderstorm"
    if code in (96, 99):
        return "storm", "Storm + hail"
    return "cloudy", "Unknown"


class WeatherIcon:
    """Weather icons built from LVGL circles and rounded rectangles
    inside a 96 x 96 box. No image files needed."""

    SIZE = 96

    def __init__(self, parent, x, y):
        self.box = lv.obj(parent)
        self.box.set_pos(x, y)
        self.box.set_size(self.SIZE, self.SIZE)
        self.box.set_style_bg_opa(0, 0)
        self.box.set_style_border_width(0, 0)
        self.box.set_style_pad_all(0, 0)
        flag_off(self.box, lv.obj.FLAG.SCROLLABLE)
        flag_off(self.box, lv.obj.FLAG.CLICKABLE)

    def show(self, kind, is_day):
        self.box.clean()                     # delete the old icon's shapes
        b = self.box
        if kind == "clear":
            self._sun(b, 48, 48, 56) if is_day else self._moon(b, 48, 48, 56)
        elif kind == "partly":
            if is_day:
                self._sun(b, 62, 34, 44)
            else:
                self._moon(b, 62, 34, 40)
            self._cloud(b, 0xF5F7FA, -8, 10)
        elif kind == "cloudy":
            self._cloud(b, 0x8FA3B8, 8, -8)
            self._cloud(b, 0xECEFF1, -4, 6)
        elif kind == "rain":
            self._cloud(b, 0xB0BEC5, 0, -12)
            for x, y in ((30, 70), (48, 78), (66, 70)):
                shape(b, x, y, 6, 16, 0x4FC3F7, 3)
        elif kind == "snow":
            self._cloud(b, 0xCFD8DC, 0, -12)
            for x, y in ((32, 78), (50, 86), (68, 78)):
                circle(b, x, y, 9, 0xFFFFFF)
        elif kind == "storm":
            self._cloud(b, 0x607D8B, 0, -12)
            shape(b, 50, 60, 8, 14, 0xFFEB3B, 2)   # lightning bolt, stair-stepped
            shape(b, 40, 72, 18, 6, 0xFFEB3B, 2)
            shape(b, 40, 76, 8, 16, 0xFFEB3B, 2)
            shape(b, 70, 70, 5, 12, 0x4FC3F7, 3)
        elif kind == "fog":
            self._cloud(b, 0xB0BEC5, 0, -16)
            shape(b, 12, 66, 70, 6, 0xCFD8DC, 3)
            shape(b, 22, 77, 62, 6, 0xCFD8DC, 3)
            shape(b, 12, 88, 58, 6, 0xCFD8DC, 3)

    @staticmethod
    def _sun(b, cx, cy, d):
        circle(b, cx, cy, d + 22, 0xFFB300, 50)   # soft glow
        circle(b, cx, cy, d + 10, 0xFFB300, 90)
        circle(b, cx, cy, d, 0xFFC107)
        circle(b, cx - d // 8, cy - d // 8, d * 2 // 3, 0xFFD54F)   # highlight

    @staticmethod
    def _moon(b, cx, cy, d):
        circle(b, cx, cy, d + 14, 0xCFD8DC, 40)   # glow
        circle(b, cx, cy, d, 0xECEFF1)
        circle(b, cx - d // 5, cy - d // 6, d // 4, 0xCFD8DC)   # craters
        circle(b, cx + d // 5, cy + d // 6, d // 5, 0xCFD8DC)
        circle(b, cx + d // 6, cy - d // 4, d // 8, 0xCFD8DC)

    @staticmethod
    def _cloud(b, color, dx, dy):
        circle(b, 34 + dx, 54 + dy, 34, color)
        circle(b, 56 + dx, 44 + dy, 44, color)
        circle(b, 74 + dx, 58 + dy, 28, color)
        shape(b, 18 + dx, 56 + dy, 70, 20, color, 10)


def wifi_connected():
    try:
        import network

        return network.WLAN(network.STA_IF).isconnected()
    except Exception:
        return True   # can't tell; just try the request


def http_get_json(url):
    try:
        import requests2 as rq
    except ImportError:
        try:
            import urequests as rq
        except ImportError:
            import requests as rq
    r = rq.get(url)
    try:
        return r.json()
    finally:
        r.close()


class ClockWeatherApp(App):
    NAME = "Clock"
    BG = NIGHT_GRADIENT[0]

    def build(self, page):
        set_page_gradient(page, *NIGHT_GRADIENT)

        # Top row: city (left) and update status (right)
        self.city = label(page, CITY_NAME, 10, 6, TEXT_SOFT, lv.font_montserrat_12)
        self.status = label(page, "", 230, 6, TEXT_SOFT, lv.font_montserrat_12)

        # Big time + AM/PM
        self.time_lbl = label(page, "--:--", 0, 0, TEXT_MAIN, lv.font_montserrat_48)
        self.time_lbl.align(lv.ALIGN.TOP_MID, 0 if USE_24H else -16, 10)
        self.ampm_lbl = label(page, "", 0, 0, TEXT_SOFT, lv.font_montserrat_18)

        # Date
        self.date_lbl = label(page, "", 0, 0, TEXT_SOFT, lv.font_montserrat_18)
        self.date_lbl.align(lv.ALIGN.TOP_MID, 0, 66)

        # Divider
        shape(page, 20, 96, 280, 1, 0x5C7AA3, 0, 120)

        # Weather block
        self.icon = WeatherIcon(page, 12, 104)
        self.temp_lbl = label(page, "--°", 118, 104, TEXT_MAIN, lv.font_montserrat_48)
        self.cond_lbl = label(page, "Loading weather...", 120, 158, TEXT_MAIN, lv.font_montserrat_18)
        self.hilo_lbl = label(page, "", 120, 182, TEXT_SOFT, lv.font_montserrat_14)
        self.misc_lbl = label(page, "", 120, 200, TEXT_SOFT, lv.font_montserrat_14)

        # State
        self.utc_offset = None      # seconds, from Open-Meteo (handles DST)
        self.correction = 0         # seconds, fixes an RTC that is off
        self.ntp_tried = False
        self.have_weather = False
        self.next_fetch = time.ticks_ms()
        self.last_shown = None
        self.is_day = None

    # --- lifecycle -------------------------------------------------------
    def on_enter(self):
        self.last_shown = None      # force a redraw of the clock
        if not self.have_weather:
            # small delay so the swipe animation finishes before we block on the network
            self.next_fetch = time.ticks_add(time.ticks_ms(), 600)

    def tick(self):
        if time.ticks_diff(time.ticks_ms(), self.next_fetch) >= 0:
            self.refresh_weather()
        self.update_clock()

    # --- clock -----------------------------------------------------------
    def now(self):
        if self.utc_offset is None:
            return time.localtime()
        return time.gmtime(time.time() + self.utc_offset + self.correction)

    def update_clock(self):
        t = self.now()
        key = (t[3], t[4], t[2])        # redraw only when hour/minute/day changes
        if key == self.last_shown:
            return
        self.last_shown = key

        if t[0] < 2024:                  # RTC not set yet
            self.time_lbl.set_text("--:--")
            self.ampm_lbl.set_text("")
            self.date_lbl.set_text("Waiting for time...")
            return

        h, m = t[3], t[4]
        if USE_24H:
            self.time_lbl.set_text("%02d:%02d" % (h, m))
            self.ampm_lbl.set_text("")
        else:
            self.time_lbl.set_text("%d:%02d" % (h % 12 or 12, m))
            self.ampm_lbl.set_text("AM" if h < 12 else "PM")
            self.ampm_lbl.align_to(self.time_lbl, lv.ALIGN.OUT_RIGHT_BOTTOM, 6, -8)

        self.date_lbl.set_text("%s, %s %d" % (DAYS[t[6]], MONTHS[t[1] - 1], t[2]))

    def try_ntp(self):
        self.ntp_tried = True
        try:
            import ntptime

            try:
                ntptime.host = NTP_HOST
            except Exception:
                pass
            ntptime.settime()            # sets the RTC to UTC
        except Exception as e:
            print("NTP sync failed (using firmware time):", e)

    def check_clock(self, api_time):
        """Open-Meteo reports the local observation time, e.g. '2026-09-27T14:45'
        (rounded to 15 min). If our clock disagrees by more than 20 min,
        correct it. Fixes an RTC that is unset or set to the wrong zone."""
        try:
            d, hm = api_time.split("T")
            Y, M, D = [int(x) for x in d.split("-")]
            hh, mm = [int(x) for x in hm.split(":")[:2]]
            api_epoch = time.mktime((Y, M, D, hh, mm, 0, 0, 0))
        except Exception:
            return
        ours = time.time() + self.utc_offset + self.correction
        diff = api_epoch - ours
        if abs(diff) <= 20 * 60:
            return
        if abs(diff) < 15 * 3600:
            self.correction += int(round(diff / 1800.0)) * 1800   # whole/half-hour zone error
        else:
            self.correction += diff                               # clock never set
        self.last_shown = None

    # --- weather ---------------------------------------------------------
    def weather_url(self):
        return (
            "https://api.open-meteo.com/v1/forecast"
            "?latitude=%s&longitude=%s"
            "&current=temperature_2m,relative_humidity_2m,apparent_temperature,"
            "is_day,weather_code,wind_speed_10m"
            "&daily=temperature_2m_max,temperature_2m_min"
            "&temperature_unit=%s&wind_speed_unit=%s"
            "&timezone=auto&forecast_days=1"
        ) % (LATITUDE, LONGITUDE, TEMP_UNIT, WIND_UNIT)

    def refresh_weather(self):
        if not wifi_connected():
            self.status.set_text("No Wi-Fi")
            self.next_fetch = time.ticks_add(time.ticks_ms(), WEATHER_RETRY_MS)
            return

        self.status.set_text(lv.SYMBOL.REFRESH + " Updating")
        refresh_now()

        if not self.ntp_tried:
            self.try_ntp()

        try:
            data = http_get_json(self.weather_url())
            self.apply_weather(data)
            self.next_fetch = time.ticks_add(time.ticks_ms(), WEATHER_REFRESH_MS)
        except Exception as e:
            print("Weather fetch failed:", e)
            self.status.set_text("Weather error")
            if not self.have_weather:
                self.cond_lbl.set_text("Weather unavailable")
            self.next_fetch = time.ticks_add(time.ticks_ms(), WEATHER_RETRY_MS)

    def apply_weather(self, data):
        cur = data["current"]
        daily = data.get("daily", {})
        unit = "F" if TEMP_UNIT == "fahrenheit" else "C"

        self.utc_offset = data.get("utc_offset_seconds", 0)
        self.check_clock(cur.get("time", ""))

        temp = int(round(cur["temperature_2m"]))
        feels = int(round(cur["apparent_temperature"]))
        hum = int(round(cur["relative_humidity_2m"]))
        wind = int(round(cur["wind_speed_10m"]))
        is_day = cur.get("is_day", 1) == 1
        kind, text = classify_weather(cur["weather_code"])

        self.temp_lbl.set_text("%d°%s" % (temp, unit))
        self.cond_lbl.set_text(text)
        try:
            hi = int(round(daily["temperature_2m_max"][0]))
            lo = int(round(daily["temperature_2m_min"][0]))
            self.hilo_lbl.set_text("H %d°  L %d°  Feels %d°" % (hi, lo, feels))
        except Exception:
            self.hilo_lbl.set_text("Feels %d°" % feels)
        self.misc_lbl.set_text("Humidity %d%%  •  Wind %d %s" % (hum, wind, WIND_UNIT))

        self.icon.show(kind, is_day)
        if is_day != self.is_day:
            self.is_day = is_day
            set_page_gradient(self.page, *(DAY_GRADIENT if is_day else NIGHT_GRADIENT))

        t = self.now()
        if USE_24H:
            stamp = "%02d:%02d" % (t[3], t[4])
        else:
            stamp = "%d:%02d%s" % (t[3] % 12 or 12, t[4], "a" if t[3] < 12 else "p")
        self.status.set_text(lv.SYMBOL.REFRESH + " " + stamp)
        self.have_weather = True
        self.last_shown = None


# =============================================================================
# APP 3: BATTERY & POWER
# =============================================================================
# Rough LiPo resting-voltage -> charge curve. Under load (servos moving) the
# voltage sags, so treat the percentage as an estimate.
LIPO_CURVE = (
    (3.30, 0), (3.50, 5), (3.60, 10), (3.70, 30), (3.80, 50),
    (3.90, 65), (4.00, 80), (4.10, 92), (4.20, 100),
)


def lipo_percent(v):
    if v <= LIPO_CURVE[0][0]:
        return 0
    for (v0, p0), (v1, p1) in zip(LIPO_CURVE, LIPO_CURVE[1:]):
        if v <= v1:
            return int(p0 + (p1 - p0) * (v - v0) / (v1 - v0))
    return 100


class BatteryApp(App):
    NAME = "Battery"
    BG = 0x0E1116

    def build(self, page):
        label(page, "Battery & Power", 10, 6, 0x0DC9F4, lv.font_montserrat_18)

        # Charge gauge (arc) on the left
        self.arc = lv.arc(page)
        self.arc.set_size(118, 118)
        self.arc.set_pos(8, 32)
        self.arc.set_bg_angles(135, 45)
        self.arc.set_range(0, 100)
        self.arc.set_value(0)
        try:
            self.arc.remove_style(None, lv.PART.KNOB)
        except Exception:
            pass
        flag_off(self.arc, lv.obj.FLAG.CLICKABLE)
        self.arc.set_style_arc_width(12, lv.PART.MAIN)
        self.arc.set_style_arc_width(12, lv.PART.INDICATOR)
        self.arc.set_style_arc_color(lv.color_hex(0x263040), lv.PART.MAIN)
        self.arc.set_style_arc_color(lv.color_hex(0x00E676), lv.PART.INDICATOR)

        self.pct_lbl = label(page, "--%", 0, 0, 0xFFFFFF, lv.font_montserrat_24)
        self.est_lbl = label(page, "estimated", 0, 0, 0x8899AA, lv.font_montserrat_12)
        self.pct_lbl.align_to(self.arc, lv.ALIGN.CENTER, 0, -6)
        self.est_lbl.align_to(self.arc, lv.ALIGN.CENTER, 0, 18)

        # Readings on the right
        self.v_lbl = label(page, "Voltage  --", 140, 38, 0xFFFFFF, lv.font_montserrat_18)
        self.i_lbl = label(page, "Current  --", 140, 64, 0xFFFFFF, lv.font_montserrat_18)
        self.p_lbl = label(page, "Power  --", 140, 90, 0xFFFFFF, lv.font_montserrat_18)
        self.s_lbl = label(page, "", 140, 118, 0x8899AA, lv.font_montserrat_14)

        # Power history graph along the bottom
        label(page, "Power (mW), last %d s" % (POWER_HISTORY_POINTS * BATTERY_UPDATE_MS // 1000),
              10, 152, 0x8899AA, lv.font_montserrat_12)
        self.chart, self.ser = make_chart(page, 8, 168, 304, 54, POWER_HISTORY_POINTS, 0xFFB300, 0, 1000)
        self.history = [0] * POWER_HISTORY_POINTS
        self.last_update = time.ticks_add(time.ticks_ms(), -BATTERY_UPDATE_MS)

    def on_enter(self):
        self.last_update = time.ticks_add(time.ticks_ms(), -BATTERY_UPDATE_MS)  # update right away

    def tick(self):
        if time.ticks_diff(time.ticks_ms(), self.last_update) < BATTERY_UPDATE_MS:
            return
        self.last_update = time.ticks_ms()

        v = self.sc.get_battery_voltage()
        a = self.sc.get_battery_current()
        p = self.sc.get_battery_power()

        if v:
            pct = lipo_percent(v)
            self.arc.set_value(pct)
            color = 0x00E676 if pct > 50 else 0xFFB300 if pct > 20 else 0xFF1744
            self.arc.set_style_arc_color(lv.color_hex(color), lv.PART.INDICATOR)
            self.pct_lbl.set_text("%d%%" % pct)
            self.pct_lbl.align_to(self.arc, lv.ALIGN.CENTER, 0, -6)
            self.v_lbl.set_text("Voltage  %.2f V" % v)
        else:
            self.v_lbl.set_text("Voltage  --")

        self.i_lbl.set_text("Current  %d mA" % int(a * 1000) if a is not None else "Current  --")
        self.p_lbl.set_text("Power  %d mW" % int(p * 1000) if p is not None else "Power  --")

        # CoreS3 power manager (if the firmware exposes it)
        status = ""
        try:
            status = "Charging" if Power.isCharging() else "On battery"
            status += "  •  Core %d%%" % Power.getBatteryLevel()
        except Exception:
            pass
        self.s_lbl.set_text(status)

        # Power history graph
        self.history.pop(0)
        self.history.append(int(abs(p) * 1000) if p is not None else 0)
        top = max(self.history)
        chart_range(self.chart, 0, max(100, int(top * 1.2)))
        chart_set_all(self.chart, self.ser, self.history)


# =============================================================================
# APP 4: AUDIO MONITOR (level meter / colors)
# =============================================================================
MODE_METER, MODE_COLORS = 0, 1
MODE_NAMES = ("Meter", "Colors")

NSEG = 24            # level meter segments
NBANDS = 12          # spectrum bands = number of StackChan LEDs
BAR_BOTTOM = 158     # spectrum bars (container coordinates)
BAR_MAX = 140
GLOW_CX, GLOW_CY = 160, 88


class AudioApp(App):
    """The CoreS3 microphone and speaker share one I2S bus, so the mic is only
    started while this app is on screen (on_enter) and the speaker is
    restored when you swipe away (on_exit)."""

    NAME = "Audio"
    BG = 0x05070A

    def build(self, page):
        self.mode = MODE_METER
        self.pending_mode = None

        # Mode buttons along the top
        self.mode_btns = []
        for i, name in enumerate(MODE_NAMES):
            btn = m5ui.M5Button(
                text=name, x=5 + i * 158, y=4, bg_c=0x263040, text_c=0xFFFFFF,
                font=lv.font_montserrat_16, parent=page,
            )
            btn.set_size(152, 36)
            flag_on(btn, lv.obj.FLAG.GESTURE_BUBBLE)

            def handler(e, m=i):
                # PRESSED (not SHORT_CLICKED) so a tap registers even if the
                # loop is busy and misses the release
                if event_code(e) == lv.EVENT.PRESSED:
                    self.pending_mode = m

            btn.add_event_cb(handler, lv.EVENT.ALL, None)
            self.mode_btns.append(btn)

        # One container per visual; only the active one is shown
        self.views = [container(page, 0, 44, 320, 182) for _ in MODE_NAMES]
        self.build_meter(self.views[MODE_METER])
        self.build_colors(self.views[MODE_COLORS])

        self.msg = label(page, "", 60, 120, 0xFF8A80, lv.font_montserrat_18)

        # Two sample buffers: one is recorded into while the other is drawn
        self.use_array = True
        self.bufs = [array.array("h", [0] * AUDIO_CHUNK) for _ in range(2)]
        self.cur = 0
        self.mic_ok = False
        self.frame = 0
        self.next_draw = time.ticks_ms()
        self.show_mode(MODE_METER)

    # --- lifecycle -------------------------------------------------------
    def on_enter(self):
        self.frame = 0
        try:
            Speaker.end()               # mic and speaker share the I2S bus
            Mic.begin()
            self.mic_ok = True
            self.msg.set_text("")
            self.start_record(self.bufs[self.cur])
        except Exception as e:
            print("Mic start failed:", e)
            self.mic_ok = False
            self.msg.set_text("Microphone unavailable")

    def on_exit(self):
        if self.mic_ok:
            t0 = time.ticks_ms()
            while Mic.isRecording() and time.ticks_diff(time.ticks_ms(), t0) < 200:
                time.sleep_ms(5)
            try:
                Mic.end()
            except Exception:
                pass
        self.mic_ok = False
        Speaker.begin()
        Speaker.setVolumePercentage(VOLUME)
        self.leds_off()

    def tick(self):
        if self.pending_mode is not None:
            m, self.pending_mode = self.pending_mode, None
            self.show_mode(m)
        if not self.mic_ok or Mic.isRecording():
            return

        # The buffer we queued last time is full: queue the other one, then draw.
        done = self.bufs[self.cur]
        self.cur ^= 1
        self.start_record(self.bufs[self.cur])

        # Frame pacing: skip drawing (but keep the mic running) until it's time
        now = time.ticks_ms()
        if time.ticks_diff(now, self.next_draw) < 0:
            return

        s = self.samples(done)
        db, mean = self.level(s)
        self.frame += 1
        if self.mode == MODE_METER:
            self.update_meter(db)
        else:
            self.update_colors(s, mean, db)

        # Leave at least as much idle time as this frame cost, so M5.update()
        # (touch, swipes, LVGL rendering) always gets its share of the CPU.
        end = time.ticks_ms()
        cost = time.ticks_diff(end, now)
        self.next_draw = time.ticks_add(end, max(AUDIO_MIN_FRAME_MS[self.mode], cost))

    # --- microphone ------------------------------------------------------
    def start_record(self, buf):
        if self.use_array:
            try:
                Mic.record(buf, AUDIO_RATE, False)
                return
            except TypeError:
                # firmware wants a bytearray: switch buffers and decode manually
                self.use_array = False
                self.bufs = [bytearray(2 * AUDIO_CHUNK) for _ in range(2)]
                buf = self.bufs[self.cur]
        Mic.record(buf, AUDIO_RATE, False)

    def samples(self, buf):
        if self.use_array:
            return buf
        out = [0] * AUDIO_CHUNK
        for i in range(AUDIO_CHUNK):
            v = buf[2 * i] | (buf[2 * i + 1] << 8)
            out[i] = v - 65536 if v > 32767 else v
        return out

    @staticmethod
    def level(s):
        """Returns (level in dBFS, DC offset)."""
        n = len(s)
        mean = sum(s) // n
        acc = 0
        for v in s:
            d = v - mean
            acc += d * d
        rms = math.sqrt(acc / n)
        db = 20 * math.log(rms / 32768.0) / 2.302585 if rms > 0 else -120
        return clamp(db + MIC_GAIN_DB, METER_FLOOR_DB, 0), mean

    # --- mode switching --------------------------------------------------
    def show_mode(self, m):
        if self.mode == MODE_COLORS and m != MODE_COLORS:
            self.leds_off()
        self.mode = m
        for i, v in enumerate(self.views):
            show(v, i == m)
        for i, b in enumerate(self.mode_btns):
            b.set_style_bg_color(lv.color_hex(0x1E88E5 if i == m else 0x263040), lv.PART.MAIN)
        self.peak_db = METER_FLOOR_DB
        self.max_db = METER_FLOOR_DB
        self.next_draw = time.ticks_ms()

    def leds_off(self):
        try:
            self.sc.set_rgb_color(0x000000)
        except Exception:
            pass

    # --- 1. level meter --------------------------------------------------
    def build_meter(self, c):
        self.db_lbl = label(c, "-- dB", 0, 0, 0xFFFFFF, lv.font_montserrat_40)
        self.db_lbl.align(lv.ALIGN.TOP_MID, 0, 6)

        seg_w, gap = 10, 3
        total = NSEG * seg_w + (NSEG - 1) * gap
        x0 = (320 - total) // 2
        self.segs = []
        self.seg_opa = []
        for i in range(NSEG):
            frac = (i + 1) / NSEG
            color = 0x00E676 if frac <= 0.6 else 0xFFEA00 if frac <= 0.85 else 0xFF1744
            self.segs.append(shape(c, x0 + i * (seg_w + gap), 62, seg_w, 48, color, 2, 40))
            self.seg_opa.append(40)

        for i in range(5):              # scale labels under the meter
            db = METER_FLOOR_DB + i * (-METER_FLOOR_DB) // 4
            x = x0 + (total * i) // 4 - (6 if i < 4 else 4)
            label(c, "%d" % db, max(0, x), 116, 0x8899AA, lv.font_montserrat_12)

        self.meter_stats = label(c, "", 0, 0, 0xB3C7E6, lv.font_montserrat_14)
        self.meter_stats.align(lv.ALIGN.TOP_MID, 0, 146)
        self.peak_db = METER_FLOOR_DB
        self.max_db = METER_FLOOR_DB

    def update_meter(self, db):
        self.peak_db = max(db, self.peak_db - 0.5)     # peak hold that slowly falls
        self.max_db = max(self.max_db, db)
        self.db_lbl.set_text("%d dB" % int(db))

        span = -METER_FLOOR_DB
        lit = int((db - METER_FLOOR_DB) / span * NSEG)
        pk = int((self.peak_db - METER_FLOOR_DB) / span * NSEG) - 1
        for i in range(NSEG):
            want = 255 if (i < lit or i == pk) else 40
            if want != self.seg_opa[i]:
                self.segs[i].set_style_bg_opa(want, 0)
                self.seg_opa[i] = want

        if self.frame % 4 == 0:
            self.meter_stats.set_text(
                "Peak hold %d dB   •   Max %d dB" % (int(self.peak_db), int(self.max_db))
            )

    # --- 2. colors (spectrum bars + glow + StackChan LEDs) ---------------
    def build_colors(self, c):
        self.glow = circle(c, GLOW_CX, GLOW_CY, 40, 0xFF00FF, 60)

        bar_w, gap = 20, 5
        x0 = (320 - (NBANDS * bar_w + (NBANDS - 1) * gap)) // 2
        self.bars, self.caps, self.band_hue = [], [], []
        for k in range(NBANDS):
            hue = int(k * 300 / (NBANDS - 1))          # red -> violet across the bands
            x = x0 + k * (bar_w + gap)
            self.band_hue.append(hue)
            self.bars.append(shape(c, x, BAR_BOTTOM - 2, bar_w, 2, hsv(hue, 1, 1), 4))
            self.caps.append(shape(c, x, BAR_BOTTOM - 6, bar_w, 3, 0xFFFFFF, 1))
        label(c, "%d Hz" % SPECTRUM_LOW_HZ, x0, BAR_BOTTOM + 4, 0x8899AA, lv.font_montserrat_12)
        label(c, "%d kHz" % (SPECTRUM_HIGH_HZ // 1000), 262, BAR_BOTTOM + 4, 0x8899AA, lv.font_montserrat_12)

        # Goertzel coefficients for log-spaced band centres
        ratio = SPECTRUM_HIGH_HZ / SPECTRUM_LOW_HZ
        self.coeffs = []
        for k in range(NBANDS):
            f = SPECTRUM_LOW_HZ * ratio ** (k / (NBANDS - 1))
            self.coeffs.append(2 * math.cos(2 * math.pi * f / AUDIO_RATE))

        self.bar_h = [0] * NBANDS
        self.cap_y = [BAR_BOTTOM - 6] * NBANDS
        self.levels = [0.0] * NBANDS
        self.spec_ref = SPECTRUM_MIN_REF
        self.hue_t = 0

    def band_powers(self, s, mean):
        """log10 power of each band (Goertzel algorithm - cheaper than an FFT
        when you only need a handful of frequencies)."""
        x = [s[i] - mean for i in range(SPECTRUM_SAMPLES)]
        out = []
        for coeff in self.coeffs:
            s1 = 0.0
            s2 = 0.0
            for v in x:
                s0 = v + coeff * s1 - s2
                s2 = s1
                s1 = s0
            p = s1 * s1 + s2 * s2 - coeff * s1 * s2
            out.append(math.log(p + 1.0) / 2.302585)
        return out

    def update_colors(self, s, mean, db):
        logs = self.band_powers(s, mean)

        # auto-gain: follow the loudest band, fall back slowly, never below the minimum
        self.spec_ref = max(max(logs), self.spec_ref - 0.02, SPECTRUM_MIN_REF)
        floor = self.spec_ref - SPECTRUM_RANGE

        for k in range(NBANDS):
            lvl = clamp((logs[k] - floor) / SPECTRUM_RANGE, 0.0, 1.0)
            self.levels[k] = lvl
            h = max(int(lvl * BAR_MAX), int(self.bar_h[k] * 0.8), 2)   # smooth fall-off
            if h != self.bar_h[k]:
                self.bar_h[k] = h
                self.bars[k].set_height(h)
                self.bars[k].set_y(BAR_BOTTOM - h)
            cy = min(BAR_BOTTOM - h - 6, self.cap_y[k] + 2)              # caps fall slowly
            if cy != self.cap_y[k]:
                self.cap_y[k] = cy
                self.caps[k].set_y(cy)

        # pulsing glow behind the bars, size = loudness, colour cycles
        loud = (db - METER_FLOOR_DB) / -METER_FLOOR_DB
        self.hue_t = (self.hue_t + 4) % 360
        d = 30 + int(loud * 170)
        self.glow.set_size(d, d)
        self.glow.set_pos(GLOW_CX - d // 2, GLOW_CY - d // 2)
        self.glow.set_style_bg_color(lv.color_hex(hsv(self.hue_t, 0.8, 1)), 0)
        self.glow.set_style_bg_opa(40 + int(loud * 90), 0)

        # StackChan's 12 LEDs: one per band, every other frame
        if self.frame % 2 == 0:
            for k in range(NBANDS):
                color = hsv(self.band_hue[k], 1, self.levels[k] * LED_BRIGHTNESS)
                self.sc.set_rgb_color(k // 6, k % 6, color)


# =============================================================================
# APP 5: PHOTO FRAME (random Unsplash photos on a timer)
# =============================================================================
# Why this app draws with M5GFX (M5.Lcd) instead of LVGL:
# On this firmware LVGL has no image cache, so it decodes an image again on
# EVERY redraw: a 320x240 PNG took ~54 s per redraw. Unsplash's JPEGs are
# progressive, which LVGL can't decode at all. M5GFX decodes the same PNG in
# ~250 ms. So the photo is decoded ONCE into an off-screen canvas (PSRAM),
# the buttons/credit/page dots are drawn onto that canvas too, and the canvas
# is copied to the screen with push(). Nothing LVGL draws is visible on this
# page. Taps come from LVGL press/release events on the page (with their
# coordinates) and are checked against the drawn buttons. Swipes still go
# through LVGL's page gesture, like every other app.

class PhotoError(Exception):
    pass


class RateLimited(PhotoError):
    pass


def http_get(url, headers=None):
    """GET a URL. Returns (status, body bytes). Unlike http_get_json, this
    keeps the status code so 401 / 403 can be reported clearly."""
    try:
        import requests2 as rq
    except ImportError:
        try:
            import urequests as rq
        except ImportError:
            import requests as rq
    r = rq.get(url, headers=headers or {})
    try:
        status = getattr(r, "status_code", 200)
        body = r.content
    finally:
        r.close()
    return status, body


def url_quote(s):
    """Percent-encode a query value (MicroPython has no urllib.parse)."""
    out = []
    for ch in s:
        if ("a" <= ch <= "z") or ("A" <= ch <= "Z") or ("0" <= ch <= "9") or ch in "-_.~":
            out.append(ch)
        else:
            out.append("".join("%%%02X" % b for b in ch.encode()))
    return "".join(out)


def dig(d, path):
    """dig(photo, ("user", "name")) -> photo["user"]["name"], or None."""
    for k in path:
        if not isinstance(d, dict):
            return None
        d = d.get(k)
    return d


def touch_point():
    """Current touch position as (x, y), from LVGL's input device."""
    indev = active_indev()
    if indev is not None:
        try:
            p = lv.point_t()
            indev.get_point(p)
            return (p.x, p.y)
        except Exception as e:
            print("indev.get_point failed:", e)
    try:
        return (M5.Touch.getX(), M5.Touch.getY())
    except Exception:
        return None


class PhotoFrameApp(App):
    """Shows random Unsplash photos full screen. Three buttons pick how
    often the photo changes. Photos are fetched PHOTO_BATCH at a time (one
    API call) and shown one by one, which keeps demo keys under their
    50 calls/hour limit."""

    NAME = "Photos"
    BG = 0x000000

    BTN_W = 56
    BTN_H = 28
    BTN_Y = 4
    BTN_ON = 0x1E88E5
    BTN_OFF = 0x263040
    TAP_SLOP = 15           # px a finger may move and still count as a tap

    def build(self, page):
        self.canvas = None
        try:
            self.canvas = M5.Lcd.newCanvas(SCREEN_W, SCREEN_H, 16, True)   # PSRAM
        except Exception as e:
            print("Photo canvas failed:", e)
        self.font_s = getattr(M5.Lcd.FONTS, "Montserrat12", None)
        self.font_m = getattr(M5.Lcd.FONTS, "Montserrat14", None)
        self.font_l = getattr(M5.Lcd.FONTS, "Montserrat16", None)

        n = len(PHOTO_INTERVALS)
        self.btn_rects = [
            (SCREEN_W - (n - i) * (self.BTN_W + 4), self.BTN_Y, self.BTN_W, self.BTN_H)
            for i in range(n)
        ]

        self.interval_i = PHOTO_DEFAULT_INTERVAL
        self.png = None                 # bytes of the photo on screen
        self.credit = ""
        self.message = ""               # big centred text when there's no photo
        self.status = ""                # small badge (errors) over the photo
        self.queue = []                 # (image url, photographer) not shown yet
        self.last_change = None
        self.retrying = False
        self.visible = False
        self.next_change = time.ticks_ms()
        self.repaint_at = None
        self.last_push = time.ticks_ms()
        self.press = None               # (x, y) where the current touch began
        self.press_moved = False
        self.pending_tap = None         # (x, y) of a tap, handled in tick()
        page.add_event_cb(self.on_page_event, lv.EVENT.ALL, None)

        remove_old_photo_files()
        if self.canvas is None:
            self.message = "Not enough memory for the photo"
        elif self.placeholder_key():
            self.message = "Set UNSPLASH_ACCESS_KEY in CONFIG"
        else:
            self.message = "Loading photo..."
        self.compose()

    # --- lifecycle -------------------------------------------------------
    def on_enter(self):
        self.visible = True
        now = time.ticks_ms()
        # LVGL draws this (empty) page during the slide animation; copy the
        # photo over it once the animation has finished.
        self.repaint_at = time.ticks_add(now, SWIPE_ANIM_MS + 150)
        if self.png is None or time.ticks_diff(now, self.next_change) >= 0:
            self.next_change = time.ticks_add(now, SWIPE_ANIM_MS + 400)

    def on_exit(self):
        self.visible = False
        self.repaint_at = None
        self.press = None
        self.pending_tap = None

    def tick(self):
        now = time.ticks_ms()
        if self.repaint_at is not None and time.ticks_diff(now, self.repaint_at) >= 0:
            self.repaint_at = None
            self.repaint()
        elif time.ticks_diff(now, self.last_push) >= PHOTO_REPUSH_MS:
            self.repaint()              # cheap insurance if LVGL drew over us
        if self.pending_tap is not None:
            x, y = self.pending_tap
            self.pending_tap = None
            print("Photos: tap at", x, y)
            self.on_tap(x, y)
        if self.repaint_at is None and time.ticks_diff(now, self.next_change) >= 0:
            self.change_photo()

    # --- drawing ---------------------------------------------------------
    def repaint(self):
        """Copy the composed canvas to the screen (~30 ms)."""
        self.last_push = time.ticks_ms()
        if not self.visible or self.canvas is None:
            return
        try:
            lv.refr_now(None)           # let LVGL finish anything pending first
        except Exception:
            pass
        self.canvas.push(0, 0)

    def text(self, s, x, y, fg, bg, font):
        c = self.canvas
        if font is not None:
            try:
                c.setFont(font)
            except Exception:
                pass
        c.setTextColor(fg, bg)
        c.drawString(s, int(x), int(y))

    def text_w(self, s, font):
        try:
            if font is not None:
                self.canvas.setFont(font)
            return self.canvas.textWidth(s)
        except Exception:
            return len(s) * 8           # rough fallback

    def badge(self, s, x, y, font):
        """White text on a dark rounded box, readable over any photo."""
        w = self.text_w(s, font) + 10
        self.canvas.fillRoundRect(int(x), int(y), int(w), 20, 4, 0x000000)
        self.text(s, x + 5, y + 3, 0xFFFFFF, 0x000000, font)

    def compose(self):
        """Build the whole frame on the canvas: photo, buttons, credit,
        status and page dots. Decoding the PNG takes ~250 ms."""
        c = self.canvas
        if c is None:
            return
        drawn = False
        if self.png is not None:
            try:
                c.drawPng(self.png, 0, 0)
                drawn = True
            except Exception as e:
                print("drawPng failed:", e)
                self.status = "Can't draw this photo"
        if not drawn:
            c.fillRect(0, 0, SCREEN_W, SCREEN_H, 0x000000)
            if self.message:
                w = self.text_w(self.message, self.font_l)
                self.text(self.message, (SCREEN_W - w) // 2, 110, 0xB3C7E6, 0x000000, self.font_l)

        for i, (x, y, w, h) in enumerate(self.btn_rects):
            color = self.BTN_ON if i == self.interval_i else self.BTN_OFF
            c.fillRoundRect(x, y, w, h, 6, color)
            name = PHOTO_INTERVALS[i][0]
            tw = self.text_w(name, self.font_m)
            self.text(name, x + (w - tw) // 2, y + 7, 0xFFFFFF, color, self.font_m)

        if drawn and self.credit:
            self.badge(self.credit, 4, 212, self.font_s)
        if self.status:
            self.badge(self.status, 4, 38, self.font_m)
        self.draw_dots()

    def draw_dots(self):
        """Same page dots the manager draws with LVGL (they're hidden
        under the canvas on this page)."""
        try:
            idx = self.mgr.apps.index(self)
            count = len(self.mgr.apps)
        except Exception:
            return
        d, gap = 6, 8
        x = (SCREEN_W - (count * d + (count - 1) * gap)) // 2
        for i in range(count):
            color = 0xFFFFFF if i == idx else 0x6A6A6A
            self.canvas.fillCircle(x + d // 2, 229 + d // 2, d // 2, color)
            x += d + gap

    def show_busy(self):
        """Draw a 'Loading' badge on the current frame without re-decoding."""
        if self.canvas is None or self.png is None:
            return
        self.badge("Loading...", 4, 38, self.font_m)
        self.repaint()

    # --- touch --------------------------------------------------------
    # LVGL already reads the touchscreen (M5.Touch didn't see taps while m5ui
    # was running), so use the page's own press/release events. This runs
    # inside an LVGL callback: only record the tap; tick() acts on it.
    def on_page_event(self, e):
        code = event_code(e)
        if code == lv.EVENT.PRESSED:
            self.press = touch_point()
            self.press_moved = False
        elif self.press is None:
            return
        elif code == lv.EVENT.PRESSING:
            pt = touch_point()
            if pt and (abs(pt[0] - self.press[0]) > self.TAP_SLOP
                       or abs(pt[1] - self.press[1]) > self.TAP_SLOP):
                self.press_moved = True            # a swipe, not a tap
        elif code == lv.EVENT.GESTURE:
            self.press_moved = True
        elif code in (lv.EVENT.RELEASED, lv.EVENT.PRESS_LOST):
            if code == lv.EVENT.RELEASED and not self.press_moved:
                self.pending_tap = self.press
            self.press = None

    def on_tap(self, x, y):
        s = 6                                      # a little extra around each button
        for i, (bx, by, bw, bh) in enumerate(self.btn_rects):
            if bx - s <= x < bx + bw + s and by - s <= y < by + bh + s:
                if i != self.interval_i:
                    self.set_interval(i)
                return

    def set_interval(self, i):
        self.interval_i = i
        self.compose()
        self.repaint()
        if self.last_change is None or self.retrying:
            return                      # keep the pending first load / retry
        # The new interval counts from when the current photo appeared
        now = time.ticks_ms()
        target = time.ticks_add(self.last_change, PHOTO_INTERVALS[i][1])
        if time.ticks_diff(target, now) < 1000:
            target = time.ticks_add(now, 1000)
        self.next_change = target

    # --- photos ----------------------------------------------------------
    @staticmethod
    def placeholder_key():
        return "YOUR-" in UNSPLASH_ACCESS_KEY or not UNSPLASH_ACCESS_KEY

    def change_photo(self):
        if self.canvas is None or self.placeholder_key():
            self.next_change = time.ticks_add(time.ticks_ms(), PHOTO_RETRY_MS)
            return
        if not wifi_connected():
            self.fail("No Wi-Fi", PHOTO_RETRY_MS)
            return

        self.show_busy()
        try:
            if not self.queue:
                self.queue = self.fetch_batch()
            if not self.queue:
                raise PhotoError("Unsplash returned no photos")
            url, who = self.queue.pop(0)
            png = self.download(url)    # keep the old photo until this works
            self.png = None
            gc.collect()
            self.png = png
            self.credit = "%s / Unsplash" % who[:22]
            self.message = ""
            self.status = ""
            self.retrying = False
            self.last_change = time.ticks_ms()
            self.next_change = time.ticks_add(self.last_change, PHOTO_INTERVALS[self.interval_i][1])
        except RateLimited as e:
            print("Unsplash:", e)
            self.fail("Rate limit reached, waiting", PHOTO_RATE_LIMIT_RETRY_MS)
            return
        except Exception as e:
            try:
                sys.print_exception(e)
            except Exception:
                print("Photo failed:", e)
            self.fail("Error: %s" % (str(e) or type(e).__name__)[:30], PHOTO_RETRY_MS)
            return
        t0 = time.ticks_ms()
        self.compose()
        self.repaint()
        print("Photo shown (%d bytes, compose+push %d ms)"
              % (len(self.png), time.ticks_diff(time.ticks_ms(), t0)))
        gc.collect()

    def fail(self, text, retry_ms):
        self.retrying = True
        if self.png is None:
            self.message = text
            self.status = ""
        else:
            self.status = text          # keep showing the last photo
        self.compose()
        self.repaint()
        self.next_change = time.ticks_add(time.ticks_ms(), retry_ms)

    def api_url(self):
        url = "https://api.unsplash.com/photos/random?count=%d&content_filter=%s" % (
            clamp(PHOTO_BATCH, 1, 30), UNSPLASH_CONTENT_FILTER)
        if UNSPLASH_ORIENTATION:
            url += "&orientation=" + UNSPLASH_ORIENTATION
        if UNSPLASH_QUERY:
            url += "&query=" + url_quote(UNSPLASH_QUERY)
        return url

    def fetch_batch(self):
        """One API call -> list of (sized image url, photographer name)."""
        status, body = http_get(self.api_url(), {
            "Authorization": "Client-ID " + UNSPLASH_ACCESS_KEY,
            "Accept-Version": "v1",
        })
        if status == 401:
            raise PhotoError("key rejected (401)")
        if status == 403 and b"rate limit" in body.lower():
            raise RateLimited(body[:80])
        if status != 200:
            raise PhotoError("Unsplash HTTP %d" % status)
        data = json.loads(body.decode())
        body = None
        photos = data if isinstance(data, list) else [data]   # count=N -> list
        out = []
        for p in photos:
            raw = dig(p, PHOTO_URL_FIELD)
            if not isinstance(raw, str):
                continue
            url = raw
            if PHOTO_SIZE_PARAMS:
                url += ("&" if "?" in raw else "?") + PHOTO_SIZE_PARAMS
            out.append((url, dig(p, ("user", "name")) or "Unknown"))
        print("Unsplash: got %d photos" % len(out))
        return out

    @staticmethod
    def download(url):
        status, body = http_get(url)
        if status != 200:
            raise PhotoError("image HTTP %d" % status)
        if body[:4] != b"\x89PNG":
            raise PhotoError("not a PNG")
        return body


def remove_old_photo_files():
    """Earlier versions of this app (and the photo tests) saved photos in
    /flash/res/img. This version keeps them in memory, so tidy those up."""
    d = "/flash/res/img"
    try:
        for name in os.listdir(d):
            if name.startswith("unsplash_") and (name.endswith(".jpg") or name.endswith(".png")):
                os.remove(d + "/" + name)
                print("Removed old", name)
    except OSError:
        pass


# =============================================================================
# TEMPLATE for your next app (copy, rename, add to APPS)
# =============================================================================
# class MyApp(App):
#     NAME = "My App"
#     BG = 0x101820
#
#     def build(self, page):
#         self.lbl = label(page, "Hello!", 110, 100, 0xFFFFFF, lv.font_montserrat_24)
#
#     def on_enter(self):
#         self.sc.set_rgb_color(0x0000FF)
#
#     def on_exit(self):
#         self.sc.set_rgb_color(0x000000)
#
#     def tick(self):
#         pass


# =============================================================================
# APP MANAGER (swipe navigation, page dots, shared hardware, main loop)
# =============================================================================
class NoBody:
    """Stand-in for StackChan when the robot body isn't found: every
    method call does nothing and returns None, so apps keep running
    (LEDs/servos do nothing, battery readings show --)."""

    SERVO_ID_X = 0
    SERVO_ID_Y = 1

    def __getattr__(self, name):
        return lambda *args, **kwargs: None


class AppManager:
    def __init__(self, app_classes):
        self.apps = [cls(self) for cls in app_classes]
        self.index = 0
        self.pending = None     # one deferred action (from a button tap)
        self.nav = 0            # +1 next, -1 previous (from a swipe)
        self.stackchan = None

    # --- public helpers for apps ----------------------------------------
    def run_later(self, fn):
        self.pending = fn

    # --- setup -----------------------------------------------------------
    def start(self):
        M5.begin()
        Widgets.setRotation(1)          # landscape 320 x 240
        m5ui.init()

        self.init_hardware()

        n = len(self.apps)
        for i, app in enumerate(self.apps):
            app.page = m5ui.M5Page(bg_c=app.BG)
            app.build(app.page)
            self.prepare_page(app.page, i, n)

        self.apps[0].page.screen_load()
        self.apps[0].on_enter()

    def init_hardware(self):
        self.stackchan = None
        self.has_body = False
        for attempt in range(1, BODY_INIT_TRIES + 1):
            try:
                self.stackchan = StackChan(i2c=1, uart=1)
                self.has_body = True
                break
            except Exception as e:
                print("StackChan body not found (try %d/%d): %s" % (attempt, BODY_INIT_TRIES, e))
                time.sleep_ms(BODY_INIT_WAIT_MS)
        Speaker.begin()
        Speaker.setVolumePercentage(VOLUME)
        if not self.has_body:
            print("Continuing WITHOUT the StackChan body (no LEDs, servos or battery data)")
            self.stackchan = NoBody()
            return
        sc = self.stackchan
        sc.set_rgb_color(0x000000)
        sc.set_servo_power(enable=True)
        sc.set_servo_torque(sc.SERVO_ID_X, enable=True)
        sc.set_servo_torque(sc.SERVO_ID_Y, enable=True)
        sc.set_servo_angle(sc.SERVO_ID_X, HOME_X, 1000, 0)
        sc.set_servo_angle(sc.SERVO_ID_Y, clamp_y(HOME_Y), 1000, 0)
        time.sleep_ms(1000)

    def prepare_page(self, page, index, count):
        try:
            page.set_flag(lv.obj.FLAG.SCROLLABLE, False)   # scrolling would swallow swipes
        except Exception:
            flag_off(page, lv.obj.FLAG.SCROLLABLE)
        page.add_event_cb(self.on_gesture, lv.EVENT.GESTURE, None)
        self.add_page_dots(page, index, count)

    @staticmethod
    def add_page_dots(page, index, count):
        d, gap = 6, 8
        total = count * d + (count - 1) * gap
        x = (SCREEN_W - total) // 2
        for i in range(count):
            if i == index:
                shape(page, x, 229, d, d, 0xFFFFFF, CIRCLE)
            else:
                shape(page, x, 229, d, d, 0xFFFFFF, CIRCLE, 90)
            x += d + gap

    # --- navigation ------------------------------------------------------
    def on_gesture(self, e):
        indev = active_indev()
        if indev is None:
            return
        d = indev.get_gesture_dir()
        if d == lv.DIR.LEFT:
            self.nav = 1
        elif d == lv.DIR.RIGHT:
            self.nav = -1

    def switch(self, step):
        old = self.apps[self.index]
        old.on_exit()
        self.index = (self.index + step) % len(self.apps)
        new = self.apps[self.index]
        self.load_page(new.page, "MOVE_LEFT" if step > 0 else "MOVE_RIGHT")
        new.on_enter()

    @staticmethod
    def load_page(page, anim_name):
        load_fn = getattr(lv, "screen_load_anim", None) or getattr(lv, "scr_load_anim", None)
        anims = getattr(lv, "SCR_LOAD_ANIM", None) or getattr(lv, "SCREEN_LOAD_ANIM", None)
        try:
            load_fn(page, getattr(anims, anim_name), SWIPE_ANIM_MS, 0, False)
        except Exception:
            page.screen_load()          # no animation available; just switch

    # --- main loop -------------------------------------------------------
    def loop_once(self):
        M5.update()                     # touch + LVGL processing
        if self.pending:
            fn, self.pending = self.pending, None
            fn()
        if self.nav:
            step, self.nav = self.nav, 0
            self.switch(step)
        self.apps[self.index].tick()
        time.sleep_ms(10)


# =============================================================================
# APPS LIST - order here = order when swiping left
# =============================================================================
APPS = [
    ControlsApp,
    ClockWeatherApp,
    BatteryApp,
    AudioApp,
    PhotoFrameApp,
    # MyApp,
]

manager = None


def setup():
    global manager
    manager = AppManager(APPS)
    manager.start()


def loop():
    manager.loop_once()


if __name__ == "__main__":
    try:
        setup()
        while True:
            loop()
    except (Exception, KeyboardInterrupt) as e:
        try:
            m5ui.deinit()
            from utility import print_error_msg

            print_error_msg(e)
        except ImportError:
            print("please update to latest firmware")
