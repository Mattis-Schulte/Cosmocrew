#!/usr/bin/env python3
import os
import math
import signal
import json
import logging
from time import time, sleep, strftime, localtime
from threading import Thread, Event, Lock

from flask import Flask, render_template_string, Response, send_from_directory, url_for, jsonify, stream_with_context, request, redirect
from flask_socketio import SocketIO, emit

# ---------- Optional imports ----------
def optional_import(module, attr=None):
    try:
        mod = __import__(module, fromlist=[attr] if attr else [])
        return getattr(mod, attr) if attr else mod
    except Exception:
        return None

requests = optional_import("requests")
cv2 = optional_import("cv2")
reset_mcu = optional_import("robot_hat.utils", "reset_mcu")
Picarx = optional_import("picarx", "Picarx")
Vilib = optional_import("vilib", "Vilib")
Music = optional_import("robot_hat", "Music")
ADC = optional_import("robot_hat", "ADC")

# ---------- Config ----------
DIR_MIN, DIR_MAX = -30, 30
CAM_PAN_MIN, CAM_PAN_MAX = -90, 90
CAM_TILT_MIN, CAM_TILT_MAX = -35, 65

BATTERY_ADC_PIN = "A4"
VBAT_MIN, VBAT_MAX = 5.4, 8.0
BATTERY_POLL_SEC = 15

OBSTACLE_THRESHOLD_CM = 8.0     # block forward below this
OBSTACLE_CLEAR_CM = 9.5         # clear block above this (hysteresis)
OBSTACLE_POLL_SEC = 0.12

LINEFOLLOW_BASE_SPEED = 42

# Photo folder
try:
    HOME = os.path.expanduser("~" + os.getlogin())
except Exception:
    HOME = os.path.expanduser("~")
PHOTO_FOLDER = os.path.join(HOME, "pictures")
MAX_PHOTOS = 10
os.makedirs(PHOTO_FOLDER, exist_ok=True)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
RECORDING_FILE = os.path.join(BASE_DIR, "recording.json")
VILIB_MJPG_URL = os.environ.get("VILIB_MJPG_URL", "http://127.0.0.1:9000/mjpg")
TLS_CERT = os.environ.get("TLS_CERT", os.path.join(BASE_DIR, "server.crt"))
TLS_KEY  = os.environ.get("TLS_KEY",  os.path.join(BASE_DIR, "server.key"))

# Logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("picarx-server")

# ---------- Global state ----------
state = {"volume": 100, "pan": 0, "tilt": 0, "ramp_rate": 8, "throttle": 0, "steer": 0}
battery_last = {"voltage": None, "percent": None, "ts": 0.0}

auto_state = {"line_follow_enabled": False, "crash_avoid_enabled": False}
obstacle_state = {"blocked_forward": False, "distance_cm": None, "ts": 0.0}

connected_clients = set()
_last_broadcast_input = {"throttle": None, "steer": None, "pan": None, "tilt": None}
last_controller_sid = None
redirect_server = None

def clamp(x, a, b):
    try:
        return max(a, min(b, int(x)))
    except Exception:
        return a

# ---------- Helpers ----------
def safe_call(obj, method, *args, log_label=None, default=None):
    if not obj:
        return default
    try:
        return getattr(obj, method)(*args)
    except Exception as e:
        if log_label:
            log.warning("%s failed: %s", log_label, e)
        return default

def safe_set(label, fn, value):
    log.info("Setting %s: %s", label, value)
    if fn:
        try:
            fn(int(value))
        except Exception as e:
            log.warning("%s failed: %s", label, e)

def broadcast_input(payload, force=False):
    global _last_broadcast_input
    try:
        changed = force
        for k in ("throttle", "steer", "pan", "tilt"):
            if k in payload:
                v = int(payload[k])
                if _last_broadcast_input.get(k) != v:
                    changed = True
                    break
        if not changed:
            return
        for k in ("throttle", "steer", "pan", "tilt"):
            if k in payload:
                _last_broadcast_input[k] = int(payload[k])
        msg = {k: int(payload[k]) for k in ("throttle", "steer", "pan", "tilt") if k in payload}
        if "_origin" in payload:
            msg["_origin"] = payload.get("_origin")
        socketio.emit("input", msg)
    except Exception:
        pass

def stop_motors_broadcast(reason=None, origin="server", record=False):
    try:
        mot.stop()
    except Exception:
        pass
    state.update({"throttle": 0, "steer": 0, "speed": 0})
    broadcast_input({"throttle": 0, "steer": 0, "_origin": origin}, force=True)
    if record:
        try:
            recorder.record_event("drive", {"throttle": 0, "steer": 0})
        except Exception:
            pass
    if reason:
        log.info("Motors stopped (broadcast): %s", reason)

# ---------- Music state ----------
music_state = {"playing": False, "song": None, "bpm": None, "since": 0.0}

def emit_music_state():
    socketio.emit("music_state", {
        "playing": bool(music_state["playing"]),
        "song": music_state.get("song"),
        "bpm": music_state.get("bpm"),
    })

def stop_music_and_emit():
    try:
        music_control("stop")
    except Exception:
        pass
    music_state.update({"playing": False, "song": None, "bpm": None, "since": 0.0})
    emit_music_state()

# ---------- Automation emitters ----------
def emit_auto_state():
    socketio.emit("auto_state", {
        "line_follow_enabled": bool(auto_state.get("line_follow_enabled")),
        "crash_avoid_enabled": bool(auto_state.get("crash_avoid_enabled")),
    })

def emit_obstacle_state():
    socketio.emit("obstacle_state", {
        "blocked_forward": bool(obstacle_state.get("blocked_forward")),
        "distance_cm": obstacle_state.get("distance_cm"),
    })

# ---------- Battery helpers ----------
adc_batt = None
_SAMPLES, _TRIM, _ALPHA = 18, 3, 0.3
_v_filt = None

def get_battery_voltage():
    if not adc_batt:
        return None
    try:
        vals = []
        for _ in range(_SAMPLES):
            raw = adc_batt.read()
            if raw is None:
                continue
            v = float(raw) * 3.3 / 4095.0 * 3.0
            if 0.0 < v < 20.0:
                vals.append(v)
            sleep(0.002)
        if not vals:
            return None
        vals.sort()
        if len(vals) > 2 * _TRIM:
            vals = vals[_TRIM:-_TRIM]
        avg = sum(vals) / len(vals)
        global _v_filt
        _v_filt = avg if _v_filt is None else (1.0 - _ALPHA) * _v_filt + _ALPHA * avg
        return float(_v_filt)
    except Exception as e:
        log.debug("Battery read failed: %s", e)
        return None

def compute_battery():
    v = get_battery_voltage()
    if v is None:
        return None, None
    try:
        if VBAT_MAX <= VBAT_MIN:
            return v, None
        r = (v - VBAT_MIN) / (VBAT_MAX - VBAT_MIN)
        r = max(0.0, min(1.0, r))
        r = r - 0.15 * r * (1.0 - r)
        p = max(0.0, min(100.0, r * 100.0))
        return v, p
    except Exception:
        return None, None

def battery_monitor_loop():
    try:
        v, p = compute_battery()
        battery_last.update({"voltage": v, "percent": p, "ts": time()})
        if v is not None and p is not None and connected_clients:
            socketio.emit("battery_state", {"voltage": v, "percent": int(round(p))})
    except Exception:
        pass
    while True:
        socketio.sleep(BATTERY_POLL_SEC)
        try:
            if not connected_clients:
                continue
            v, p = compute_battery()
            battery_last.update({"voltage": v, "percent": p, "ts": time()})
            if v is not None and p is not None:
                socketio.emit("battery_state", {"voltage": v, "percent": int(round(p))})
        except Exception:
            pass

# ---------- Motor controller ----------
class MotorController:
    def __init__(self, picarx, rate=8):
        self.px = picarx
        self.rate = max(1, int(rate))
        self.cur_l = self.cur_r = self.tgt_l = self.tgt_r = 0
        self._stop = Event()
        self._lock = Lock()
        Thread(target=self._loop, daemon=True).start()

    def _hw(self, l, r):
        log.info("Setting motors: L=%s R=%s", l, r)
        if not self.px:
            return
        try:
            r = -r
            self.px.set_motor_speed(1, l)
            self.px.set_motor_speed(2, r)
        except Exception as e:
            log.warning("Setting motor speed failed: %s", e)

    def _loop(self):
        while not self._stop.is_set():
            changed = False
            with self._lock:
                for side in ("l", "r"):
                    cur = getattr(self, "cur_" + side)
                    tgt = getattr(self, "tgt_" + side)
                    if cur != tgt:
                        d = tgt - cur
                        step = min(self.rate, abs(d))
                        setattr(self, "cur_" + side, int(math.copysign(step, d)) + cur)
                        changed = True
                cur_l, cur_r = self.cur_l, self.cur_r
            if changed:
                self._hw(cur_l, cur_r)
            else:
                sleep(0.03)
                continue
            sleep(0.03)

    def set_target(self, l, r):
        with self._lock:
            self.tgt_l = clamp(l, -100, 100)
            self.tgt_r = clamp(r, -100, 100)

    def stop(self):
        with self._lock:
            self.tgt_l = self.tgt_r = self.cur_l = self.cur_r = 0
        self._hw(0, 0)

    def shutdown(self):
        self._stop.set()
        self.stop()

    def snapshot(self):
        with self._lock:
            return {
                "current_left": int(self.cur_l),
                "current_right": int(self.cur_r),
                "target_left": int(self.tgt_l),
                "target_right": int(self.tgt_r),
            }

# ---------- Hardware init ----------
if reset_mcu:
    try:
        reset_mcu()
        sleep(0.05)
    except Exception as e:
        log.warning("reset_mcu() failed: %s", e)

