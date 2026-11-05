import csv
import json
import logging
import os
import threading
import time
from collections import deque
from datetime import datetime

import cv2
import numpy as np
from flask import Flask, Response, jsonify, request

os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;tcp"
logging.getLogger("werkzeug").setLevel(logging.ERROR)   # hide "GET /count" lines

# ---------------- settings ----------------
STREAM = "stream1"                # stream1 = HD, stream2 = SD
STREAMS = {"cam4": "stream2"}
STALE_SEC = 5                     # a frame older than this = camera treated as offline
SD_MODEL = "yolo11n_1280.onnx"
SD_IMG = 1280
CAMS = {                          # name: (IP, RTSP 아이디, 비밀번호) — 실제 값은 라즈베리파이 원본에만 (HANDOFF 18장)
    "cam1": ("<cam1 IP>", "camera", "RTSP비밀번호"),
    "cam2": ("<cam2 IP>", "camera2", "RTSP비밀번호"),
    "cam3": ("<cam3 IP>", "camera3", "RTSP비밀번호"),
    "cam4": ("<cam4 IP>", "camera4", "RTSP비밀번호"),
}
MODEL = "yolo11n_1280.onnx"
MODEL_BATCH = 1                   # must match the export batch size (3 for yolo11n_b3.onnx)
USE_NCNN = True                   # NCNN runs ~1.5x faster on the Pi; falls back to OpenCV DNN if it fails
NCNN_MODEL = "yolo11n_1280_ncnn_model"      # same weights as MODEL, exported with imgsz=1280
SD_NCNN_MODEL = "yolo11n_1280_ncnn_model"
NCNN_THREADS = 4
SHADOW_MODEL = "yolo26n_1280_ncnn_model"   # comparison model: runs on one camera per round when time is left, never shown (None = off)
SHADOW_ONNX = "yolo26n_1280.onnx"          # same weights for the OpenCV DNN fallback
SHADOW_CONF = 0.05                         # log low-score boxes too so thresholds can be compared afterwards
SHADOW_DIR = "shadow"                      # per-day JSONL: both models' people for the same frame (numbers only)

KIOSK_CAMS = {"cam4"}             # these cameras watch the kiosk queue, not seats
KIOSK_N = 3                       # people standing at the kiosks are ordering, not waiting
KIOSK_RATE = 4.0                  # people served per minute by all kiosks together (measure on site)
TBL_WIN = 30                      # seconds: a table is occupied when seen in >= TBL_MIN_HITS rounds within this window
TBL_MIN_HITS = 3
TBL_AWAY = 60                     # seconds without anyone -> away (person may come back)
TBL_EMPTY = 240                   # seconds without anyone -> empty
STATE_FILE = "tables_state.json"  # current table/kiosk state (sent to the server by push_counts.py)
TABLES_LOG = "tables_log.jsonl"   # one line per state change
TBL_COLOR = {"empty": (235, 200, 120), "occupied": (80, 220, 110), "away": (60, 170, 255)}   # BGR
IMG = 1280
CONF = 0.2                         # detection confidence threshold
CAM_CONF = {"cam1": 0.08}           # 카메라별 기준. 위에서 내려다봐 점수가 낮게 나오는 cam1만 낮춤
CONF_MIN = min([CONF] + list(CAM_CONF.values()))
NMS = 0.6
SMOOTH_SEC = 15                    # median of counts from the last N seconds (removes one-frame drops)
SMOOTH_PCT = 75                   # 50 = median, higher = counts flickering people more generously
OUT_FPS = 2                       # frames per second sent to the browser
ZONES_FILE = "zones.json"         # counting area per camera (set from the web page)
LOG_CSV = "people_log.csv"        # timestamp, smoothed count per camera, total, raw total
DET_DIR = "detections"            # per-day JSONL: every detected person's position (no images)
TABLES_FILE = "tables.json"       # table areas per camera (set from /tables page); the same id on several cameras = one table
SETUP_KEEPALIVE = 180             # /tables page open outside hours -> keep video (no inference) this long
TABLE_IDS = [f"T{i}" for i in range(1, 151)] + [f"S{i}" for i in range(1, 11)]   # T = 테이블 1개 + 의자 2개(자리 단위), S = 1인석

# ---- schedule (adjust after looking at the collected data) ----
OPEN_HOURS = [("08:00", "19:30")]               # 학생회관 식당 운영 시간: outside -> everything stops
PEAK_HOURS = [("11:00", "14:00"), ("17:00", "19:00")]
OPEN_DAYS = [0, 1, 2, 3, 4]                     # 0=Mon ... 6=Sun (weekdays only)
PEAK_INTERVAL = 3.0
NORMAL_INTERVAL = 5.0
CLOSED_CHECK = 30                               # seconds between schedule checks while closed
FORCE_MODE = None                               # "peak" / "normal" to ignore the schedule (demo), None = schedule
# ------------------------------------------


def _in_ranges(now, ranges):
    hm = now.strftime("%H:%M")
    return any(start <= hm < end for start, end in ranges)


def current_mode(now=None):
    """Returns "peak", "normal" or "closed"."""
    if FORCE_MODE:
        return FORCE_MODE
    now = now or datetime.now()
    if now.weekday() not in OPEN_DAYS or not _in_ranges(now, OPEN_HOURS):
        return "closed"
    return "peak" if _in_ranges(now, PEAK_HOURS) else "normal"


MODE_LABEL = {"peak": "피크 시간", "normal": "운영 시간", "closed": "운영 시간 아님"}


def load_zones():
    try:
        with open(ZONES_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


zones = load_zones()               # {"cam2": [[x, y], ...]} normalized 0~1, missing = whole frame
zones_lock = threading.Lock()


def load_tables():
    try:
        with open(TABLES_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


tables = load_tables()             # {"cam1": [{"id": "T3", "seats": 4, "partial": false, "pts": [[x, y], ...]}]}
tables_lock = threading.Lock()


def table_registry():
    """Tables as objects: one entry per id, joining every camera view of it.
    {"T3": {"seats": 4, "zone": "cam1", "cams": ["cam1", "cam3"]}}. zone = the first camera (by name) that sees
    the whole table, else the first that sees part of it; per-area numbers (구역) are grouped by it."""
    with tables_lock:
        views = [(cam, t) for cam in sorted(tables) if cam not in KIOSK_CAMS for t in tables[cam]]
    reg, full = {}, {}
    for cam, t in views:
        r = reg.setdefault(t["id"], {"seats": t["seats"], "zone": cam, "cams": []})
        r["seats"] = max(r["seats"], t["seats"])
        r["cams"].append(cam)
        if not t.get("partial") and t["id"] not in full:
            full[t["id"]] = cam
    for tid, cam in full.items():
        reg[tid]["zone"] = cam
    return reg
setup_until = [0.0]                # while the /tables page is open, video keeps streaming even when closed


def streaming_allowed():
    return current_mode() != "closed" or time.time() < setup_until[0]


class Camera:
    """Reads one RTSP stream in the background and keeps the newest frame and result."""

    def __init__(self, name, ip, user, pw):
        self.name = name
        self.url = f"rtsp://{user}:{pw}@{ip}:554/{STREAMS.get(name, STREAM)}"
        self.raw_t = 0.0
        self.raw = None
        self.view = None
        self.count = None                     # smoothed count (None = no data)
        self.seen = None                      # everyone detected in the frame this round
        self.history = deque()                # (time, raw count)
        self.lock = threading.Lock()
        threading.Thread(target=self._read_loop, daemon=True).start()

    def _read_loop(self):
        while True:
            if not streaming_allowed():                # closed: do not even receive video
                time.sleep(3 if setup_until[0] else CLOSED_CHECK)
                continue
            cap = cv2.VideoCapture(self.url, cv2.CAP_FFMPEG)
            if not cap.isOpened():
                print(f"[{self.name}] connect failed, retrying...")
                time.sleep(3)
                continue
            print(f"[{self.name}] connected")
            while True:
                if not streaming_allowed():
                    print(f"[{self.name}] closed hours, stream stopped")
                    break
                ok, frame = cap.read()
                if not ok:
                    print(f"[{self.name}] frame lost, reconnecting...")
                    break
                with self.lock:
                    self.raw = frame
                    self.raw_t = time.time()
            with self.lock:
                self.raw = None
            cap.release()
            time.sleep(2)

    def smooth(self, raw, now):
        self.history.append((now, raw))
        while self.history and now - self.history[0][0] > SMOOTH_SEC:
            self.history.popleft()
        return int(round(float(np.percentile([c for _, c in self.history], SMOOTH_PCT))))

    def latest_raw(self):
        with self.lock:
            if self.raw is None or time.time() - self.raw_t > STALE_SEC:
                return None                   # frozen stream: skip instead of counting an old frame
            return self.raw.copy()

    def set_result(self, view, count):
        with self.lock:
            self.view, self.count = view, count

    def jpeg(self):
        with self.lock:
            img = self.view if self.view is not None else self.raw
            if img is None:
                return None
            ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 70])
        return buf.tobytes() if ok else None