px = Picarx() if Picarx else None
if not px:
    log.info("Picarx not available in this environment.")

music = Music() if Music else None
if not music:
    log.info("Music not available in this environment.")

if Vilib:
    try:
        Vilib.camera_start(vflip=False, hflip=False)
        if hasattr(Vilib, "display"):
            try:
                Vilib.display(local=False, web=True)
            except Exception as e:
                log.info("Vilib.display() non-fatal: %s", e)
    except Exception as e:
        log.warning("Vilib.camera_start() failed: %s", e)
else:
    log.info("Vilib not available in this environment.")

adc_batt = ADC(BATTERY_ADC_PIN) if ADC else None
if not adc_batt:
    log.info("ADC not available in this environment.")

mot = MotorController(px, rate=state["ramp_rate"])

safe_set_dir_servo = lambda angle: safe_set("dir servo angle", getattr(px, "set_dir_servo_angle", None), angle)
safe_set_cam_pan   = lambda angle: safe_set("cam pan angle",   getattr(px, "set_cam_pan_angle", None),   angle)
safe_set_cam_tilt  = lambda angle: safe_set("cam tilt angle",  getattr(px, "set_cam_tilt_angle", None),  angle)

def music_control(action, song=None, volume=100):
    if not music:
        log.info("Music not available in this environment.")
        return False
    try:
        if action == "play":
            try:
                music.music_set_volume(volume)
            except Exception:
                pass
            music.music_play(song)
            log.info("Playing music: %s at volume %s", song, volume)
        else:
            music.music_stop()
            log.info("Stopping music")
        return True
    except Exception as e:
        log.warning("Music action '%s' failed: %s", action, e)
        return False

# ---------- Steering / head / photo ----------
def map_steer_to_servo(steer):
    s = clamp(steer, -100, 100)
    return int(round(DIR_MIN + (s + 100) * (DIR_MAX - DIR_MIN) / 200.0))

def set_steer_throttle(throttle, steer):
    throttle = clamp(throttle, -100, 100)
    steer = clamp(steer, -100, 100)
    state["throttle"], state["steer"] = throttle, steer
    safe_set_dir_servo(map_steer_to_servo(steer))
    steer_scaled = int(round(steer * (1.0 - abs(throttle) / 100.0)))
    l = clamp(throttle + steer_scaled, -100, 100)
    r = clamp(throttle - steer_scaled, -100, 100)
    mot.set_target(l, r)

def set_head(pan=None, tilt=None):
    if pan is not None:
        state["pan"] = clamp(pan, CAM_PAN_MIN, CAM_PAN_MAX)
        safe_set_cam_pan(state["pan"])
    if tilt is not None:
        state["tilt"] = clamp(tilt, CAM_TILT_MIN, CAM_TILT_MAX)
        safe_set_cam_tilt(state["tilt"])
    return {"pan": state["pan"], "tilt": state["tilt"]}

def prune_photos(max_photos=MAX_PHOTOS):
    try:
        files = [f for f in os.listdir(PHOTO_FOLDER) if f.lower().endswith(('.jpg', '.jpeg', '.png', '.gif'))]
        if len(files) <= max_photos:
            return
        files.sort(key=lambda fn: os.path.getmtime(os.path.join(PHOTO_FOLDER, fn)))
        to_remove = files[:max(0, len(files) - max_photos)]
        for fn in to_remove:
            try:
                os.remove(os.path.join(PHOTO_FOLDER, fn))
                log.info("Pruned old photo: %s", fn)
            except Exception as e:
                log.warning("Failed to remove old photo %s: %s", fn, e)
    except Exception as e:
        log.debug("prune_photos failed: %s", e)

def take_photo():
    name = f"photo_{strftime('%Y-%m-%d-%H-%M-%S', localtime(time()))}.jpg"
    path = os.path.join(PHOTO_FOLDER, name)
    if Vilib and hasattr(Vilib, "take_photo"):
        try:
            res = Vilib.take_photo(name[:-4], PHOTO_FOLDER + "/")
            if isinstance(res, str) and os.path.exists(res):
                prune_photos()
                return res
        except Exception as e:
            log.info("Vilib.take_photo failed (falling back): %s", e)
    if not cv2:
        return None
    try:
        cap = cv2.VideoCapture(0)
        if not cap or not cap.isOpened():
            if cap: cap.release()
            return None
        ok, frame = cap.read()
        cap.release()
        if not ok or frame is None:
            return None
        try:
            cv2.imwrite(path, frame)
            prune_photos()
            return path
        except Exception as e:
            log.warning("cv2.imwrite failed: %s", e)
            return None
    except Exception as e:
        log.info("OpenCV capture failed: %s", e)
        return None

# ---------- Automation: Obstacle monitor ----------
class ObstacleMonitor:
    def __init__(self, picarx, threshold_cm=OBSTACLE_THRESHOLD_CM, clear_cm=OBSTACLE_CLEAR_CM):
        self.px = picarx
        self.th = float(threshold_cm)
        self.cl = float(clear_cm)

    def _read_distance(self):
        if not self.px or not hasattr(self.px, "get_distance"):
            return None
        try:
            d = float(self.px.get_distance() or 0.0)
            if not (0.1 <= d <= 400.0):
                return None
            return d
        except Exception:
            return None

    def _update(self, dist):
        now = time()
        obstacle_state["distance_cm"] = dist
        obstacle_state["ts"] = now
        blk = obstacle_state["blocked_forward"]
        changed = False
        if dist is None:
            return False
        if blk:
            if dist > self.cl:
                obstacle_state["blocked_forward"] = False
                changed = True
        else:
            if dist < self.th:
                obstacle_state["blocked_forward"] = True
                changed = True
        return changed

    def loop(self):
        while True:
            socketio.sleep(OBSTACLE_POLL_SEC)
            try:
                d = self._read_distance()
                changed = self._update(d)
                if changed:
                    emit_obstacle_state()
                    if obstacle_state["blocked_forward"]:
                        if auto_state.get("crash_avoid_enabled") and playback.is_playing():
                            playback.pause("obstacle_detected")
                        if state.get("throttle", 0) > 0:
                            stop_motors_broadcast("obstacle detected", origin="auto_crash", record=True)
                    else:
                        if playback.is_playing() and playback.is_paused():
                            playback.resume("obstacle_cleared")
            except Exception:
                pass

obmon = ObstacleMonitor(px)


# ---------- Automation: Line follower ----------
class LineFollower:
    def __init__(self, picarx):
        self.px = picarx
        self._stop = Event()
        self._task = None
        self.running = False

    def is_running(self):
        return self.running

    def start(self):
        if self.running:
            return False
        if not self.px or not hasattr(self.px, "get_grayscale_data"):
            log.info("Line follow unavailable (no sensors).")
            return False
        self._stop.clear()
        self.running = True
        self._task = socketio.start_background_task(self._loop)
        return True

    def stop(self, reason=None):
        if not self.running:
            return
        self._stop.set()
        self.running = False
        if reason:
            log.info("Line follower stopped: %s", reason)

    def _compute(self, vals):
        try:
            if hasattr(self.px, "get_line_status"):
                st = safe_call(self.px, "get_line_status", vals, log_label="line_status", default=None)
                if isinstance(st, (list, tuple)) and len(st) >= 3:
                    # treat numeric 0/1 or bools
                    L, M, R = int(bool(st[0])), int(bool(st[1])), int(bool(st[2]))
                    # interpret 0=line, 1=background (as in the sample script)
                    if L == 0 and M == 0 and R == 0:
                        return 0, 0
                    if M == 1:
                        return LINEFOLLOW_BASE_SPEED, 0
                    # if left sensor reads background -> line on right -> steer right
                    if L == 1:
                        return LINEFOLLOW_BASE_SPEED, -40
                    if R == 1:
                        return LINEFOLLOW_BASE_SPEED, 40
        except Exception as e:
            log.debug("get_line_status helper failed: %s", e)

    def _loop(self):
        origin = "auto_line"
        while not self._stop.is_set():
            if playback.is_playing():
                self.stop("playback started")
                break
            try:
                vals = safe_call(self.px, "get_grayscale_data", log_label="grayscale", default=None)
                if not isinstance(vals, (list, tuple)) or len(vals) < 3:
                    socketio.sleep(0.06)
                    continue

                thr, st = self._compute(vals)

                # respect crash avoidance
                if auto_state.get("crash_avoid_enabled") and obstacle_state.get("blocked_forward"):
                    thr = 0

                set_steer_throttle(thr, st)
                broadcast_input({"throttle": thr, "steer": st, "_origin": origin})
                recorder.record_event("drive", {"throttle": thr, "steer": st})
            except Exception as e:
                log.debug("Line follower step failed: %s", e)
            socketio.sleep(0.06)

linef = LineFollower(px)

def set_line_follow_enabled(enabled: bool, reason=None):
    auto_state["line_follow_enabled"] = bool(enabled)
    if enabled:
        ok = linef.start()
        if not ok:
            auto_state["line_follow_enabled"] = False
    else:
        linef.stop(reason or "disabled")

def set_crash_avoid_enabled(enabled: bool):
    auto_state["crash_avoid_enabled"] = bool(enabled)
    if enabled and obstacle_state.get("blocked_forward") and state.get("throttle", 0) > 0:
        stop_motors_broadcast("crash avoidance enabled while forward blocked", origin="auto_crash", record=True,)
        if playback.is_playing() and not playback.is_paused():
            playback.pause("crash_avoid_enabled_and_blocked")
    elif not enabled:
        if playback.is_playing() and playback.is_paused():
            playback.resume("crash_avoid_disabled")

# ---------- Flask + SocketIO ----------
app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

redirect_app = Flask("redirect_app")

@redirect_app.route("/", defaults={"path": ""})
@redirect_app.route("/<path:path>")
def redirect_to_https(path):
    host = request.host.split(":")[0] 
    https_port = os.environ.get("PORT", "443")
    target = f"https://{host}:{https_port}/{path}"
    return redirect(target, code=301)

def start_redirect_server(host="0.0.0.0", port=80):
    global redirect_server
    from werkzeug.serving import make_server

    try:
        srv = make_server(host, port, redirect_app)
        thr = Thread(target=srv.serve_forever, daemon=True)
        thr.start()
        redirect_server = srv
        log.info("Redirect server started on http://%s:%s -> https", host, port)
        return srv
    except Exception as e:
        log.warning("Failed to start redirect server: %s", e)
        return None

# ---------- Recorder & Playback ----------
class Recorder:
    def __init__(self, path):
        self.path = path
        self.recording = False
        self.started = 0.0
        self.events = []
        self._lock = Lock()

    def start(self):
        with self._lock:
            if self.recording:
                return False
            self.recording = True
            self.started = time()
            self.events = [
                {"t": 0.0, "type": "drive", "throttle": clamp(state.get("throttle", 0), -100, 100), "steer": clamp(state.get("steer", 0), -100, 100)},
                {"t": 0.0, "type": "head", "pan": clamp(state.get("pan", 0), CAM_PAN_MIN, CAM_PAN_MAX), "tilt": clamp(state.get("tilt", 0), CAM_TILT_MIN, CAM_TILT_MAX)}
            ]
            log.info("Recording started (baseline captured).")
        emit_recorder_state()
        return True

    def stop(self, save=True):
        with self._lock:
            if not self.recording:
                return None
            t_rel = max(0.0, time() - self.started)
            self.events.append({"t": t_rel, "type": "drive", "throttle": clamp(state.get("throttle", 0), -100, 100), "steer": clamp(state.get("steer", 0), -100, 100)})
            self.events.append({"t": t_rel, "type": "head", "pan": clamp(state.get("pan", 0), CAM_PAN_MIN, CAM_PAN_MAX), "tilt": clamp(state.get("tilt", 0), CAM_TILT_MIN, CAM_TILT_MAX)})
            data = {"version": 1, "created": self.started, "duration": max(0.0, time() - self.started), "events": list(self.events)}
            self.recording = False
            self.started = 0.0
        if save:
            try:
                tmp = self.path + ".tmp"
                with open(tmp, "w") as f:
                    json.dump(data, f, separators=(",", ":"), ensure_ascii=False)
                os.replace(tmp, self.path)
                log.info("Recording saved to %s", self.path)
            except Exception as e:
                log.warning("Saving recording failed: %s", e)
        emit_recorder_state()
        return {"duration": data["duration"]}

    def record_event(self, typ, payload):
        if not self.recording:
            return
        try:
            ev = {"t": time() - self.started, "type": typ}
            if typ == "drive":
                ev["throttle"] = clamp(payload.get("throttle", 0), -100, 100)
                ev["steer"] = clamp(payload.get("steer", 0), -100, 100)
            elif typ == "head":
                if payload.get("pan") is not None:
                    ev["pan"] = clamp(payload.get("pan"), CAM_PAN_MIN, CAM_PAN_MAX)
                if payload.get("tilt") is not None:
                    ev["tilt"] = clamp(payload.get("tilt"), CAM_TILT_MIN, CAM_TILT_MAX)
            elif typ == "music":
                ev["action"] = payload.get("action")
                if "song" in payload:
                    ev["song"] = payload.get("song")
                if "bpm" in payload:
                    ev["bpm"] = payload.get("bpm")
            elif typ == "photo":
                pass
            self.events.append(ev)
        except Exception as e:
            log.debug("record_event failed: %s", e)

    def available(self):
        try:
            return os.path.exists(self.path) and os.path.getsize(self.path) > 0
        except Exception:
            return False

    def load(self):
        try:
            with open(self.path, "r") as f:
                return json.load(f)
        except Exception as e:
            log.info("No recording to load or failed: %s", e)
            return None

    def state(self):
        return {"recording": self.recording, "available": self.available()}

class PlaybackRunner:
    def __init__(self):
        self.playing = False
        self._stop = Event()
        self._lock = Lock()
        self._task = None
        # pause bookkeeping
        self._paused = False
        self._pause_start = None
        self._paused_reason = None

    def is_playing(self):
        return self.playing

    def is_paused(self):
        return bool(self._paused)

    def play(self, data):
        if not data or "events" not in data:
            return False
        with self._lock:
            self._stop.clear()
            self._paused = False
            self._pause_start = None
            self._paused_reason = None
            self.playing = True
        emit_playback_state()
        if auto_state.get("line_follow_enabled"):
            set_line_follow_enabled(False, reason="playback")
        self._task = socketio.start_background_task(self._run, data)
        return True

    def stop(self, reason=None):
        with self._lock:
            self._stop.set()
            if self.playing:
                log.info("Playback stop: %s", reason or "")
            self.playing = False
            self._paused = False
            self._pause_start = None
            self._paused_reason = None
        emit_playback_state()

    def pause(self, reason=None):
        with self._lock:
            if not self.playing or self._paused:
                return
            self._paused = True
            self._pause_start = time()
            self._paused_reason = reason
            log.info("Playback paused: %s", reason or "")
        emit_playback_state()

    def resume(self, reason=None):
        with self._lock:
            if not self.playing or not self._paused:
                return
            # clear pause; actual timeline fixup is done inside _run
            log.info("Playback resume: %s (was paused for: %s)", reason or "", self._paused_reason)
            self._paused = False
            self._pause_start = None
            self._paused_reason = None
        emit_playback_state()

    def _wait_if_blocked_forward(self, need_forward):
        if not need_forward:
            return
        while not self._stop.is_set() and obstacle_state.get("blocked_forward") and auto_state.get("crash_avoid_enabled"):
            socketio.sleep(0.08)

    def _run(self, data):
        evs = sorted(data.get("events", []), key=lambda e: float(e.get("t", 0.0)))
        t0 = time()
        outer_stop = False
        for ev in evs:
            if self._stop.is_set():
                break
            ev_t = float(ev.get("t", 0.0))
            target = t0 + ev_t
            # wait loop (handles pause/resume and normal waiting)
            while not self._stop.is_set():
                # handle pause: when paused we wait and on resume, shift t0 by pause duration
                if self._paused:
                    pause_start = self._pause_start or time()
                    # wait until unpaused
                    while self._paused and not self._stop.is_set():
                        socketio.sleep(0.05)
                    if self._stop.is_set():
                        outer_stop = True
                        break
                    # we resumed: shift timeline so remaining events will be relative to resume time
                    paused_duration = time() - pause_start
                    t0 += paused_duration
                    target = t0 + ev_t

                if ev.get("type") == "drive":
                    thr = clamp(ev.get("throttle", 0), -100, 100)
                    if thr > 0 and auto_state.get("crash_avoid_enabled") and obstacle_state.get("blocked_forward"):
                        self.pause("obstacle_detected")
                        continue

                dt = target - time()
                if dt <= 0:
                    break
                socketio.sleep(min(0.05, dt))
            if outer_stop or self._stop.is_set():
                break
            self._dispatch(ev)
        self.stop("done")

    def _dispatch(self, ev):
        typ = ev.get("type")
        if typ == "drive":
            thr = clamp(ev.get("throttle", 0), -100, 100)
            st = clamp(ev.get("steer", 0), -100, 100)
            if thr > 0 and auto_state.get("crash_avoid_enabled") and obstacle_state.get("blocked_forward"):
                thr = 0
            set_steer_throttle(thr, st)
            broadcast_input({"throttle": thr, "steer": st, "_origin": "playback"}, force=True)
        elif typ == "head":
            pan, tilt = ev.get("pan"), ev.get("tilt")
            if pan is not None or tilt is not None:
                set_head(pan=pan, tilt=tilt)
                broadcast_input({
                    **({"pan": state["pan"]} if pan is not None else {}),
                    **({"tilt": state["tilt"]} if tilt is not None else {}),
                    "_origin": "playback"
                }, force=True)
        elif typ == "music":
            action = ev.get("action")
            if action in ("play", "stop"):
                ok = music_control(action, ev.get("song"), state.get("volume", 100))
                if action == "play" and ok:
                    music_state.update({"playing": True, "song": ev.get("song"), "bpm": ev.get("bpm"), "since": time()})
                else:
                    music_state.update({"playing": False, "song": None, "bpm": None, "since": 0.0})
                emit_music_state()
        elif typ == "photo":
            try:
                p = take_photo()
                if p:
                    socketio.emit("gallery_update")
            except Exception:
                pass

    def state(self):
        return {"playing": self.playing, "paused": bool(self._paused)}

recorder = Recorder(RECORDING_FILE)
playback = PlaybackRunner()

def emit_recorder_state():
    socketio.emit("recorder_state", recorder.state())

def emit_playback_state():
    socketio.emit("playback_state", playback.state())