def parse_output(out, frame_shape, img=None, conf=None):
    """out: (84, 8400) for one image -> list of [x, y, w, h, score] person boxes."""
    img = img or IMG
    conf = CONF_MIN if conf is None else conf
    h, w = frame_shape[:2]
    out = out.T
    scores = out[:, 4]                # class 0 = person
    keep = scores > conf
    out, scores = out[keep], scores[keep]
    sx, sy = w / img, h / img
    boxes = [[int((cx - bw / 2) * sx), int((cy - bh / 2) * sy), int(bw * sx), int(bh * sy)]
             for cx, cy, bw, bh in out[:, :4]]
    idx = cv2.dnn.NMSBoxes(boxes, scores.tolist(), conf, NMS)
    return [boxes[i] + [float(scores[i])] for i in np.array(idx).flatten()] if len(idx) else []


class NcnnNet:
    """Same setInput()/forward() interface as cv2.dnn so the rest of the code stays unchanged.
    If NCNN fails while running, it switches to OpenCV DNN with the ONNX file and keeps going."""

    def __init__(self, ncnn_dir, onnx_path):
        import ncnn
        self.ncnn, self.onnx_path, self.fallback, self.blob = ncnn, onnx_path, None, None
        self.net = ncnn.Net()
        self.net.opt.use_vulkan_compute = False
        self.net.opt.num_threads = NCNN_THREADS
        if self.net.load_param(os.path.join(ncnn_dir, "model.ncnn.param")) or \
           self.net.load_model(os.path.join(ncnn_dir, "model.ncnn.bin")):
            raise RuntimeError(f"cannot load {ncnn_dir}")
        self.in_name = self.net.input_names()[0]
        self.out_name = sorted(self.net.output_names())[0]
        self.backend = "NCNN"

    def setInput(self, blob):
        self.blob = blob
        if self.fallback is not None:
            self.fallback.setInput(blob)

    def forward(self):
        if self.fallback is not None:
            return self.fallback.forward()
        try:
            outs = []
            for img in self.blob:
                ex = self.net.create_extractor()
                ex.input(self.in_name, self.ncnn.Mat(np.ascontiguousarray(img)))
                ret, out = ex.extract(self.out_name)
                if ret != 0:
                    raise RuntimeError(f"extract failed ({ret})")
                outs.append(np.array(out))
            return np.stack(outs)
        except Exception as e:
            print(f"[model] NCNN failed ({e}) -> switching to OpenCV DNN ({self.onnx_path})")
            self.fallback = cv2.dnn.readNetFromONNX(self.onnx_path)
            self.backend = "OpenCV DNN (fallback)"
            self.fallback.setInput(self.blob)
            return self.fallback.forward()


def load_net(onnx_path, ncnn_dir):
    if USE_NCNN:
        try:
            net = NcnnNet(ncnn_dir, onnx_path)
            print(f"[model] NCNN: {ncnn_dir}")
            return net
        except Exception as e:
            print(f"[model] NCNN unavailable ({e}) -> OpenCV DNN: {onnx_path}")
    print(f"[model] OpenCV DNN: {onnx_path}")
    return cv2.dnn.readNetFromONNX(onnx_path)


def detect_batch(net, frames, img=None, conf=None):
    img = img or IMG
    results = []
    for i in range(0, len(frames), MODEL_BATCH):
        chunk = frames[i:i + MODEL_BATCH]
        pad = [np.zeros((img, img, 3), np.uint8)] * (MODEL_BATCH - len(chunk))
        blob = cv2.dnn.blobFromImages(chunk + pad, 1 / 255.0, (img, img), swapRB=True, crop=False)
        net.setInput(blob)
        out = net.forward()
        for j, frame in enumerate(chunk):
            results.append(parse_output(out[j], frame.shape, img, conf))
    return results


def count_in_zone(name, frame, boxes):
    """Counts only people whose feet (bottom-center of the box) are inside this camera's zone."""
    h, w = frame.shape[:2]
    with zones_lock:
        poly = zones.get(name)
    pts = None
    if poly and len(poly) >= 3:
        pts = np.array([[int(x * w), int(y * h)] for x, y in poly], np.int32)
        overlay = frame.copy()
        cv2.fillPoly(overlay, [pts], (0, 255, 255))
        cv2.addWeighted(overlay, 0.15, frame, 0.85, 0, frame)
        cv2.polylines(frame, [pts], True, (0, 255, 255), 2)

    n, recs = 0, []
    for x, y, bw, bh, score in boxes:
        foot = (x + bw / 2, y + bh)
        inside = pts is None or cv2.pointPolygonTest(pts, foot, False) >= 0
        color = (0, 255, 0) if inside else (128, 128, 128)   # gray = seen but not counted here
        cv2.rectangle(frame, (x, y), (x + bw, y + bh), color, 2)
        cv2.circle(frame, (int(foot[0]), int(foot[1])), 4, color, -1)
        n += inside
        # [foot x, foot y, box x, box y, box w, box h, confidence, counted in zone]
        recs.append([int(foot[0]), int(foot[1]), x, y, bw, bh, round(score, 2), int(inside)])
    return n, recs


def log_detections(now, mode, interval, per_cam):
    """Appends one line per inference round: where each person was (numbers only, no images)."""
    os.makedirs(DET_DIR, exist_ok=True)
    path = os.path.join(DET_DIR, now.strftime("detections_%Y-%m-%d.jsonl"))
    rec = {"t": now.strftime("%Y-%m-%d %H:%M:%S"), "mode": mode, "interval": interval, "cams": per_cam}
    with open(path, "a") as f:
        f.write(json.dumps(rec, separators=(",", ":")) + "\n")


def log_row(ts, counts, total, total_raw, mode, interval):
    new = not os.path.exists(LOG_CSV)
    with open(LOG_CSV, "a", newline="") as f:
        wr = csv.writer(f)
        if new:
            wr.writerow(["timestamp"] + list(cams) + ["total", "total_raw", "mode", "interval"])
        wr.writerow([ts] + [counts.get(n, "") for n in cams] + [total, total_raw, mode, interval])


state = {"mode": None, "interval": None, "last": None, "temp": None, "throttled": None, "snapshot": None}


def read_temp():
    """CPU temperature (℃) and throttled flags; never raises."""
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            state["temp"] = round(int(f.read().strip()) / 1000, 1)
    except Exception:
        state["temp"] = None
    try:
        import subprocess
        out = subprocess.run(["vcgencmd", "get_throttled"], capture_output=True, text=True, timeout=2).stdout
        state["throttled"] = out.strip().split("=")[-1] if "=" in out else None
    except Exception:
        state["throttled"] = None


class TableTracker:
    # Per-table state machine: empty -> occupied -> away -> empty, driven by detections inside the table area.

    def __init__(self):
        self.hits, self.last_seen, self.state, self.people, self.since = {}, {}, {}, {}, {}

    def reset(self):
        self.__init__()

    def update(self, now_t, per_cam, online, ts=None):
        # per_cam = {cam: {table id: people}}. A table seen by several cameras takes the largest count,
        # and keeps being judged while at least one of its cameras is online.
        reg = table_registry()
        merged = {}
        for cam, d in per_cam.items():
            for tid, n in d.items():
                merged[tid] = max(merged.get(tid, 0), n)
        out, changes = {}, {}
        for tid, info in reg.items():
            st = self.state.get(tid, "empty")
            self.since.setdefault(tid, ts)
            base = {"seats": info["seats"], "zone": info["zone"], "cams": info["cams"], "since": self.since[tid]}
            if not (set(info["cams"]) & online):     # every camera for this table is offline: state unknown
                out[tid] = dict(base, st=st, n=self.people.get(tid, 0), unknown=True)
                continue
            n = merged.get(tid, 0)
            dq = self.hits.setdefault(tid, deque())
            if n > 0:
                dq.append((now_t, n))
            while dq and now_t - dq[0][0] > TBL_WIN:
                dq.popleft()
            if n > 0 and len(dq) >= 2:   # a single stray hit must not refresh the away/empty timer
                self.last_seen[tid] = now_t
            since = now_t - self.last_seen.get(tid, -1e9)
            new = st
            if st == "empty" and len(dq) >= TBL_MIN_HITS:
                new = "occupied"
            elif st == "occupied" and since >= TBL_AWAY:
                new = "away"
            elif st == "away":
                if len(dq) >= 2:
                    new = "occupied"
                elif since >= TBL_EMPTY:
                    new = "empty"
            if new != st:
                changes[tid] = new
                self.since[tid] = base["since"] = ts
            self.state[tid] = new
            people = 0
            if new == "occupied" and dq:
                vals = sorted(v for _, v in dq)
                people = min(info["seats"], max(1, vals[int(0.75 * (len(vals) - 1))]))
            self.people[tid] = people
            out[tid] = dict(base, st=new, n=people)
        for tid in list(self.state):
            if tid not in reg:
                for d in (self.state, self.hits, self.people, self.last_seen, self.since):
                    d.pop(tid, None)
        return out, changes