# ---------- Page (template) ----------
PAGE = """
<!doctype html>
<html>
<head>
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Cosmocrew — Mars Rover WebControl</title>

<meta name="theme-color" content="#051016"/>

<style>
:root{
  --solid-bg:#051016;--card:rgba(255,255,255,0.04);
  --accent:#ff7a59;--accent-2:#ef5e39;--muted:#9fb0c9;--cream:#f6e6cf;
  --glass-border:rgba(255,255,255,0.06);--pill-bg:rgba(255,255,255,0.03);
  --control-min-height:420px; /* baseline control height */
  color:var(--cream);font-family:Inter,system-ui,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial
}

/* basic page layout */
*{box-sizing:border-box}
html,body{height:100%;margin:0;background:var(--solid-bg);-webkit-font-smoothing:antialiased;-webkit-text-size-adjust: 100%;text-size-adjust: 100%}
body{display:flex;flex-direction:column;min-height:100vh}
.main{flex:1 0 auto;display:flex;flex-direction:column;align-items:center;padding:28px 16px;position:relative;z-index:0}
.main::before{content:"";position:absolute;inset:0;margin:auto;pointer-events:none;background:radial-gradient(1200px 600px at 50% 40%,rgba(255,255,255,0.02),rgba(255,255,255,0.008) 20%,transparent 60%);mix-blend-mode:overlay;opacity:.8;border-radius:0;z-index:0}
.header{display:flex;align-items:center;gap:14px;padding:14px 20px;background:var(--solid-bg);border-bottom:1px solid rgba(255,255,255,0.02);z-index:3}
.logo{display:flex;align-items:center;gap:12px}
.logo img{height:44px;width:auto;border-radius:8px;box-shadow:0 6px 20px rgba(0,0,0,.5)}
.brand{font-weight:700;font-size:15px;color:var(--cream);letter-spacing:1px}
.sub{font-size:11px;color:var(--muted);margin-top:3px;font-weight:600}
.footer{flex:0 0 auto;padding:16px 14px;text-align:center;color:var(--muted);font-size:13px;border-top:1px solid rgba(255,255,255,0.02);background:var(--solid-bg);position:relative;z-index:2}
.footer .footer-inner{max-width:1200px;margin:0 auto}

/* GRID: video (left) + controls (right) on top row, photos spanning both below.
   align-items:stretch makes both top items match the tallest row height. */
.container{
  display:grid;
  grid-template-columns:minmax(300px,1fr) 420px;
  grid-template-areas:"video control" "photo photo";
  gap:20px;max-width:1200px;width:100%;align-items:stretch;justify-content:center
}

/* ensure top-row items stretch to the same height; video card has a sensible min height */
.video-card,.control-panel{align-self:stretch}
.video-card{grid-area:video;position:relative;display:flex;flex-direction:column;min-height:var(--control-min-height)}
.control-panel{grid-area:control;display:flex;flex-direction:column;gap:14px;min-width:300px}
.photo-card{grid-area:photo;min-height:120px}

/* generic card */
.card{background:linear-gradient(180deg,rgba(255,255,255,0.02),rgba(255,255,255,0.015));border-radius:14px;padding:14px;box-shadow:0 6px 30px rgba(0,0,0,.6);border:1px solid var(--glass-border);color:var(--cream)}

/* video element fills card area */
.video-card #videoFeed{width:100%;height:100%;border-radius:12px;border:1px solid rgba(255,255,255,0.04);display:block;box-shadow:0 18px 40px rgba(2,8,12,.6);object-fit:contain}

/* control elements */
.slider-row{display:flex;flex-direction:column;gap:8px}.small{font-size:13px;color:var(--muted)}.range{width:100%}
input[type=range]{-webkit-appearance:none;height:10px;border-radius:12px;background:linear-gradient(90deg,var(--accent),var(--accent-2));outline:none}
input[type=range]::-webkit-slider-thumb{-webkit-appearance:none;width:18px;height:18px;border-radius:50%;background:#fff;border:3px solid var(--accent)}
input[type=range]::-moz-range-thumb{width:18px;height:18px;border-radius:50%;background:#fff;border:3px solid var(--accent)}

.row{display:flex;gap:8px;align-items:center}
.main-row-container{display:flex;flex-direction:column;gap:8px}
.main-row{display:flex;gap:10px;align-items:center;flex-wrap:nowrap}
.secondary-row{display:flex;gap:10px;align-items:center}
.drive-pad-wrapper{display:flex;align-items:center;justify-content:center}
.drive-pad{width:220px;height:220px;border-radius:50%;display:flex;align-items:center;justify-content:center;background:radial-gradient(circle at 30% 20%,rgba(255,255,255,0.02),rgba(0,0,0,0.06));border:8px solid #0f191e;box-shadow:0 18px 40px rgba(2,8,12,.6),inset 0 2px 12px rgba(255,255,255,0.02);touch-action:none;user-select:none;position:relative}
.drive-pad::after,.drive-pad::before{content:"";position:absolute;border-radius:4px;z-index:1}
.drive-pad::after{width:48%;height:2px;background:linear-gradient(to right,rgba(255,255,255,0.03),rgba(255,255,255,0.06))}
.drive-pad::before{height:48%;width:2px;background:linear-gradient(to bottom,rgba(255,255,255,0.03),rgba(255,255,255,0.06))}
.drive-nub{width:70px;height:70px;border-radius:50%;background:linear-gradient(180deg,var(--accent),var(--accent-2));display:flex;align-items:center;justify-content:center;color:#fff;font-weight:800;z-index:2;box-shadow:0 10px 26px rgba(239,94,57,.22),0 6px 12px rgba(0,0,0,.5);border:6px solid #cf5a3c;transform:translate(0,0);transition:transform .02s linear;position:relative;overflow:hidden}
.drive-block-overlay{position:absolute;top:-4px;left:-4px;right:-4px;height:54%;border-top-left-radius:999px;border-top-right-radius:999px;pointer-events:none;opacity:0;background:radial-gradient(140px 90px at 50% 0%,rgba(239,94,57,0.55),rgba(239,94,57,0.18) 60%,rgba(239,94,57,0) 80%),linear-gradient(to bottom,rgba(255,122,89,0.28),rgba(255,122,89,0));mask-image:linear-gradient(to bottom,black 70%,transparent);-webkit-mask-image:linear-gradient(to bottom,black 70%,transparent);transition:opacity .12s ease-in-out}
.drive-pad.forward-blocked .drive-block-overlay{opacity:.45}

/* action buttons */
.actions{display:flex;flex-direction:column;gap:12px}.icon-row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.btn,.icon-btn,.btn-primary,.btn-ghost{cursor:pointer;border-radius:10px;padding:10px 12px;border:1px solid rgba(255,255,255,0.04);font-weight:700;background:linear-gradient(180deg,rgba(255,255,255,0.02),rgba(0,0,0,0.02));color:var(--cream)}
.btn-primary{background:linear-gradient(90deg,var(--accent),var(--accent-2));color:#fff;border:none;box-shadow:0 10px 26px rgba(239,94,57,.18)}
.btn-ghost{background:transparent;border:1px solid rgba(255,255,255,0.06);transition:box-shadow .12s ease,background .12s ease,color .12s ease}
.btn-ghost.active,.btn-ghost[aria-pressed="true"]{background:linear-gradient(180deg,#f7ead0 0%,#f5dfb3 100%);color:#2b1a05;border:1px solid rgba(30,20,10,0.12);box-shadow:0 10px 20px rgba(8,6,4,0.18),inset 0 1px 0 rgba(255,255,255,0.35)}
.icon-btn{width:44px;height:44px;border-radius:10px;display:inline-flex;align-items:center;justify-content:center;background:var(--pill-bg);border:1px solid rgba(255,255,255,0.03)}
.icon-btn.listening{background:linear-gradient(90deg,var(--accent),var(--accent-2));color:#fff;border-color:rgba(0,0,0,0.06);transform:scale(1.04)}

/* photo gallery: larger thumbs on larger screens */
.photo-gallery{display:grid;grid-template-columns:repeat(auto-fill,minmax(140px,1fr));gap:12px}
.thumb{width:100%;height:90px;object-fit:cover;border-radius:8px;cursor:pointer;border:2px solid rgba(255,255,255,0.03);box-shadow:0 8px 18px rgba(0,0,0,.45);transition:transform .12s ease,box-shadow .12s ease}
@media(min-width:900px){.photo-gallery{grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:14px}.thumb{height:110px}}
@media(min-width:1200px){.photo-gallery{grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:16px}.thumb{height:140px}}

/* battery widget */
.battery-widget{display:flex;align-items:center;gap:8px;padding:6px 8px;border-radius:15px;border:1px solid rgba(255,255,255,0.03);background:var(--pill-bg)}
.battery-widget .pct{font-weight:800;color:var(--cream);min-width:32px;text-align:right;font-size:14px}

/* control stretching helper: control-inner fills vertical space, spacer consumes remainder */
.control-inner{display:flex;flex-direction:column;height:100%}
.control-spacer{flex:1}

/* mobile: single column order video, controls, photos (photos already span) */
@media(max-width:900px){
  .container{grid-template-columns:1fr;grid-template-areas:"video" "control" "photo"}
  .control-panel{width:100%}
  .video-card{max-width:100%}
  .photo-card{width:100%}
}

/* small hover polish */
@media(hover:hover) and (pointer:fine){.btn-primary:hover{transform:translateY(-3px)}.thumb:hover{transform:translateY(-6px);box-shadow:0 22px 46px rgba(0,0,0,.6)}}
</style>
</head>
<body>
<div class="header">
  <div class="logo">
    <img src="{{ url_for('static', filename='cosmocrew-logo.png') }}" alt="Cosmocrew logo">
    <div>
      <div class="brand">COSMOCREW</div>
      <div class="sub">Mars Rover WebControl</div>
    </div>
  </div>
  <div style="flex:1"></div>
  <div id="batteryWidget" class="battery-widget" title="Battery">
    <svg id="battSvg" width="46" height="24" viewBox="0 0 52 28" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
      <rect x="2" y="4" width="42" height="20" rx="5" ry="5" stroke="rgba(255,255,255,0.5)" stroke-width="2" fill="none"/>
      <rect id="battFill" x="4" y="6" width="0" height="16" rx="4" ry="4" fill="#4caf50"/>
      <rect x="46" y="10" width="6" height="8" rx="2" ry="2" fill="rgba(255,255,255,0.5)"/>
    </svg>
    <div class="pct"><span class="val">--</span>%</div>
  </div>
</div>

<div class="main">
  <div class="container">
    <!-- Live view on the left -->
    <div class="card video-card">
      <img id="videoFeed" src="/video_feed" alt="video feed" />
    </div>

    <!-- Controls on the right (control-inner + spacer to expand if needed) -->
    <div class="card control-panel">
      <div class="control-inner">
        <div class="slider-row">
          <div>
            <div class="small" style="margin-bottom:4px">Camera Pan <span style="float:right;color:var(--muted)" id="panVal">{{ pan }}</span></div>
            <input id="pan" class="range" type="range" min="{{ CAM_PAN_MIN }}" max="{{ CAM_PAN_MAX }}" value="{{ pan }}" oninput="setHead('pan',this.value); document.getElementById('panVal').textContent=this.value">
          </div>
          <div>
            <div class="small" style="margin-bottom:4px">Camera Tilt <span style="float:right;color:var(--muted)" id="tiltVal">{{ tilt }}</span></div>
            <input id="tilt" class="range" type="range" min="{{ CAM_TILT_MIN }}" max="{{ CAM_TILT_MAX }}" value="{{ tilt }}" oninput="setHead('tilt',this.value); document.getElementById('tiltVal').textContent=this.value">
          </div>
        </div>

        <div class="row" style="margin:32px 0 32px 0">
          <div class="drive-pad-wrapper" style="width:100%">
            <div class="drive-pad" id="drivePad">
              <div class="drive-block-overlay" id="driveBlockOverlay" aria-hidden="true"></div>
              <div class="drive-nub" id="driveNub"></div>
            </div>
          </div>
        </div>

        <div class="actions">
          <div class="icon-row">
            <button class="icon-btn" id="voiceBtn" onclick="toggleVoice()" aria-label="Voice" aria-pressed="false" title="Voice">
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
                <path d="M12 14a3 3 0 0 0 3-3V6a3 3 0 0 0-6 0v5a3 3 0 0 0 3 3z" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/>
                <path d="M19 11a7 7 0 0 1-14 0" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/>
              </svg>
            </button>

            <button class="icon-btn" id="recordBtn" onclick="toggleRecord()" aria-label="Record" aria-pressed="false" title="Record">
              <svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor" xmlns="http://www.w3.org/2000/svg" aria-hidden="true"><circle cx="12" cy="12" r="6.5"/></svg>
            </button>

            <button class="icon-btn" id="playbackBtn" onclick="togglePlayback()" aria-label="Playback" aria-pressed="false" title="Playback">
              <svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor" xmlns="http://www.w3.org/2000/svg" aria-hidden="true"><path d="M8 5v14l11-7-11-7z"/></svg>
            </button>
          </div>

          <div class="main-row-container">
            <div class="main-row">
              <button class="btn-primary" id="photoBtn" onclick="takePhoto()">Take Photo</button>
              <button class="btn-ghost" onclick="centerHead()">Center Cam</button>
              <button class="btn-ghost" id="danceBtn" onclick="toggleMusic()">Start Dance</button>
            </div>
            <div class="secondary-row">
              <button class="btn-ghost" id="lineFollowBtn" onclick="toggleLineFollow()" aria-pressed="false">Line Following</button>
              <button class="btn-ghost" id="crashAvoidBtn" onclick="toggleCrashAvoid()" aria-pressed="false">Crash Avoidance</button>
            </div>
          </div>
        </div>

        <!-- spacer grows to make controls fill the same height as video when needed -->
        <div class="control-spacer"></div>
      </div>
    </div>

    <!-- Photos card placed under both columns and spans the full width -->
    <div class="card photo-card">
      <div style="margin-bottom:8px" class="small">Recent photos:</div>
      <div id="photoGallery" class="photo-gallery"></div>
    </div>

  </div>
</div>

<div class="footer">
  <div class="footer-inner">
    Tips — Keyboard: W/A/S/D drive, Space stop, T photo, M dance, C center cam, F line follow, G crash avoid, Arrows pan/tilt. Gamepad: left stick drive, LB/RB pan, LT/RT tilt, A photo, B dance, Y record, X playback, Select line follow, Start crash avoid.
  </div>
</div>
<script src="{{ url_for('static', filename='socket.io.min.js') }}"></script>
<script>
const s=io(),DRIVE_SEND_MS=33,DRIVE_EPS=2;let mySid=null;s.on('connect',()=>{mySid=s.id;s.emit('hi')})
const $=id=>document.getElementById(id),clamp=(v,a,b)=>Math.max(a,Math.min(b,~~v))
let _p={thr:0,str:0,dirty:false,force:false},_last={thr:0,str:0},_lastTs=0,_timer=null,_lastRefusedForward=null
const _startLoop=()=>{
  if(_timer) return
  _timer=setInterval(()=>{
    const now=performance.now(),due=now-_lastTs>=DRIVE_SEND_MS,p=_p,l=_last
    const changed=Math.abs(p.thr-l.thr)>DRIVE_EPS||Math.abs(p.str-l.str)>DRIVE_EPS||p.force
    if(due&&(changed||p.dirty)){s.emit('cmd',{type:'drive',throttle:p.thr,steer:p.str});_last={thr:p.thr,str:p.str};_lastTs=now;p.force=false;p.dirty=false}
  },Math.max(20,DRIVE_SEND_MS/2))
}
function scheduleDrive(throttle,steer,opts){
  const thr=clamp(throttle,-100,100),str=clamp(steer,-100,100)
  const now=(typeof performance!=='undefined'&&performance.now)?performance.now():Date.now()
  if(forwardBlocked&&_autoState.crash_avoid_enabled&&thr>0){_lastRefusedForward={thr,str,ts:now}}
  if(_lastRefusedForward){
    const same=_lastRefusedForward.thr===thr&&_lastRefusedForward.str===str&&Math.abs(now-_lastRefusedForward.ts)<50
    if(!same){ if(thr===0||now>=_lastRefusedForward.ts) _lastRefusedForward=null }
  }
  const chg=Math.abs(thr-_p.thr)>DRIVE_EPS||Math.abs(str-_p.str)>DRIVE_EPS||!!(opts&&opts.force)
  _p.thr=thr;_p.str=str
  if(chg)_p.dirty=true
  if(opts&&opts.force)_p.force=true
  _startLoop()
}
/* music/dance */
let musicPlaying=false,clientDanceRunning=false,clientDanceStart=0,clientDanceBpm=100,clientDanceAnim=null,clientSpinTimeout=null,danceActive=false
const updateMusicButton=()=>{const b=$('danceBtn');if(!b)return;const isD=!!(clientDanceRunning||danceActive);b.textContent=isD?'Stop Dance':(musicPlaying?'Stop Music':'Start Dance')}
const CAM_PAN_MIN={{ CAM_PAN_MIN }},CAM_PAN_MAX={{ CAM_PAN_MAX }},CAM_TILT_MIN={{ CAM_TILT_MIN }},CAM_TILT_MAX={{ CAM_TILT_MAX }}
let _autoState={line_follow_enabled:false,crash_avoid_enabled:false}
const applyAutoButtons=()=>{const lf=$('lineFollowBtn'),ca=$('crashAvoidBtn');if(lf){lf.setAttribute('aria-pressed',String(!!_autoState.line_follow_enabled));lf.classList.toggle('active',!!_autoState.line_follow_enabled)}if(ca){ca.setAttribute('aria-pressed',String(!!_autoState.crash_avoid_enabled));ca.classList.toggle('active',!!_autoState.crash_avoid_enabled)}}
function toggleLineFollow(){stopClientDance();s.emit('cmd',{type:'line_follow',action:!_autoState.line_follow_enabled?'enable':'disable'})}
function toggleCrashAvoid(){s.emit('cmd',{type:'crash_avoid',action:!_autoState.crash_avoid_enabled?'enable':'disable'})}
function setHead(w,v){stopClientDance();s.emit('cmd',{type:'head',[w]:parseInt(v)})}
function centerHead(){stopClientDance();s.emit('cmd',{type:'head',pan:0,tilt:0});updateHeadInputs(0,0,true)}
const takePhoto=()=>s.emit('cmd',{type:'photo'})
s.on('photo_result',r=>{if(!r)return;if(r.error)alert('Photo error: '+r.error);else if(r.path)loadGallery();else alert('No camera available; no photo was captured.')})
s.on('gallery_update',loadGallery)
async function loadGallery(){try{const res=await fetch('/recent_photos'),data=await res.json(),g=$('photoGallery');g.innerHTML='';if(!data||!data.length){g.innerHTML='<div class="small">No photos yet</div>';return}data.forEach(it=>{const img=document.createElement('img');img.src=it.url;img.className='thumb';img.title=it.name;img.onclick=()=>window.open(it.url,'_blank');g.appendChild(img)})}catch(e){console.warn('gallery load failed',e)}}
window.addEventListener('load',loadGallery)
function updateHeadInputs(pan,tilt,force=false){
  const panEl=$('pan'),tiltEl=$('tilt'),panValEl=$('panVal'),tiltValEl=$('tiltVal')
  if(pan!==undefined&&pan!==null){const p=Math.round(+pan);if(panEl)panEl.value=p;if(panValEl&&(force||document.activeElement!==panEl))panValEl.textContent=p}
  if(tilt!==undefined&&tilt!==null){const t=Math.round(+tilt);if(tiltEl)tiltEl.value=t;if(tiltValEl&&(force||document.activeElement!==tiltEl))tiltValEl.textContent=t}
}
const battEl=$('batteryWidget'),battFill=$('battFill'),battPct=battEl?.querySelector('.val')
function updateBatteryUI(percent,voltage){const pct=Math.max(0,Math.min(100,percent|0));if(battPct)battPct.textContent=isFinite(pct)?pct:'--';const w=Math.round(pct/100*38);battFill?.setAttribute('width',String(Math.max(0,Math.min(38,w))));let color='#4caf50';if(pct<20)color='#f44336';else if(pct<40)color='#ff9800';else if(pct<60)color='#ffc107';battFill?.setAttribute('fill',color);if(battEl)battEl.title=typeof voltage==='number'&&isFinite(voltage)?voltage.toFixed(2)+' V':'Battery'}
s.on('battery_state',d=>{if(d)updateBatteryUI(d.percent,d.voltage)})
let forwardBlocked=false
function updateForwardBlockedUI(){const pad=$('drivePad');if(!pad)return;pad.classList.toggle('forward-blocked',!!forwardBlocked)}
s.on('obstacle_state',st=>{if(!st)return;forwardBlocked=!!st.blocked_forward;updateForwardBlockedUI()})
/* client dance */
function startClientDance(bpm=100){
  if(clientDanceRunning)return
  clientDanceRunning=true;clientDanceStart=performance.now()/1000;clientDanceBpm=Math.max(40,Math.min(200,+bpm||100))
  let panCenter=~~$('pan').value||0,tiltCenter=~~$('tilt').value||0
  const panAmp=30,tiltAmp=10,spinSpeed=18,pulseDuty=0.6;let lastPulse=performance.now()/1000,dir=1
  const frame=()=>{
    if(!clientDanceRunning){clientDanceAnim=null;return}
    const now=performance.now()/1000,elapsed=now-clientDanceStart,beat=60/clientDanceBpm,panPeriod=2*beat,tiltPeriod=beat
    const pan=Math.round(panCenter+panAmp*Math.sin(2*Math.PI*((elapsed%panPeriod)/panPeriod)))
    const tilt=Math.round(tiltCenter+tiltAmp*Math.sin(2*Math.PI*((elapsed%tiltPeriod)/tiltPeriod)))
    s.emit('cmd',{type:'head',pan,tilt});updateHeadInputs(pan,tilt)
    const half=beat/2
    if(now-lastPulse>=half){dir=-dir;lastPulse=now;const dur=Math.min(0.18,half*pulseDuty);scheduleDrive(0,dir*spinSpeed,{force:true});if(clientSpinTimeout)clearTimeout(clientSpinTimeout);clientSpinTimeout=setTimeout(()=>{if(clientDanceRunning)scheduleDrive(0,0,{force:true})},Math.round(dur*1000))}
    clientDanceAnim=requestAnimationFrame(frame)
  }
  clientDanceAnim=requestAnimationFrame(frame);updateMusicButton()
}
function stopClientDance(opts){
  if(!(clientDanceRunning||danceActive)){return}
  const keepDrive=!!(opts&&opts.keepDrive);clientDanceRunning=false;danceActive=false
  if(clientDanceAnim){cancelAnimationFrame(clientDanceAnim);clientDanceAnim=null}
  if(clientSpinTimeout){clearTimeout(clientSpinTimeout);clientSpinTimeout=null}
  if(!keepDrive){scheduleDrive(0,0,{force:true});updateJoystickVisual(0,0)}
  updateMusicButton()
}
function toggleMusic(){
  if(danceActive||clientDanceRunning){s.emit('cmd',{type:'music',action:'stop'});stopClientDance();danceActive=false;scheduleDrive(0,0,{force:true});updateMusicButton();return}
  if(musicPlaying){s.emit('cmd',{type:'music',action:'stop'});updateMusicButton();return}
  const bpm=123
  if(isDriveNeutral()){s.emit('cmd',{type:'head',pan:0,tilt:0});updateHeadInputs(0,0,true);startClientDance(bpm);danceActive=true}
  s.emit('cmd',{type:'music',action:'play',bpm});updateMusicButton()
}
s.on('music_state',st=>{musicPlaying=!!(st&&st.playing);if(!musicPlaying&&(clientDanceRunning||danceActive)){stopClientDance();danceActive=false;scheduleDrive(0,0,{force:true});updateJoystickVisual(0,0)}updateMusicButton()})
/* voice (DE) */
let recognition=null,listening=false,endWatch=null,sessionId=0
const SR=window.SpeechRecognition||window.webkitSpeechRecognition
const voiceBtn=$('voiceBtn'),isSecure=window.isSecureContext===true
const updateVoiceUI=()=>{if(!voiceBtn)return;voiceBtn.classList.toggle('listening',listening);voiceBtn.setAttribute('aria-pressed',String(listening));voiceBtn.title=!isSecure?'Voice requires HTTPS':(SR?(listening?'Listening… tap again to stop':'Voice'):'Voice not supported')}
const setL=on=>{listening=!!on;updateVoiceUI()}
const hardStop=()=>{try{recognition&&recognition.stop()}catch(_){ }clearTimeout(endWatch);endWatch=setTimeout(()=>{try{recognition&&recognition.abort()}catch(_){ }setL(false)},800)}
function initRecognition(){
  if(!SR||!isSecure)return null
  if(recognition)return recognition
  const r=new SR(),sid=++sessionId
  Object.assign(r,{lang:'de-DE',continuous:false,interimResults:true,maxAlternatives:3})
  r.onstart=()=>{if(sid!==sessionId)return;clearTimeout(endWatch);setL(true)}
  r.onaudioend=()=>{if(sid!==sessionId)return;setL(false)}
  r.onerror=e=>{if(sid!==sessionId)return;if(e&&e.error!=='aborted')hardStop();setL(false)}
  r.onend=()=>{if(sid!==sessionId)return;clearTimeout(endWatch);setL(false)}
  r.onresult=e=>{if(sid!==sessionId)return;let txt='';for(let i=e.resultIndex;i<e.results.length;i++){const r=e.results[i];if(r.isFinal)txt+=r[0].transcript}if(txt)processVoiceTranscript(txt.trim().toLowerCase());if(Array.from(e.results).some(r=>r.isFinal))hardStop()}
  return recognition=r
}
function startListening(){const r=initRecognition();if(!r){alert('Voice requires HTTPS and a supported browser.');return}try{r.start()}catch(_){ }}
function stopListening(){sessionId++;clearTimeout(endWatch);try{recognition&&recognition.abort()}catch(_){ }setL(false);recognition=null}
function toggleVoice(){if(!SR||!isSecure){alert('Voice requires HTTPS and a supported browser.');return}listening?stopListening():startListening()}
updateVoiceUI()
document.addEventListener('visibilitychange',()=>{if(document.hidden&&listening)stopListening()})
window.addEventListener('beforeunload',()=>{if(listening)stopListening()})
function processVoiceTranscript(txt){
  if(!txt)return;console.log('Voice command:',txt)
  const has=k=>txt.includes(k),any=a=>a.some(has)
  if(any(['foto','photo']))return takePhoto()
  if(any(['david','bowie','musik','music','tanz','dance']))return toggleMusic()
  if(any(['zentrier','mitte','center']))return centerHead()
  if(any(['kamera','kopf','schau','seh'])){if(has('links'))return stepPan(-14);if(has('rechts'))return stepPan(14);if(has('hoch')||has('oben'))return stepTilt(25);if(has('runter')||has('unten'))return stepTilt(-25)}
  if(any(['linie','line','autopilot'])&&any(['an','start','ein'])){stopClientDance();s.emit('cmd',{type:'line_follow',action:'enable'});return}
  if(any(['linie','line','autopilot'])&&any(['aus','stop','stopp'])){stopClientDance();s.emit('cmd',{type:'line_follow',action:'disable'});return}
  if(any(['kollisions','hindernis','crash'])&&any(['an','ein','start'])){s.emit('cmd',{type:'crash_avoid',action:'enable'});return}
  if(any(['kollisions','hindernis','crash'])&&any(['aus','stop','stopp'])){s.emit('cmd',{type:'crash_avoid',action:'disable'});return}
  if(any(['stop','stopp'])){stopClientDance();scheduleDrive(0,0,{force:true});updateJoystickVisual(0,0);return}
  if(has('links')){stopClientDance();scheduleDrive(60,-50);updateJoystickVisual(60,-50);return}
  if(has('rechts')){stopClientDance();scheduleDrive(60,50);updateJoystickVisual(60,50);return}
  if(txt.match(/r(ü|u|ue)ck/)){stopClientDance();scheduleDrive(-100,0);updateJoystickVisual(-80,0);return}
  if(has('vor')){stopClientDance();scheduleDrive(100,0);updateJoystickVisual(80,0);return}
  console.log('Befehl nicht erkannt: '+txt)
}
/* joystick */
const drivePad=$('drivePad'),driveNub=$('driveNub');let dragging=false,rect,cx,cy,radius
function resize(){rect=drivePad.getBoundingClientRect();cx=rect.left+rect.width/2;cy=rect.top+rect.height/2;radius=rect.width/2-10}
window.addEventListener('resize',resize);resize()
drivePad.addEventListener('pointerdown',e=>{stopClientDance();dragging=true;drivePad.setPointerCapture(e.pointerId)})
window.addEventListener('pointerup',()=>{if(dragging){dragging=false;driveNub.style.transform='';if(!clientDanceRunning)scheduleDrive(0,0,{force:true})}})
window.addEventListener('pointermove',e=>{if(!dragging)return;let dx=e.clientX-cx,dy=e.clientY-cy,dist=Math.hypot(dx,dy);if(dist>radius){dx=dx/dist*radius;dy=dy/dist*radius}driveNub.style.transform=`translate(${dx}px,${dy}px)`;let nx=dx/radius,ny=-dy/radius,thr=Math.round(ny*100),str=Math.round(nx*100);if(clientDanceRunning&&(thr||str))stopClientDance();scheduleDrive(thr,str)})
function updateJoystickVisual(thr,str){if(dragging)return;thr=clamp(thr,-100,100);str=clamp(str,-100,100);if(!thr&&!str){driveNub.style.transform='';return}const dx=str/100*radius,dy=-(thr/100)*radius;driveNub.style.transform=`translate(${dx}px,${dy}px)`}
let currentPan=~~($('pan')?.value)||0,currentTilt=~~($('tilt')?.value)||0
const PAN_STEP=2.5,TILT_STEP=0.75
function stepPan(dir){stopClientDance();currentPan=Math.max(CAM_PAN_MIN,Math.min(CAM_PAN_MAX,currentPan+dir*PAN_STEP));$('pan').value=currentPan;updateHeadInputs(currentPan,null);s.emit('cmd',{type:'head',pan:Math.round(currentPan)})}
function stepTilt(dir){stopClientDance();currentTilt=Math.max(CAM_TILT_MIN,Math.min(CAM_TILT_MAX,currentTilt+dir*TILT_STEP));$('tilt').value=currentTilt;updateHeadInputs(null,currentTilt);s.emit('cmd',{type:'head',tilt:Math.round(currentTilt)})}
window.addEventListener('keydown',e=>{
  const k=e.key,isArrow=k==='ArrowLeft'||k==='ArrowRight'||k==='ArrowUp'||k==='ArrowDown'
  if(e.repeat&&!isArrow)return;if(isArrow)e.preventDefault()
  if(k==='w'){stopClientDance();scheduleDrive(100,0);updateJoystickVisual(80,0)}
  if(k==='s'){stopClientDance();scheduleDrive(-100,0);updateJoystickVisual(-80,0)}
  if(k==='a'){stopClientDance();scheduleDrive(60,-50);updateJoystickVisual(60,-50)}
  if(k==='d'){stopClientDance();scheduleDrive(60,50);updateJoystickVisual(60,50)}
  if(k===' '){stopClientDance();scheduleDrive(0,0,{force:true});updateJoystickVisual(0,0)}
  if(k==='t')takePhoto()
  if(k==='m')toggleMusic()
  if(k==='c'){centerHead();currentPan=0;currentTilt=0}
  if(k==='f'||k==='F')toggleLineFollow()
  if(k==='g'||k==='G')toggleCrashAvoid()
  if(k==='ArrowLeft')stepPan(-1)
  if(k==='ArrowRight')stepPan(1)
  if(k==='ArrowUp')stepTilt(1)
  if(k==='ArrowDown')stepTilt(-1)
})
/* gamepad */
let gpIndex=null,prevButtons=[],_lastGpThr=0,_lastGpStr=0
function pollGP(){
  const gps=navigator.getGamepads?navigator.getGamepads():[],idx=gpIndex===null?gps.findIndex(g=>!!g):gpIndex
  if(gpIndex===null&&idx!==-1)gpIndex=idx
  const gp=gps[gpIndex]
  if(gp){
    const dead=0.12
    const ax0=Math.abs(gp.axes[0])>dead?gp.axes[0]:0
    const ax1=Math.abs(gp.axes[1])>dead?gp.axes[1]:0
    const thr=Math.round(-ax1*100),str=Math.round(ax0*100)
    if(clientDanceRunning){
      if(thr||str){stopClientDance();if(thr!==_lastGpThr||str!==_lastGpStr){updateJoystickVisual(thr,str);scheduleDrive(thr,str);_lastGpThr=thr;_lastGpStr=str}}
      else{if(_lastGpThr||_lastGpStr){updateJoystickVisual(0,0);scheduleDrive(0,0);_lastGpThr=0;_lastGpStr=0}else updateJoystickVisual(0,0)}
    }else if(thr!==_lastGpThr||str!==_lastGpStr){updateJoystickVisual(thr,str);scheduleDrive(thr,str);_lastGpThr=thr;_lastGpStr=str}
    const btns=gp.buttons||[],pressed=btns.map(b=>!!(b&&(b.pressed||(typeof b==='object'&&b.value>0.1))))
    if(pressed[1]&&!prevButtons[1])takePhoto()
    if(pressed[0]&&!prevButtons[0])toggleMusic()
    if(pressed[2]&&!prevButtons[2])toggleRecord()
    if(pressed[3]&&!prevButtons[3])togglePlayback()
    if(pressed[8]&&!prevButtons[8])toggleLineFollow()
    if(pressed[9]&&!prevButtons[9])toggleCrashAvoid()
    const lb=!!pressed[4],rb=!!pressed[5]
    if(lb&&!rb)stepPan(-1); else if(rb&&!lb)stepPan(1)
    const lt=(btns[6]&&typeof btns[6].value==='number')?btns[6].value:(pressed[6]?1:0)
    const rt=(btns[7]&&typeof btns[7].value==='number')?btns[7].value:(pressed[7]?1:0)
    if(rt>0.05&&lt<=0.05){stopClientDance();currentTilt=Math.min(CAM_TILT_MAX,currentTilt+Math.max(1,Math.round(TILT_STEP*rt*2)));updateHeadInputs(null,currentTilt);s.emit('cmd',{type:'head',tilt:Math.round(currentTilt)})}
    else if(lt>0.05&&rt<=0.05){stopClientDance();currentTilt=Math.max(CAM_TILT_MIN,currentTilt-Math.max(1,Math.round(TILT_STEP*lt*2)));updateHeadInputs(null,currentTilt);s.emit('cmd',{type:'head',tilt:Math.round(currentTilt)})}
    prevButtons=pressed
  }else{gpIndex=null;prevButtons=[];_lastGpThr=0;_lastGpStr=0}
  requestAnimationFrame(pollGP)
}
pollGP()
/* telemetry & inputs */
let lastTelemetryThr=0,lastTelemetryStr=0
s.on('input',m=>{
  try{
    if(!m)return
    if(m._origin==='auto_crash' || (m._origin && m._origin!==mySid && m._origin!=='playback' && m._origin!=='server' && m._origin!=='auto_crash')){
      if(clientDanceRunning||danceActive){stopClientDance({keepDrive:true});danceActive=false;updateMusicButton()}
    }
    const origin=m._origin
    if(m.throttle!==undefined||m.steer!==undefined){
      const thr=m.throttle|0||0,str=m.steer|0||0
      lastTelemetryThr=thr;lastTelemetryStr=str
      if(origin==='auto_crash'){
        const now=(typeof performance!=='undefined'&&performance.now)?performance.now():Date.now()
        const sentRecently=(typeof _lastTs==='number'&&(now-_lastTs)<300)||dragging||Math.abs(_lastGpThr)>DRIVE_EPS
        if(!sentRecently) updateJoystickVisual(0,0)
      }else updateJoystickVisual(thr,str)
    }
    if(m.pan!==undefined && !clientDanceRunning){currentPan=m.pan|0;updateHeadInputs(currentPan,null)}
    if(m.tilt!==undefined && !clientDanceRunning){currentTilt=m.tilt|0;updateHeadInputs(null,currentTilt)}
  }catch(e){console.warn('input handler error',e)}
})
s.on('state',st=>{try{if(!st)return;if(st.throttle!==undefined)lastTelemetryThr=st.throttle|0;if(st.steer!==undefined)lastTelemetryStr=st.steer|0;if(st.pan!==undefined){currentPan=st.pan|0;updateHeadInputs(currentPan,null,true)}if(st.tilt!==undefined){currentTilt=st.tilt|0;updateHeadInputs(null,currentTilt,true)}updateJoystickVisual(lastTelemetryThr,lastTelemetryStr)}catch(e){console.warn('state handler error',e)}})
s.on('auto_state',st=>{if(!st)return;const prevCA=_autoState.crash_avoid_enabled;_autoState=st;applyAutoButtons();if(_autoState.line_follow_enabled){stopClientDance();danceActive=false;updateMusicButton()}if(!prevCA&&_autoState.crash_avoid_enabled){if(forwardBlocked&&lastTelemetryThr>0) scheduleDrive(0,lastTelemetryStr,{force:true})}else if(prevCA&&!_autoState.crash_avoid_enabled){if(_lastRefusedForward&&_lastRefusedForward.thr>0){scheduleDrive(_lastRefusedForward.thr,_lastRefusedForward.str,{force:true});_lastRefusedForward=null}}})
s.on('playback_state',st=>{if(st?.playing){stopClientDance({keepDrive:true});danceActive=false;updateMusicButton()}})
/* record/playback UI */
let recState={recording:false,available:false},pbState={playing:false}
function toggleRecord(){s.emit('record',{action:recState.recording?'stop':'start'})}
function togglePlayback(){s.emit('playback',{action:pbState.playing?'stop':'play'})}
function updateRPUI(){const r=$('recordBtn'),p=$('playbackBtn');if(!r||!p)return;r.classList.toggle('listening',!!recState.recording),r.setAttribute('aria-pressed',!!recState.recording),p.classList.toggle('listening',!!pbState.playing),p.setAttribute('aria-pressed',!!pbState.playing),p.disabled=!recState.available&&!recState.recording&&!pbState.playing}
s.on('recorder_state',st=>{if(st)recState=st;updateRPUI()})
s.on('playback_state',st=>{if(st)pbState=st;updateRPUI()})
window.addEventListener('load',()=>{updateMusicButton();applyAutoButtons()})
function isDriveNeutral(){const thr=+(_p?.thr||0),str=+(_p?.str||0),pn=Math.abs(thr)<=DRIVE_EPS&&Math.abs(str)<=DRIVE_EPS,gp=Math.abs(_lastGpThr||0)<=DRIVE_EPS&&Math.abs(_lastGpStr||0)<=DRIVE_EPS,te=Math.abs(lastTelemetryThr||0)<=DRIVE_EPS&&Math.abs(lastTelemetryStr||0)<=DRIVE_EPS;return pn&&gp&&te&&!dragging}
</script>
</body></html>
"""

@app.route("/")
def index():
    return render_template_string(PAGE,
                                  CAM_PAN_MIN=CAM_PAN_MIN, CAM_PAN_MAX=CAM_PAN_MAX,
                                  CAM_TILT_MIN=CAM_TILT_MIN, CAM_TILT_MAX=CAM_TILT_MAX,
                                  pan=state.get("pan", 0), tilt=state.get("tilt", 0))

# ---------- Socket events ----------
@socketio.on("connect")
def _on_connect():
    try:
        connected_clients.add(request.sid)
    except Exception:
        pass

@socketio.on("hi")
def hi():
    sid = request.sid
    try:
        socketio.emit("state", {
            "pan": int(state.get("pan", 0)),
            "tilt": int(state.get("tilt", 0)),
            "throttle": int(state.get("throttle", 0)),
            "steer": int(state.get("steer", 0)),
        }, room=sid)
    except Exception:
        pass
    if battery_last["voltage"] is not None and battery_last["percent"] is not None:
        emit("battery_state", {"voltage": battery_last["voltage"], "percent": int(round(battery_last["percent"]))}, room=sid)
    emit_recorder_state()
    emit_playback_state()
    emit_music_state()
    try:
        emit("auto_state", {
            "line_follow_enabled": bool(auto_state.get("line_follow_enabled")),
            "crash_avoid_enabled": bool(auto_state.get("crash_avoid_enabled")),
        }, room=sid)
        emit("obstacle_state", {
            "blocked_forward": bool(obstacle_state.get("blocked_forward")),
            "distance_cm": obstacle_state.get("distance_cm"),
        }, room=sid)
    except Exception:
        pass