tracker = TableTracker()


def count_tables(name, frame, boxes):
    # People at each registered table area of this camera. Each person counts for one table only:
    # the one their feet are in (the deepest one if several overlap), else the one their box centre is in.
    # Counting a person in every area they touch made neighbouring tables look used (replay of 11/4: 11.8%
    # of people touched 2+ areas). Also draws the areas on the frame, coloured by their current state.
    with tables_lock:
        tlist = list(tables.get(name, []))
    if not tlist:
        return {}
    h, w = frame.shape[:2]
    polys = [(t, np.array([[int(x * w), int(y * h)] for x, y in t["pts"]], np.int32)) for t in tlist]
    out = {t["id"]: 0 for t in tlist}
    for x, y, bw, bh, score in boxes:
        foot = (float(x + bw / 2), float(y + bh))
        centre = (float(x + bw / 2), float(y + bh / 2))
        best = None
        for t, pts in polys:
            df = cv2.pointPolygonTest(pts, foot, True)
            dc = cv2.pointPolygonTest(pts, centre, True)
            if df < 0 and dc < 0:
                continue
            key = (0, -df) if df >= 0 else (1, -dc)       # feet inside first, then the deepest inside
            if best is None or key < best[0]:
                best = (key, t["id"])
        if best:
            out[best[1]] += 1
    for t, pts in polys:
        st = tracker.state.get(t["id"], "empty")
        color = TBL_COLOR[st]
        if st == "occupied":
            overlay = frame.copy()
            cv2.fillPoly(overlay, [pts], color)
            cv2.addWeighted(overlay, 0.18, frame, 0.82, 0, frame)
        cv2.polylines(frame, [pts], True, color, 2)
        cx, cy = int(pts[:, 0].mean()), int(pts[:, 1].mean())
        if st == "occupied":
            label = f"{t['id']} {tracker.people.get(t['id'], 0)}/{t['seats']}"
        elif st == "away":
            label = f"{t['id']} away"
        else:
            label = t["id"]
        (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.6, 2)
        cv2.rectangle(frame, (cx - tw // 2 - 4, cy - th - 6), (cx + tw // 2 + 4, cy + 4), (0, 0, 0), -1)
        cv2.putText(frame, label, (cx - tw // 2, cy), cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2)
    return out


def build_state(ts, mode, counts, tstate):
    seat_cams = {n: c for n, c in counts.items() if n not in KIOSK_CAMS}
    people = sum(v for v in seat_cams.values() if v is not None) if seat_cams else None
    kiosk = None
    for kc in KIOSK_CAMS:
        if kc in cams:
            k = counts.get(kc)
            q = max(0, k - KIOSK_N) if k is not None else None
            kiosk = {"cam": kc, "people": k, "n": KIOSK_N, "rate": KIOSK_RATE, "queue": q,
                     "wait_min": (round(q / KIOSK_RATE, 1) if q is not None else None)}
            break
    summ = zones_sum = None
    if tstate:
        # Tables whose cameras are all offline are left out of the totals (their state is unknown), so seats and
        # people both cover only the part of the room that is being watched right now.
        known = [v for v in tstate.values() if not v.get("unknown")]
        occ = [v for v in known if v["st"] == "occupied"]
        away = [v for v in known if v["st"] == "away"]
        emp = [v for v in known if v["st"] == "empty"]
        summ = {"total": len(known), "occupied": len(occ), "away": len(away), "empty": len(emp),
                "seats": sum(v["seats"] for v in known),
                "free_seats": sum(v["seats"] for v in emp),
                "people": sum(v["n"] for v in occ),
                "unknown": len(tstate) - len(known),
                "unknown_seats": sum(v["seats"] for v in tstate.values() if v.get("unknown"))}
        zones_sum = {}                                # per area (구역 = table's main camera)
        for v in tstate.values():
            z = zones_sum.setdefault(v["zone"], {"tables": 0, "seats": 0, "free_seats": 0, "occupied": 0, "away": 0,
                                                 "empty": 0, "unknown_seats": 0, "people": seat_cams.get(v["zone"])})
            if v.get("unknown"):
                z["unknown_seats"] += v["seats"]
                continue
            z["tables"] += 1
            z["seats"] += v["seats"]
            z[v["st"]] += 1
            if v["st"] == "empty":
                z["free_seats"] += v["seats"]
    return {"t": ts, "mode": mode, "seating": {"people": people, "cams": seat_cams},
            "kiosk": kiosk, "tables": tstate, "summary": summ, "zones": zones_sum}


def write_state(obj):
    state["snapshot"] = obj
    try:
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(obj, f, ensure_ascii=False)
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        print(f"[state] write failed: {e}")


shadow = {"net": None, "next": 0, "cost": 0.6, "failed": False}


def zone_flags(name, shape, boxes):
    """1 if the feet of each box are inside this camera's counting zone (same rule as count_in_zone, no drawing)."""
    h, w = shape[:2]
    with zones_lock:
        poly = zones.get(name)
    if not poly or len(poly) < 3:
        return [1] * len(boxes)
    pts = np.array([[int(x * w), int(y * h)] for x, y in poly], np.int32)
    return [int(cv2.pointPolygonTest(pts, (float(x + bw / 2), float(y + bh)), False) >= 0) for x, y, bw, bh, _ in boxes]


def shadow_round(pick, main_boxes, ts, t0, interval):
    """Runs SHADOW_MODEL on one camera's untouched frame when this round still has time left, and logs both
    models' people for that same frame as [foot x, foot y, score, in zone]. Comparison only: nothing shown uses it."""
    if main_boxes is None or time.time() - t0 + shadow["cost"] > interval - 0.1:
        return
    name, frame = pick
    try:
        if shadow["net"] is None:
            shadow["net"] = load_net(SHADOW_ONNX, SHADOW_MODEL)
        t1 = time.time()
        boxes = detect_batch(shadow["net"], [frame], IMG, SHADOW_CONF)[0]
        shadow["cost"] = time.time() - t1
    except Exception as e:
        print(f"[shadow] turned off: {e}")
        shadow["failed"] = True
        return

    def recs(bs):
        return [[int(x + bw / 2), int(y + bh), round(s, 3), z] for (x, y, bw, bh, s), z in zip(bs, zone_flags(name, frame.shape, bs))]
    rec = {"t": ts, "cam": name, "th": CAM_CONF.get(name, CONF), "ms": round(shadow["cost"] * 1000),
           "main": recs(main_boxes), "shadow": recs(boxes)}
    try:
        os.makedirs(SHADOW_DIR, exist_ok=True)
        with open(os.path.join(SHADOW_DIR, f"shadow_{ts[:10]}.jsonl"), "a") as f:
            f.write(json.dumps(rec, separators=(",", ":")) + "\n")
    except Exception as e:
        print(f"[shadow] log failed: {e}")


def inference_loop():
    net = load_net(MODEL, NCNN_MODEL)
    sd_cams = {n for n, st in STREAMS.items() if st == "stream2"}
    net_sd = load_net(SD_MODEL, SD_NCNN_MODEL) if sd_cams and SD_MODEL != MODEL else None
    print("model loaded" + (f" (+ SD model for {sorted(sd_cams)})" if net_sd else ""))
    names = list(cams)
    while True:
        t0 = time.time()
        mode = current_mode()
        if mode != state["mode"]:
            print(f"[schedule] {MODE_LABEL[mode]} ({mode})")
        state["mode"] = mode

        if mode == "closed":
            state["interval"] = None
            state["last"] = None
            read_temp()
            tracker.reset()
            write_state({"t": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "mode": "closed",
                         "seating": None, "kiosk": None, "tables": None, "summary": None})
            for n in names:
                cams[n].history.clear()
                cams[n].set_result(None, None)
            time.sleep(CLOSED_CHECK)
            continue

        interval = PEAK_INTERVAL if mode == "peak" else NORMAL_INTERVAL
        state["interval"] = interval
        read_temp()
        snap = [(n, cams[n].latest_raw()) for n in names]
        ready = [(n, f) for n, f in snap if f is not None]
        pick = None                                   # comparison model: next camera in turn, before anything is drawn on it
        if ready and SHADOW_MODEL and not shadow["failed"]:
            n_s, f_s = ready[shadow["next"] % len(ready)]
            shadow["next"] += 1
            pick = (n_s, f_s.copy())
        if ready:
            if net_sd is None:
                results = detect_batch(net, [f for _, f in ready])
            else:
                hd = [i for i, (n, _) in enumerate(ready) if n not in sd_cams]
                sd = [i for i, (n, _) in enumerate(ready) if n in sd_cams]
                results = [None] * len(ready)
                for idxs, model, size in ((hd, net, IMG), (sd, net_sd, SD_IMG)):
                    if idxs:
                        for i, r in zip(idxs, detect_batch(model, [ready[i][1] for i in idxs], size)):
                            results[i] = r
            counts, raw_total, per_cam, tbl_counts = {}, 0, {}, {}
            main_boxes = {n: boxes for (n, _), boxes in zip(ready, results)}
            for (n, frame), boxes in zip(ready, results):
                th = CAM_CONF.get(n, CONF)
                boxes = [b for b in boxes if b[4] > th]
                raw, recs = count_in_zone(n, frame, boxes)
                cams[n].seen = len(recs)
                if n not in KIOSK_CAMS:
                    tbl_counts[n] = count_tables(n, frame, boxes)
                fh, fw = frame.shape[:2]
                per_cam[n] = {"w": fw, "h": fh, "p": recs}
                cam = cams[n]
                smooth = cam.smooth(raw, t0)
                cv2.putText(frame, f"People: {smooth}", (15, 45),
                            cv2.FONT_HERSHEY_SIMPLEX, 1.3, (0, 0, 255), 3)
                cam.set_result(frame, smooth)
                counts[n] = smooth
                raw_total += raw
            for n, f in snap:
                if f is None:
                    cams[n].history.clear()
                    cams[n].set_result(None, None)
                    cams[n].seen = None
                    per_cam[n] = None                   # camera offline this round
            total = sum(counts.values())
            now = datetime.now()
            ts = now.strftime("%Y-%m-%d %H:%M:%S")
            log_row(ts, counts, total, raw_total, mode, interval)
            log_detections(now, mode, interval, per_cam)
            state["last"] = {"t": ts, "cams": per_cam, "tables": tbl_counts}
            try:
                tstate, changes = tracker.update(t0, tbl_counts, {n for n, _ in ready}, ts)
                if changes:
                    with open(TABLES_LOG, "a") as f:
                        f.write(json.dumps({"t": ts, "changes": changes}) + "\n")
                write_state(build_state(ts, mode, counts, tstate))
            except Exception as e:
                print(f"[tables] {e}")
            took = time.time() - t0
            if pick:
                shadow_round(pick, main_boxes.get(pick[0]), ts, t0, interval)
            print(f"[{ts}] {mode} {counts} total={total} (raw {raw_total}, {took:.2f}s)")
        time.sleep(max(0, interval - (time.time() - t0)))


cams = {name: Camera(name, *info) for name, info in CAMS.items()}
threading.Thread(target=inference_loop, daemon=True).start()
app = Flask(__name__)

PAGE = """<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><title>학생회관 1층 식당 · 실시간 인원 감지</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<script>try{if(new URLSearchParams(location.search).get("dark")==="1"||localStorage.getItem("theme")==="dark")document.documentElement.dataset.dark="1";}catch(e){}</script>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/gh/orioncactus/pretendard@v1.3.9/dist/web/static/pretendard.min.css">
<style>
/* patch_admin_theme v1 — 학생용 혼잡도 웹과 같은 색·글자·간격 규칙 (밝은/어두운 화면) */
:root{--ground:#f6f7f9;--surface:#fff;--raised:#f7f8fa;--active:#eef0f3;--border:#e6e8eb;--rule:#eef0f2;--control:#d1d5db;
--text:#111418;--muted:#5f6368;--faint:#6b6f76;--data:#2f6db5;--data-tint:rgba(47,109,181,.12);
--ok:#247b50;--ok-fill:#2f9e63;--ok-bg:#e4f4ea;--mid:#92400e;--mid-bg:#fdf3e1;--warn:var(--mid);--busy:#c4242a;--busy-bg:#fdeaea;--on-data:#fff;
--r-card:8px;--r-ctl:6px;--ease-out:cubic-bezier(.23,1,.32,1)}
html[data-dark]{color-scheme:dark;--ground:#0a0a0a;--surface:#141414;--raised:#1b1b1b;--active:#262626;--border:#262626;--rule:#202020;--control:#464646;
--text:#f2f2f2;--muted:#b0b0b0;--faint:#8f8f8f;--data:#5b9bea;--data-tint:rgba(91,155,234,.16);
--ok:#4cc584;--ok-fill:#3fc07a;--ok-bg:#15291c;--mid:#f0b452;--mid-bg:#2b2213;--busy:#ef6a7f;--busy-bg:#2c161a;--on-data:#0b0b0b}
*{box-sizing:border-box}
[hidden]{display:none!important}
body{margin:0;background:var(--ground);color:var(--text);font-family:Pretendard,-apple-system,"Apple SD Gothic Neo","Malgun Gothic",sans-serif;font-size:14px;line-height:20px;-webkit-font-smoothing:antialiased}
.topbar{height:56px;background:var(--surface);border-bottom:1px solid var(--border)}
.topbar .in{max-width:1600px;height:100%;margin:0 auto;padding:0 24px;display:flex;align-items:center;justify-content:space-between;gap:16px}
.mark{display:flex;align-items:center;gap:10px;font-size:16px;font-weight:700;color:var(--text);text-decoration:none;white-space:nowrap}
.mark svg{color:var(--data);flex:none}
.mark span{font-weight:400;color:var(--muted);font-size:14px;margin-left:6px}
.top-right{display:flex;align-items:center;gap:8px}
.pill{height:28px;display:inline-flex;align-items:center;padding:0 12px;border:1px solid var(--border);border-radius:999px;background:var(--raised);color:var(--muted);font-size:13px;white-space:nowrap;font-variant-numeric:tabular-nums}
.pill:empty{display:none}
button{height:32px;padding:0 12px;border:1px solid var(--control);border-radius:var(--r-ctl);background:var(--surface);color:var(--text);cursor:pointer;
  font:inherit;font-weight:600;display:inline-flex;align-items:center;gap:6px;transition:transform 160ms var(--ease-out),background-color 150ms ease}
button:hover{background:var(--raised)}
button:active{transform:scale(.97)}
button:focus-visible{outline:2px solid var(--data);outline-offset:2px}
.btn-icon{width:32px;padding:0;justify-content:center;color:var(--muted)}
#themeBtn{font-weight:500;color:var(--muted)}
#themeBtn .sun,#themeBtn .lbl-light{display:none}
html[data-dark] #themeBtn .sun,html[data-dark] #themeBtn .lbl-light{display:inline}
html[data-dark] #themeBtn .moon,html[data-dark] #themeBtn .lbl-dark{display:none}
.wrap{max-width:1600px;margin:0 auto;padding:24px 24px 32px}
h1{margin:0;font-size:24px;line-height:32px;font-weight:700;letter-spacing:-.01em}
a{color:var(--data)}
.title-row{margin-bottom:20px}
.title-row p{margin:4px 0 0;color:var(--muted)}
.summary{display:grid;grid-template-columns:repeat(4,1fr);background:var(--surface);border:1px solid var(--border);border-radius:var(--r-card);margin-bottom:16px;overflow:hidden}
.summary .card{padding:20px 24px 22px;min-width:0}
.summary .card+.card{border-left:1px solid var(--border)}
.card small{display:block;color:var(--muted);font-size:14px;line-height:20px}
.card b{display:block;font-size:24px;font-weight:600;letter-spacing:-.01em;line-height:32px;margin:4px 0 2px;font-variant-numeric:tabular-nums}
.card span{font-size:12px;line-height:16px;color:var(--muted)}
.card.ok b{color:var(--ok)}.card.warn b{color:var(--mid)}.card.busy b{color:var(--busy)}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:16px}
.cam{background:var(--surface);border:1px solid var(--border);border-radius:var(--r-card);padding:16px;min-width:0}
.camhead{display:flex;align-items:center;gap:10px;margin-bottom:12px}
.camhead b{font-size:16px;font-weight:600}
.role{background:var(--data-tint);color:var(--data);border-radius:999px;padding:2px 10px;font-size:12px;font-weight:600}
.role.kiosk{background:var(--mid-bg);color:var(--mid)}
.cnt{margin-left:auto;color:var(--muted);font-size:13px;font-variant-numeric:tabular-nums}
.cnt b{color:var(--text);font-size:inherit;font-weight:600}
.box{position:relative;width:100%}
.box img{width:100%;aspect-ratio:16/9;object-fit:fill;display:block;border-radius:6px;background:#000}
.box canvas{position:absolute;left:0;top:0;width:100%;height:100%;pointer-events:none}
body.edit .box canvas{pointer-events:auto;cursor:crosshair}
.tools{display:none;margin-top:12px}
body.edit .tools{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.msg{font-size:13px;color:var(--ok);margin-left:4px}
footer{display:flex;align-items:center;gap:16px;margin-top:20px;color:var(--muted);font-size:12px;line-height:16px}
footer a{text-decoration:none;font-weight:600;white-space:nowrap}
footer a.tables{display:none}
body.edit footer a.tables{display:inline}
footer .sp{flex:1}
#editbtn{color:var(--muted)}
body.edit #editbtn{border-color:var(--data);color:var(--data);background:var(--data-tint)}
@media (max-width:1100px){.summary{grid-template-columns:repeat(2,1fr)}.summary .card:nth-child(3){border-left:0}.summary .card:nth-child(n+3){border-top:1px solid var(--border)}
  .grid{grid-template-columns:1fr}.mark span{display:none}footer{flex-wrap:wrap}}
</style></head><body>
<div class="topbar"><div class="in">
  <a class="mark" href="/"><svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 12h18a9 9 0 0 1-18 0Z"/><path d="M9.5 8.5c0-1.6 1.2-1.6 1.2-3.3M13.8 8.5c0-1.6 1.2-1.6 1.2-3.3"/></svg>밥먹을래말래<span>관리 화면 · 실시간 인원 감지</span></a>
  <div class="top-right"><span class="pill" id="mode">-</span><span class="pill" id="temp"></span><span class="pill" id="clock"></span>
    <button id="themeBtn" type="button" aria-label="어두운 화면으로 전환" title="밝은·어두운 화면 전환">
      <svg class="moon" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8Z"/></svg>
      <svg class="sun" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>
      <span class="lbl-dark">다크 모드</span><span class="lbl-light">라이트 모드</span>
    </button></div>
</div></div>
<div class="wrap">
<div class="title-row">
  <h1>학생회관 1층 식당 · 카메라 4대</h1>
  <p>영상은 저장하지 않고 사람 수만 기록해요 · 초록 박스는 집계, 회색 박스는 제외</p>
</div>
<section class="summary">
  <div class="card"><small>좌석 구역 인원</small><b id="s-people">-</b><span id="s-people-sub">좌석 카메라 영역 안</span></div>
  <div class="card" id="c-free"><small>완전히 빈 자리</small><b id="s-free">-</b><span id="s-free-sub">자리 등록 전</span></div>
  <div class="card"><small>사용 중인 자리</small><b id="s-occ">-</b><span id="s-occ-sub"></span></div>
  <div class="card" id="c-kiosk"><small>키오스크 주문 대기</small><b id="s-kq">-</b><span id="s-kq-sub">키오스크 카메라 없음</span></div>
</section>
<section class="grid">__CAMS__</section>
<footer>
  <span>구역 설정: [설정 모드]를 켠 뒤 영상 위를 클릭해 꼭짓점 3~6개를 찍고 [구역 저장] · 발 위치가 노란 영역 안인 사람만 셉니다</span>
  <span class="sp"></span>
  <a href="http://<맥미니 Tailscale IP>:8000" target="_blank" rel="noopener">학생용 혼잡도 화면 →</a>
  <a class="tables" href="/tables">테이블 영역 설정 →</a>
  <button id="editbtn" type="button" onclick="toggleEdit()">설정 모드</button>
</footer>
</div>
<script>
const pts = {};
function toggleEdit(){ document.body.classList.toggle('edit'); }
if (location.search.includes('edit=1')) document.body.classList.add('edit');
function draw(name){
  const c = document.getElementById('cv-' + name), ctx = c.getContext('2d');
  c.width = c.clientWidth; c.height = c.clientHeight;
  ctx.clearRect(0, 0, c.width, c.height);
  const p = pts[name] || [];
  if (!p.length) return;
  ctx.strokeStyle = '#ff0'; ctx.fillStyle = 'rgba(255,255,0,0.15)'; ctx.lineWidth = 2;
  ctx.beginPath();
  p.forEach(([x, y], i) => { const X = x * c.width, Y = y * c.height; i ? ctx.lineTo(X, Y) : ctx.moveTo(X, Y); });
  if (p.length >= 3) { ctx.closePath(); ctx.fill(); }
  ctx.stroke();
  ctx.fillStyle = '#ff0'; ctx.font = 'bold 14px sans-serif';
  p.forEach(([x, y], i) => {
    ctx.beginPath(); ctx.arc(x * c.width, y * c.height, 5, 0, 7); ctx.fill();
    ctx.fillText(String(i + 1), x * c.width + 8, y * c.height - 8);
  });
}
const snapEdge = v => v < 0.02 ? 0 : v > 0.98 ? 1 : v;
function addPoint(e, name){
  if (!document.body.classList.contains('edit')) return;
  const p = pts[name] = pts[name] || [];
  if (p.length >= 6) { msg(name, '점은 6개까지예요. [구역 저장] 또는 [점 취소]'); return; }
  const r = e.target.getBoundingClientRect();
  p.push([snapEdge((e.clientX - r.left) / r.width), snapEdge((e.clientY - r.top) / r.height)]);
  draw(name);
  msg(name, p.length >= 3 ? p.length + '개 - [구역 저장] 가능 (최대 6개)' : p.length + '개 찍음 (3개 이상 필요)');
}
function undo(name){ (pts[name] || []).pop(); draw(name); }
async function save(name, clear){
  const p = clear ? [] : (pts[name] || []);
  if (!clear && p.length < 3) { msg(name, '꼭짓점을 3개 이상 찍어주세요 (지금 ' + p.length + '개)'); return; }
  await fetch('/zone/' + name, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({points: p})});
  pts[name] = []; draw(name);
  msg(name, clear ? '초기화됨 (화면 전체)' : '저장됨');
}
function msg(name, t){ const m = document.getElementById('msg-' + name); m.textContent = t; setTimeout(() => m.textContent = '', 3000); }
function levelClass(free, total){ if (!total) return ''; const r = free / total; return r >= 0.3 ? 'ok' : r >= 0.1 ? 'warn' : 'busy'; }
async function update(){
  try{
    const d = await (await fetch('/count2')).json();
    for (const [k, v] of Object.entries(d)) {
      const el = document.getElementById('cnt-' + k), es = document.getElementById('seen-' + k);
      if (el) el.textContent = (v.zone === null ? '-' : v.zone);
      if (es) es.textContent = (v.seen === null ? '-' : v.seen);
    }
    const st = await (await fetch('/status')).json();
    document.getElementById('mode').textContent = st.label + (st.interval ? ' · ' + st.interval + '초마다 갱신' : '');
    const te = document.getElementById('temp');
    if (st.temp == null) { te.textContent = ''; te.style.display = 'none'; }
    else {
      te.style.display = '';
      const thr = st.throttled && st.throttled !== '0x0';
      te.textContent = 'CPU ' + st.temp.toFixed(1) + '℃' + (thr ? ' · 스로틀링 발생' : '');
      te.style.color = thr || st.temp >= 80 ? 'var(--busy)' : st.temp >= 70 ? 'var(--warn)' : '';
    }
    const s = await (await fetch('/api/state_local')).json();
    const sp = document.getElementById('s-people');
    if (s && s.seating) { sp.textContent = s.seating.people == null ? '-' : s.seating.people + '명'; }
    const sm = s && s.summary;
    const cf = document.getElementById('c-free');
    if (sm && sm.total) {
      document.getElementById('s-free').textContent = sm.empty + '자리';
      document.getElementById('s-free-sub').textContent = '등록 ' + sm.total + '자리 중 · 빈 좌석 ' + sm.free_seats + '석';
      document.getElementById('s-occ').textContent = sm.occupied + '자리';
      document.getElementById('s-occ-sub').textContent = '자리 비움 ' + sm.away + '자리 · 앉은 사람 약 ' + sm.people + '명';
      cf.className = 'card ' + levelClass(sm.empty, sm.total);
    } else {
      document.getElementById('s-free').textContent = '-';
      document.getElementById('s-occ').textContent = '-';
      cf.className = 'card';
    }
    const ck = document.getElementById('c-kiosk');
    if (s && s.kiosk && s.kiosk.people != null) {
      document.getElementById('s-kq').textContent = '줄 ' + s.kiosk.queue + '명';
      document.getElementById('s-kq-sub').textContent = '예상 대기 약 ' + s.kiosk.wait_min + '분 · 키오스크 앞 ' + s.kiosk.people + '명';
      ck.className = 'card ' + (s.kiosk.queue <= 0 ? 'ok' : s.kiosk.queue <= 6 ? 'warn' : 'busy');
    } else if (s && s.kiosk) {
      document.getElementById('s-kq').textContent = '-';
      document.getElementById('s-kq-sub').textContent = '키오스크 카메라 연결 안 됨';
      ck.className = 'card';
    }
  }catch(e){}
  const n = new Date();
  document.getElementById('clock').textContent = n.toLocaleDateString('ko-KR', {month: 'long', day: 'numeric', weekday: 'short'}) + ' ' + n.toTimeString().slice(0, 5);
}
document.getElementById('themeBtn').addEventListener('click', () => {
  const h = document.documentElement, dark = !h.dataset.dark;
  if (dark) h.dataset.dark = '1'; else delete h.dataset.dark;
  document.getElementById('themeBtn').setAttribute('aria-label', dark ? '밝은 화면으로 전환' : '어두운 화면으로 전환');
  try { localStorage.setItem('theme', dark ? 'dark' : 'light'); } catch (e) {}
});
setInterval(update, 1000); update();
</script></body></html>
"""


@app.route("/")
def index():
    blocks = "".join(
        f'<div class="cam"><div class="camhead"><b>{n}</b>'
        f'<span class="role{" kiosk" if n in KIOSK_CAMS else ""}">{"키오스크 줄" if n in KIOSK_CAMS else "좌석 구역"}</span>'
        f'<span class="cnt">영역 <b id="cnt-{n}">-</b>명 · 화면 <b id="seen-{n}">-</b>명</span></div>'
        f'<div class="box"><img src="/video/{n}"><canvas id="cv-{n}" onclick="addPoint(event, \'{n}\')"></canvas></div>'
        f'<div class="tools"><button onclick="save(\'{n}\')">구역 저장</button>'
        f'<button onclick="undo(\'{n}\')">점 취소</button>'
        f'<button onclick="save(\'{n}\', true)">초기화</button>'
        f'<span class="msg" id="msg-{n}"></span></div></div>'
        for n in cams
    )
    return PAGE.replace("__CAMS__", blocks)


@app.route("/api/state_local")
def state_local():
    return jsonify(state.get("snapshot") or {})


@app.route("/count")
def count():
    return jsonify({n: c.count for n, c in cams.items()})


@app.route("/count2")
def count2():
    return jsonify({n: {"zone": c.count, "seen": c.seen} for n, c in cams.items()})


@app.route("/status")
def status():
    mode = state["mode"] or current_mode()
    return jsonify(mode=mode, label=MODE_LABEL[mode], interval=state["interval"],
                   temp=state["temp"], throttled=state["throttled"])


@app.route("/zone/<name>", methods=["POST"])
def set_zone(name):
    if name not in cams:
        return "no such camera", 404
    points = (request.get_json(silent=True) or {}).get("points", [])
    with zones_lock:
        if len(points) >= 3:
            zones[name] = [[float(x), float(y)] for x, y in points]
        else:
            zones.pop(name, None)
        with open(ZONES_FILE, "w") as f:
            json.dump(zones, f)
        with open("zones_history.jsonl", "a") as f:
            f.write(json.dumps({"t": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "cam": name,
                                "zone": zones.get(name)}, ensure_ascii=False) + "\n")
    cams[name].history.clear()
    print(f"[{name}] zone {'set' if name in zones else 'cleared'}")
    return jsonify(ok=True)


TABLE_PAGE = r"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><title>테이블 영역 설정</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<script>try{if(new URLSearchParams(location.search).get("dark")==="1"||localStorage.getItem("theme")==="dark")document.documentElement.dataset.dark="1";}catch(e){}</script>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/gh/orioncactus/pretendard@v1.3.9/dist/web/static/pretendard.min.css">
<style>
:root{--ground:#f6f7f9;--surface:#fff;--raised:#f7f8fa;--active:#eef0f3;--border:#e6e8eb;--rule:#eef0f2;--control:#d1d5db;
--text:#111418;--muted:#5f6368;--faint:#6b6f76;--data:#2f6db5;--data-tint:rgba(47,109,181,.12);
--ok:#247b50;--ok-fill:#2f9e63;--ok-bg:#e4f4ea;--mid:#92400e;--mid-bg:#fdf3e1;--warn:var(--mid);--busy:#c4242a;--busy-bg:#fdeaea;--on-data:#fff;
--r-card:8px;--r-ctl:6px;--ease-out:cubic-bezier(.23,1,.32,1)}
html[data-dark]{color-scheme:dark;--ground:#0a0a0a;--surface:#141414;--raised:#1b1b1b;--active:#262626;--border:#262626;--rule:#202020;--control:#464646;
--text:#f2f2f2;--muted:#b0b0b0;--faint:#8f8f8f;--data:#5b9bea;--data-tint:rgba(91,155,234,.16);
--ok:#4cc584;--ok-fill:#3fc07a;--ok-bg:#15291c;--mid:#f0b452;--mid-bg:#2b2213;--busy:#ef6a7f;--busy-bg:#2c161a;--on-data:#0b0b0b}
*{box-sizing:border-box}
[hidden]{display:none!important}
body{margin:0;background:var(--ground);color:var(--text);font-family:Pretendard,-apple-system,"Apple SD Gothic Neo","Malgun Gothic",sans-serif;font-size:14px;line-height:20px;-webkit-font-smoothing:antialiased}
.topbar{height:56px;background:var(--surface);border-bottom:1px solid var(--border)}
.topbar .in{max-width:1600px;height:100%;margin:0 auto;padding:0 24px;display:flex;align-items:center;justify-content:space-between;gap:16px}
.mark{display:flex;align-items:center;gap:10px;font-size:16px;font-weight:700;color:var(--text);text-decoration:none;white-space:nowrap}
.mark svg{color:var(--data);flex:none}
.mark span{font-weight:400;color:var(--muted);font-size:14px;margin-left:6px}
.top-right{display:flex;align-items:center;gap:8px}
.pill{height:28px;display:inline-flex;align-items:center;padding:0 12px;border:1px solid var(--border);border-radius:999px;background:var(--raised);color:var(--muted);font-size:13px;white-space:nowrap;font-variant-numeric:tabular-nums}
.pill:empty{display:none}
button{height:32px;padding:0 12px;border:1px solid var(--control);border-radius:var(--r-ctl);background:var(--surface);color:var(--text);cursor:pointer;
  font:inherit;font-weight:600;display:inline-flex;align-items:center;gap:6px;transition:transform 160ms var(--ease-out),background-color 150ms ease}
button:hover{background:var(--raised)}
button:active{transform:scale(.97)}
button:focus-visible{outline:2px solid var(--data);outline-offset:2px}
.btn-icon{width:32px;padding:0;justify-content:center;color:var(--muted)}
#themeBtn{font-weight:500;color:var(--muted)}
#themeBtn .sun,#themeBtn .lbl-light{display:none}
html[data-dark] #themeBtn .sun,html[data-dark] #themeBtn .lbl-light{display:inline}
html[data-dark] #themeBtn .moon,html[data-dark] #themeBtn .lbl-dark{display:none}
.wrap{max-width:1600px;margin:0 auto;padding:24px 24px 32px}
h1{margin:0;font-size:24px;line-height:32px;font-weight:700;letter-spacing:-.01em}
a{color:var(--data)}
.help{color:var(--muted);line-height:1.6;margin:4px 0 16px;max-width:1100px}
.help b{color:var(--text)}
.tabs{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px}
.layout{display:flex;gap:16px;align-items:flex-start;flex-wrap:wrap}
.box{position:relative;width:min(1100px,100%)}
.box img{width:100%;aspect-ratio:16/9;display:block;border-radius:6px;background:#000;object-fit:fill}
.box canvas{position:absolute;left:0;top:0;width:100%;height:100%;cursor:crosshair}
.side{flex:1;min-width:260px;max-width:360px}
.panel{background:var(--surface);border:1px solid var(--border);border-radius:var(--r-card);padding:16px;margin-bottom:12px}
.panel h3{margin:0 0 10px;font-size:15px;font-weight:600}
.panel button{margin:8px 6px 0 0}
label{font-size:14px;margin-right:8px}
select,input[type=number]{background:var(--surface);color:var(--text);border:1px solid var(--control);border-radius:var(--r-ctl);height:32px;padding:0 8px;font:inherit}
input[type=number]{width:64px}
button.on{background:var(--data);border-color:var(--data);color:var(--on-data)}
button.primary{background:var(--ok-fill);border-color:var(--ok-fill);color:#fff}
.msg{font-size:13px;color:var(--ok);min-height:18px;margin-top:8px}
.warn{color:var(--mid)}
.multi{color:var(--data)}
table{width:100%;border-collapse:collapse;font-size:13px}
td{padding:6px 2px;border-bottom:1px solid var(--rule)}
td button{height:28px;margin:0}
td.r{text-align:right}
.dot{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:4px;vertical-align:0}
.stat{font-size:13px;color:var(--muted)}
</style></head><body>
<div class="topbar"><div class="in">
  <a class="mark" href="/"><svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 12h18a9 9 0 0 1-18 0Z"/><path d="M9.5 8.5c0-1.6 1.2-1.6 1.2-3.3M13.8 8.5c0-1.6 1.2-1.6 1.2-3.3"/></svg>밥먹을래말래<span>관리 화면 · 테이블 영역 설정</span></a>
  <div class="top-right"><a class="pill" href="/" style="text-decoration:none">← 카메라 화면</a>
    <button id="themeBtn" type="button" aria-label="어두운 화면으로 전환" title="밝은·어두운 화면 전환">
      <svg class="moon" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8Z"/></svg>
      <svg class="sun" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>
      <span class="lbl-dark">다크 모드</span><span class="lbl-light">라이트 모드</span>
    </button></div>
</div></div>
<div class="wrap">
<h1>테이블 영역 설정</h1>
<div class="help">
 <b>기준: 테이블 1개 + 의자 2개 = 자리 1개(T번호, 2석)</b>, 1인석은 S번호(1석)예요. 자리마다 <b>의자까지 포함해서</b> 꼭짓점을 3~8개 찍고 [테이블 추가]를 누르세요. 바로 저장되고 다음 번호가 자동으로 선택돼요. 두 카메라에 같은 자리가 보이면 <b>같은 번호</b>로 찍으세요.
 화면에 일부만 보이면 "일부만 보임"을 체크하세요. <b>같은 번호를 여러 카메라에 찍으면 한 테이블로 합쳐 판정해요</b>: 카메라 하나가 꺼지거나 가려져도 다른 카메라로 계속 판정하고, 구역은 테이블 전체가 보이는 카메라를 따라가요.
 여기 등록한 테이블은 자리 판정과 혼잡도에 바로 쓰여요. 한 사람은 발이 있는 테이블 하나에만 세요. 운영 시간이면 사람 위치(흰 점)와 테이블별 감지 인원이 함께 보여요.
</div>
<div class="tabs" id="tabs"></div>
<div class="layout">
  <div class="box"><img id="video" alt="카메라 영상"><canvas id="cv"></canvas></div>
  <div class="side">
    <div class="panel">
      <h3>새 테이블</h3>
      <label>번호 <select id="tid"></select></label>
      <label>좌석 <input type="number" id="seats" min="1" max="12" value="2"></label><br>
      <label><input type="checkbox" id="partial"> 일부만 보임</label><br>
      <button onclick="undo()">점 취소</button>
      <button class="primary" onclick="addTable()">테이블 추가</button>
      <div class="msg" id="msg"></div>
    </div>
    <div class="panel">
      <h3 id="listTitle">이 카메라의 테이블</h3>
      <table id="list"></table>
      <div class="stat" id="live"></div>
    </div>
    <div class="panel">
      <h3>전체 등록 현황</h3>
      <div class="stat" id="overall"></div>
    </div>
  </div>
</div>
<script>
const CAMS = __CAMS__, IDS = __IDS__;
let cur = CAMS[0], tables = {}, drawing = [], last = null;
const $ = id => document.getElementById(id);
const cv = $('cv'), ctx = cv.getContext('2d');

function msg(t, warn){ const m = $('msg'); m.textContent = t; m.className = 'msg' + (warn ? ' warn' : ''); clearTimeout(msg.t); msg.t = setTimeout(() => m.textContent = '', 4000); }
function where(id){ return CAMS.filter(c => (tables[c] || []).some(t => t.id === id)); }

function buildTabs(){
  $('tabs').innerHTML = '';
  CAMS.forEach(c => {
    const b = document.createElement('button');
    b.textContent = c + ' (' + (tables[c] || []).length + ')';
    if (c === cur) b.className = 'on';
    b.onclick = () => { cur = c; drawing = []; snap(); refresh(); };
    $('tabs').appendChild(b);
  });
}
function buildSelect(){
  const sel = $('tid'), keep = sel.value;
  sel.innerHTML = '';
  IDS.forEach(id => {
    const o = document.createElement('option'), w = where(id);
    o.value = id; o.textContent = id + (w.length ? '  ✓ ' + w.join(', ') : '');
    sel.appendChild(o);
  });
  const next = IDS.find(id => !where(id).length);
  sel.value = keep && IDS.includes(keep) && !where(keep).includes(cur) ? keep : (next || IDS[0]);
  $('seats').value = sel.value.startsWith('S') ? 1 : 2;
}
$('tid').onchange = () => { $('seats').value = $('tid').value.startsWith('S') ? 1 : 2; };

function inside(pt, poly){
  let c = false;
  for (let i = 0, j = poly.length - 1; i < poly.length; j = i++){
    const [xi, yi] = poly[i], [xj, yj] = poly[j];
    if ((yi > pt[1]) !== (yj > pt[1]) && pt[0] < (xj - xi) * (pt[1] - yi) / (yj - yi) + xi) c = !c;
  }
  return c;
}
function people(){
  const d = last && last.cams && last.cams[cur];
  if (!d) return [];
  return d.p.map(p => ({foot: [p[0] / d.w, p[1] / d.h], center: [(p[2] + p[4] / 2) / d.w, (p[3] + p[5] / 2) / d.h]}));
}
// 테이블별 인원: 라즈베리파이가 한 사람을 테이블 하나에만 나눠 센 값 (없으면 화면에서 대략 계산)
function occupancy(t, ps){
  const srv = last && last.tables && last.tables[cur];
  if (srv && t.id in srv) return srv[t.id];
  return ps.filter(p => inside(p.foot, t.pts) || inside(p.center, t.pts)).length;
}

function draw(){
  cv.width = cv.clientWidth; cv.height = cv.clientHeight;
  const W = cv.width, H = cv.height;
  ctx.clearRect(0, 0, W, H);
  const ps = people();
  (tables[cur] || []).forEach(t => {
    const n = occupancy(t, ps);
    ctx.beginPath();
    t.pts.forEach(([x, y], i) => i ? ctx.lineTo(x * W, y * H) : ctx.moveTo(x * W, y * H));
    ctx.closePath();
    ctx.fillStyle = n ? 'rgba(46,200,90,0.25)' : 'rgba(0,200,255,0.12)';
    ctx.fill();
    ctx.setLineDash(t.partial ? [8, 6] : []);
    ctx.strokeStyle = n ? '#3c6' : '#0cf'; ctx.lineWidth = 2; ctx.stroke(); ctx.setLineDash([]);
    const cx = t.pts.reduce((s, p) => s + p[0], 0) / t.pts.length * W;
    const cy = t.pts.reduce((s, p) => s + p[1], 0) / t.pts.length * H;
    const label = t.id + (last ? ' · ' + n + '/' + t.seats : ' · ' + t.seats + '석');
    ctx.font = 'bold 14px sans-serif';
    const tw = ctx.measureText(label).width;
    ctx.fillStyle = 'rgba(0,0,0,0.7)'; ctx.fillRect(cx - tw / 2 - 5, cy - 11, tw + 10, 22);
    ctx.fillStyle = '#fff'; ctx.textAlign = 'center'; ctx.textBaseline = 'middle'; ctx.fillText(label, cx, cy);
    ctx.textAlign = 'start'; ctx.textBaseline = 'alphabetic';
  });
  ps.forEach(p => { ctx.fillStyle = '#fff'; ctx.beginPath(); ctx.arc(p.foot[0] * W, p.foot[1] * H, 4, 0, 7); ctx.fill(); });
  if (drawing.length){
    ctx.strokeStyle = '#ff0'; ctx.fillStyle = 'rgba(255,255,0,0.15)'; ctx.lineWidth = 2;
    ctx.beginPath();
    drawing.forEach(([x, y], i) => i ? ctx.lineTo(x * W, y * H) : ctx.moveTo(x * W, y * H));
    if (drawing.length >= 3){ ctx.closePath(); ctx.fill(); }
    ctx.stroke();
    ctx.fillStyle = '#ff0'; ctx.font = 'bold 13px sans-serif';
    drawing.forEach(([x, y], i) => { ctx.beginPath(); ctx.arc(x * W, y * H, 5, 0, 7); ctx.fill(); ctx.fillText(String(i + 1), x * W + 7, y * H - 7); });
  }
}
cv.addEventListener('click', e => {
  if (drawing.length >= 8){ msg('꼭짓점은 8개까지예요. [테이블 추가] 또는 [점 취소]', true); return; }
  const r = cv.getBoundingClientRect();
  drawing.push([(e.clientX - r.left) / r.width, (e.clientY - r.top) / r.height]);
  draw();
});
function undo(){ drawing.pop(); draw(); }

async function save(cam){
  const r = await fetch('/api/tables/' + cam, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({tables: tables[cam] || []})});
  if (!r.ok) throw new Error(r.status);
}
async function addTable(){
  if (drawing.length < 3){ msg('꼭짓점을 3개 이상 찍어 주세요 (지금 ' + drawing.length + '개)', true); return; }
  const id = $('tid').value, list = tables[cur] = tables[cur] || [];
  if (list.some(t => t.id === id)){ msg(id + '는 이 카메라에 이미 있어요. 목록에서 지운 뒤 다시 추가하세요', true); return; }
  list.push({id, seats: +$('seats').value || 1, partial: $('partial').checked, pts: drawing});
  try { await save(cur); drawing = []; $('partial').checked = false; msg(id + ' 저장됨'); }
  catch (e) { list.pop(); msg('저장 실패: 서버 연결을 확인하세요', true); }
  refresh();
}
async function removeTable(id){
  const list = tables[cur] || [], i = list.findIndex(t => t.id === id);
  if (i < 0) return;
  const [gone] = list.splice(i, 1);
  try { await save(cur); msg(id + ' 삭제됨'); }
  catch (e) { list.splice(i, 0, gone); msg('삭제 실패', true); }
  refresh();
}
function buildList(){
  const ps = people(), list = (tables[cur] || []).slice().sort((a, b) => IDS.indexOf(a.id) - IDS.indexOf(b.id));
  $('listTitle').textContent = cur + '의 테이블 ' + list.length + '개';
  $('list').innerHTML = list.map(t => {
    const others = where(t.id).filter(c => c !== cur);
    const n = occupancy(t, ps);
    return `<tr><td><span class="dot" style="background:${last && n ? '#3c6' : '#0cf'}"></span><b>${t.id}</b> ${t.seats}석${t.partial ? ' · 일부' : ''}${others.length ? ' <span class="multi">· ' + others.join(', ') + '에서도 보임 (합쳐 판정)</span>' : ''}</td>`
         + `<td class="r">${last ? n + '명' : ''}</td><td class="r"><button onclick="removeTable('${t.id}')">삭제</button></td></tr>`;
  }).join('') || '<tr><td class="stat">아직 없어요. 영상 위를 클릭해 시작하세요.</td></tr>';
  $('live').textContent = last ? '사람 위치 기준: ' + last.t : '운영 시간이 아니라 사람 위치는 표시되지 않아요.';
  const done = IDS.filter(id => where(id).length), dup = IDS.filter(id => where(id).length > 1);
  const missing = IDS.filter(id => !where(id).length);
  $('overall').innerHTML = `등록 ${done.length}/${IDS.length}개 · 여러 카메라로 보는 테이블 ${dup.length}개<br>`
    + (done.length ? '등록된 번호: ' + done.join(', ') + '<br>' : '') + (missing.length ? '다음 빈 번호: ' + missing.filter(x => x.startsWith('T'))[0] + ' · 남은 번호 ' + missing.length + '개' : '모든 번호가 등록됐어요.');
}
function refresh(){ buildTabs(); buildSelect(); buildList(); draw(); }

async function loadTables(){ try { tables = await (await fetch('/api/tables')).json(); } catch (e) {} refresh(); }
async function loadLast(){ try { const d = await (await fetch('/api/last')).json(); last = d && d.cams ? d : null; } catch (e) {} buildList(); draw(); }
async function ping(){ try { await fetch('/api/setup', {method: 'POST'}); } catch (e) {} }

window.addEventListener('resize', draw);
$('video').onload = draw;
let snapBusy = false;
function snap(){
  if (snapBusy) return;
  snapBusy = true;
  const img = new Image();
  img.onload = () => { $('video').src = img.src; snapBusy = false; };
  img.onerror = () => { snapBusy = false; };
  img.src = '/snap/' + cur + '?t=' + Date.now();
}
snap(); setInterval(snap, 1000);
ping(); setInterval(ping, 60000);
loadTables(); loadLast(); setInterval(loadLast, 3000);
document.getElementById('themeBtn').addEventListener('click', () => {
  const h = document.documentElement, dark = !h.dataset.dark;
  if (dark) h.dataset.dark = '1'; else delete h.dataset.dark;
  document.getElementById('themeBtn').setAttribute('aria-label', dark ? '밝은 화면으로 전환' : '어두운 화면으로 전환');
  try { localStorage.setItem('theme', dark ? 'dark' : 'light'); } catch (e) {}
});
</script></div></body></html>
"""


@app.route("/tables")
def tables_page():
    return TABLE_PAGE.replace("__CAMS__", json.dumps(list(cams))).replace("__IDS__", json.dumps(TABLE_IDS))


@app.route("/api/tables")
def get_tables():
    with tables_lock:
        return jsonify(tables)


@app.route("/api/registry")
def registry():
    """Tables as objects (every camera view joined) with their current state."""
    live = (state.get("snapshot") or {}).get("tables") or {}
    return jsonify({tid: dict(info, state=live.get(tid)) for tid, info in table_registry().items()})


@app.route("/api/tables/<name>", methods=["POST"])
def set_tables(name):
    if name not in cams:
        return "no such camera", 404
    items = (request.get_json(silent=True) or {}).get("tables", [])
    clean = []
    for t in items:
        pts = [[min(1.0, max(0.0, float(x))), min(1.0, max(0.0, float(y)))] for x, y in t.get("pts", [])]
        if t.get("id") in TABLE_IDS and len(pts) >= 3:
            clean.append({"id": t["id"], "seats": max(1, min(12, int(t.get("seats") or 1))),
                          "partial": bool(t.get("partial")), "pts": pts})
    with tables_lock:
        if clean:
            tables[name] = clean
        else:
            tables.pop(name, None)
        with open(TABLES_FILE, "w") as f:
            json.dump(tables, f, ensure_ascii=False)
        with open("tables_history.jsonl", "a") as f:
            f.write(json.dumps({"t": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "cam": name,
                                "tables": tables.get(name, [])}, ensure_ascii=False) + "\n")
    print(f"[{name}] tables saved: {[t['id'] for t in clean]}")
    return jsonify(ok=True, count=len(clean))


@app.route("/api/setup", methods=["POST"])
def setup_ping():
    setup_until[0] = time.time() + SETUP_KEEPALIVE
    return jsonify(ok=True, closed=current_mode() == "closed")


@app.route("/api/last")
def last_detections():
    return jsonify(state["last"] or {})


@app.route("/snap/<name>")
def snap(name):
    cam = cams.get(name)
    data = cam.jpeg() if cam else None
    if not data:
        return "", 204
    return Response(data, mimetype="image/jpeg", headers={"Cache-Control": "no-store"})


def mjpeg(cam):
    while True:
        jpg = cam.jpeg()
        if jpg:
            yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + jpg + b"\r\n"
        time.sleep(1 / OUT_FPS)


@app.route("/video/<name>")
def video(name):
    if name not in cams:
        return "no such camera", 404
    return Response(mjpeg(cams[name]), mimetype="multipart/x-mixed-replace; boundary=frame")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, threaded=True)