# ---------- Commands ----------
def stop_automation_if_user_input():
    if playback.is_playing():
        playback.stop("user input")
    if auto_state.get("line_follow_enabled"):
        set_line_follow_enabled(False, reason="user input")

@socketio.on("cmd")
def on_cmd(d):
    if not isinstance(d, dict):
        return
    t = d.get("type")
    if t in ("drive", "head", "music", "photo", "line_follow", "crash_avoid"):
        if t in ("drive", "head", "music", "photo"):
            stop_automation_if_user_input()

    if t in ("head", "music", "photo"):
        try:
            recorder.record_event(t, d)
        except Exception:
            pass

    if t == "motors":
        left, right = clamp(d.get("left", 0), -100, 100), clamp(d.get("right", 0), -100, 100)
        mot.set_target(left, right)
    elif t == "drive":
        thr, strv = clamp(d.get("throttle", 0), -100, 100), clamp(d.get("steer", 0), -100, 100)
        blocked = auto_state.get("crash_avoid_enabled") and obstacle_state.get("blocked_forward")
        global last_controller_sid
        if blocked and thr > 0:
            log.info("Refusing forward drive due to obstacle (thr=%s, steer=%s) from %s", thr, strv, request.sid)
            if last_controller_sid is None or last_controller_sid == request.sid:
                stop_motors_broadcast("forward blocked; stopping", origin="auto_crash", record=True)
            return
        set_steer_throttle(thr, strv)
        
        try:
            recorder.record_event("drive", {"throttle": thr, "steer": strv})
        except Exception:
            pass
        broadcast_input({"throttle": thr, "steer": strv, "_origin": request.sid})
        last_controller_sid = request.sid
    elif t == "head":
        pan, tilt = d.get("pan"), d.get("tilt")
        if pan is not None or tilt is not None:
            set_head(pan=pan, tilt=tilt)
            b = {}
            if pan is not None: b["pan"] = int(state["pan"])
            if tilt is not None: b["tilt"] = int(state["tilt"])
            b["_origin"] = request.sid
            broadcast_input(b)
    elif t == "photo":
        p = take_photo()
        if p:
            name = os.path.basename(p)
            url = url_for("photo_file", filename=name)
            emit("photo_result", {"path": url, "name": name, "error": None}, room=request.sid)
            socketio.emit("gallery_update")
        else:
            emit("photo_result", {"path": None, "name": None, "error": "no_image"}, room=request.sid)
    elif t == "music":
        action = d.get("action")
        song = d.get("song", "Life on Mars - 2015 Remaster - David Bowie.mp3")
        bpm = d.get("bpm", None)
        ok = music_control(action, song, volume=state.get("volume", 100))
        if action == "play" and ok:
            music_state.update({"playing": True, "song": song, "bpm": bpm, "since": time()})
        else:
            music_state.update({"playing": False, "song": None, "bpm": None, "since": 0.0})
        emit_music_state()
    elif t == "line_follow":
        action = (d.get("action") or "").lower()
        if action == "enable":
            set_line_follow_enabled(True)
        elif action == "disable":
            set_line_follow_enabled(False, reason="user")
        emit_auto_state()
    elif t == "crash_avoid":
        action = (d.get("action") or "").lower()
        if action == "enable":
            set_crash_avoid_enabled(True)
        elif action == "disable":
            set_crash_avoid_enabled(False)
        emit_auto_state()

@socketio.on("record")
def on_record(d):
    if not isinstance(d, dict):
        return
    action = d.get("action")
    if action == "start":
        if playback.is_playing():
            playback.stop("record-start")
            emit_playback_state()
        recorder.start()
    elif action == "stop":
        recorder.stop(save=True)
    emit_recorder_state()

@socketio.on("playback")
def on_playback(d):
    if not isinstance(d, dict):
        return
    action = d.get("action")
    if action == "play":
        if auto_state.get("line_follow_enabled"):
            set_line_follow_enabled(False, reason="playback")
        if recorder.recording:
            recorder.stop(save=True)
        data = recorder.load()
        if data:
            playback.play(data)
    elif action == "stop":
        playback.stop("user")
    emit_recorder_state(); emit_playback_state()

@socketio.on("disconnect")
def on_disconnect():
    global last_controller_sid
    if last_controller_sid == request.sid:
        stop_motors_broadcast("latest controlling client disconnected", origin="disconnected_client", record=True)
        last_controller_sid = None
    try:
        connected_clients.discard(request.sid)
    except Exception:
        pass

socketio.start_background_task(battery_monitor_loop)
socketio.start_background_task(obmon.loop)

# ---------- Video ----------
def stream_mjpeg(url):
    if not requests:
        return None
    try: 
        r = requests.get(url, stream=True, timeout=(3.0, 12.0))
        if r.status_code != 200:
            r.close()
            return None
        content_type = r.headers.get("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        def stream():
            try:
                for chunk in r.iter_content(chunk_size=16384):
                    if chunk:
                        yield chunk
            finally:
                r.close()
        return Response(stream_with_context(stream()), mimetype=content_type)
    except Exception as e:
        log.info("vilib MJPEG proxy not available: %s", e)
        return None

def gen_frames():
    if not cv2:
        yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n\r\n"
        return
    cap = cv2.VideoCapture(0)
    if not cap or not cap.isOpened():
        if cap: cap.release()
        yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n\r\n"
        return
    try:
        while True:
            ok, frm = cap.read()
            if not ok:
                break
            try:
                _, j = cv2.imencode(".jpg", frm)
                yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + j.tobytes() + b"\r\n"
            except Exception:
                pass
            sleep(0.03)
    finally:
        cap.release()

@app.route("/video_feed")
def video_feed():
    res = stream_mjpeg(VILIB_MJPG_URL)
    if res:
        return res
    return Response(gen_frames(), mimetype="multipart/x-mixed-replace; boundary=frame")

@app.route("/photos/<path:filename>")
def photo_file(filename):
    return send_from_directory(PHOTO_FOLDER, filename)

@app.route("/recent_photos")
def recent_photos():
    try:
        files = [f for f in os.listdir(PHOTO_FOLDER) if f.lower().endswith(('.jpg', '.jpeg', '.png', '.gif'))]
        files.sort(key=lambda fn: os.path.getmtime(os.path.join(PHOTO_FOLDER, fn)), reverse=True)
        files = files[:MAX_PHOTOS]
        return jsonify([{"name": f, "url": url_for("photo_file", filename=f)} for f in files])
    except Exception as e:
        log.warning("recent_photos failed: %s", e)
        return jsonify([])

def cleanup():
    log.info("Cleaning up hardware resources...")
    try: playback.stop("shutdown")
    except Exception: pass
    try: linef.stop("shutdown")
    except Exception: pass
    try: mot.shutdown()
    except Exception: pass
    try:
        if px and hasattr(px, "stop"):
            px.stop()
    except Exception: pass
    try:
        if Vilib and hasattr(Vilib, "camera_close"):
            Vilib.camera_close()
    except Exception: pass
    try: music_control("stop")
    except Exception: pass
    try:
        if redirect_server:
            log.info("Shutting down redirect server...")
            try: redirect_server.shutdown()
            except Exception: pass
    except Exception: pass
    log.info("Cleanup complete.")

def _signal_handler(signum, frame):
    log.info("Received signal %s, shutting down...", signum)
    try:
        cleanup()
    finally:
        os._exit(0)

for sig in (signal.SIGINT, signal.SIGTERM):
    try:
        signal.signal(sig, _signal_handler)
    except Exception:
        pass

if __name__ == "__main__":
    try:
        ssl_ctx = None
        if os.path.exists(TLS_CERT) and os.path.exists(TLS_KEY):
            ssl_ctx = (TLS_CERT, TLS_KEY)
            port = int(os.environ.get("PORT", "443"))#
            http_port = int(os.environ.get("PORT", "80"))
            log.info("Starting on https://0.0.0.0:%s (TLS enabled)", port)
            start_redirect_server(host="0.0.0.0", port=http_port)
        else:
            port = int(os.environ.get("PORT", "80"))
            log.info("Starting on http://0.0.0.0:%s", port)
        socketio.run(app, host="0.0.0.0", port=port, debug=False, use_reloader=False, allow_unsafe_werkzeug=True, ssl_context=ssl_ctx)
    finally:
        cleanup()