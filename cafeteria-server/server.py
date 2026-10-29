import json, os, sqlite3, datetime, threading, time
from flask import Flask, request, jsonify, Response

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(BASE, "counts.db")
STATE_PATH = os.path.join(BASE, "state_latest.json")
LAYOUT_PATH = os.path.join(BASE, "layout.json")
TOKEN = open(os.path.join(BASE, "token.txt")).read().strip()

CAPACITY = 120                   # 좌석 수 (테이블 등록이 끝나면 등록된 좌석 수를 자동으로 씀)
KIOSK_CAMS = {"cam4"}            # 키오스크 줄 카메라: 좌석 인원 합계에서 뺌
ALLOWED_INGEST = {"<라즈베리파이 Tailscale IP>"}   # 데이터 입력(/api/ingest, /api/state)을 허용하는 주소 (라즈베리파이 Tailscale IP)
STALE_SEC = 60
OPEN_START, OPEN_END = "08:00", "19:30"
OPEN_DAYS = {0, 1, 2, 3, 4}

SLOT_MIN = 15
WINDOWS = [("점심", "11:00", "14:00"), ("저녁", "16:30", "19:30")]
WINDOW_SWITCH = "15:15"
CACHE_SEC = 20

app = Flask(__name__)
_cache, _lock = {}, threading.Lock()
latest_state = {"obj": None}


def db():
    con = sqlite3.connect(DB, timeout=10)
    con.row_factory = sqlite3.Row
    return con


with db() as con:
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("""CREATE TABLE IF NOT EXISTS counts(
        ts TEXT PRIMARY KEY, total INTEGER, total_raw INTEGER,
        cams TEXT, mode TEXT, received_at TEXT)""")
    con.execute("""CREATE TABLE IF NOT EXISTS state_log(
        ts TEXT PRIMARY KEY, seat_people INTEGER, tables_total INTEGER, tables_empty INTEGER,
        tables_occupied INTEGER, tables_away INTEGER, free_seats INTEGER,
        kiosk_people INTEGER, kiosk_queue INTEGER, kiosk_wait REAL, received_at TEXT)""")

try:
    with open(STATE_PATH) as f:
        latest_state["obj"] = json.load(f)
except Exception:
    pass


def to_int(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def is_open(now):
    hm = now.strftime("%H:%M")
    return now.weekday() in OPEN_DAYS and OPEN_START <= hm < OPEN_END


def seat_total(cams):
    """Sum of seating cameras only (kiosk cameras excluded)."""
    vals = [v for k, v in cams.items() if k not in KIOSK_CAMS and v is not None]
    return sum(vals) if vals else None


def kiosk_count(cams):
    for k in KIOSK_CAMS:
        if k in cams:
            return cams[k]
    return None


def level_from_ratio(free_ratio):
    return "여유" if free_ratio >= 0.3 else "보통" if free_ratio >= 0.1 else "혼잡"


def slot_of(hm):
    h, m = int(hm[:2]), int(hm[3:5])
    return "%02d:%02d" % (h, m - m % SLOT_MIN)


def cached(key, fn):
    with _lock:
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < CACHE_SEC:
            return hit[1]
    val = fn()
    with _lock:
        _cache[key] = (time.time(), val)
    return val


def authorized():
    if request.headers.get("X-API-Key") != TOKEN:
        return False
    return request.remote_addr in ALLOWED_INGEST


# ---------- 입력 (라즈베리파이) ----------
@app.post("/api/ingest")
def ingest():
    if not authorized():
        return jsonify(error="unauthorized"), 401
    rows = request.get_json(force=True)
    if isinstance(rows, dict):
        rows = [rows]
    now = datetime.datetime.now().isoformat(timespec="seconds")
    n = 0
    with db() as con:
        for r in rows:
            ts = r.get("timestamp")
            if not ts:
                continue
            cams = {k: to_int(v) for k, v in r.items() if k.startswith("cam")}
            cur = con.execute(
                "INSERT OR IGNORE INTO counts VALUES (?,?,?,?,?,?)",
                (ts, to_int(r.get("total")), to_int(r.get("total_raw")),
                 json.dumps(cams), r.get("mode"), now))
            n += cur.rowcount
    return jsonify(ok=True, inserted=n)


@app.post("/api/state")
def ingest_state():
    if not authorized():
        return jsonify(error="unauthorized"), 401
    obj = request.get_json(force=True)
    if not isinstance(obj, dict) or "t" not in obj:
        return jsonify(error="bad state"), 400
    obj["received_at"] = datetime.datetime.now().isoformat(timespec="seconds")
    latest_state["obj"] = obj
    try:
        with open(STATE_PATH + ".tmp", "w") as f:
            json.dump(obj, f, ensure_ascii=False)
        os.replace(STATE_PATH + ".tmp", STATE_PATH)
    except Exception:
        pass
    sm, k, seating = obj.get("summary") or {}, obj.get("kiosk") or {}, obj.get("seating") or {}
    with db() as con:
        con.execute("INSERT OR IGNORE INTO state_log VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (obj["t"], seating.get("people"), sm.get("total"), sm.get("empty"), sm.get("occupied"),
                     sm.get("away"), sm.get("free_seats"), k.get("people"), k.get("queue"), k.get("wait_min"),
                     obj["received_at"]))
    return jsonify(ok=True)


# ---------- 조회 (웹) ----------
@app.get("/api/latest")
def latest():
    now = datetime.datetime.now()
    with db() as con:
        r = con.execute("SELECT * FROM counts ORDER BY ts DESC LIMIT 1").fetchone()
    if not r:
        return jsonify(status="nodata", open=is_open(now), capacity=CAPACITY)
    age = (now - datetime.datetime.fromisoformat(r["received_at"])).total_seconds()
    cams = json.loads(r["cams"])
    people = seat_total(cams)
    st = latest_state["obj"] or {}
    st_age = None
    if st.get("received_at"):
        st_age = (now - datetime.datetime.fromisoformat(st["received_at"])).total_seconds()
    fresh_state = st_age is not None and st_age <= STALE_SEC and st.get("mode") != "closed"
    summ = st.get("summary") if fresh_state else None
    kiosk = st.get("kiosk") if fresh_state else None
    if kiosk is None and kiosk_count(cams) is not None:      # 상태 파일 전이라도 cam4 인원으로 계산
        k = kiosk_count(cams)
        q = max(0, k - 3)
        kiosk = {"people": k, "queue": q, "wait_min": round(q / 4.0, 1), "n": 3, "rate": 4.0, "estimated": True}

    if summ and summ.get("total"):
        capacity = summ["seats"] or CAPACITY
        free_ratio = summ["empty"] / summ["total"]
        basis = "tables"
    else:
        capacity = CAPACITY
        free_ratio = max(0.0, (capacity - (people or 0)) / capacity)
        basis = "people"
    return jsonify(status="stale" if age > STALE_SEC else "ok", open=is_open(now),
                   ts=r["ts"], people=people, total=people,
                   cams={k: v for k, v in cams.items() if k not in KIOSK_CAMS},
                   kiosk=kiosk, tables=(st.get("tables") if fresh_state else None), summary=summ,
                   level=level_from_ratio(free_ratio), free_pct=round(free_ratio * 100), basis=basis,
                   capacity=capacity, age_sec=int(age))


@app.get("/api/today")
def today():
    def build():
        d = datetime.date.today().isoformat()
        with db() as con:
            rows = con.execute(
                "SELECT ts, cams FROM counts WHERE ts >= ? AND ts < ? ORDER BY ts",
                (d, d + "~")).fetchall()
        step = max(1, len(rows) // 600)
        out = []
        for r in rows[::step]:
            cams = json.loads(r["cams"])
            out.append({"ts": r["ts"], "total": seat_total(cams), "kiosk": kiosk_count(cams)})
        return out
    return jsonify(cached("today", build))


@app.get("/api/slots")
def slots():
    """운영 시간 전체를 15분 칸으로 나눠 오늘 실제값과 지난 평일 평균·최소·최대를 돌려준다."""
    def build():
        now = datetime.datetime.now()
        name, start, end = "하루", OPEN_START, OPEN_END
        today_str = now.date().isoformat()
        labels = []
        t = datetime.datetime.strptime(start, "%H:%M")
        e = datetime.datetime.strptime(end, "%H:%M")
        while t < e:
            labels.append(t.strftime("%H:%M"))
            t += datetime.timedelta(minutes=SLOT_MIN)
        with db() as con:
            rows = con.execute(
                "SELECT ts, cams FROM counts WHERE substr(ts, 12, 5) >= ? AND substr(ts, 12, 5) < ?",
                (start, end)).fetchall()
        actual, past, days = {}, {}, set()
        for r in rows:
            v = seat_total(json.loads(r["cams"]))
            if v is None:
                continue
            d, s = r["ts"][:10], slot_of(r["ts"][11:16])
            if d == today_str:
                actual.setdefault(s, []).append(v)
                continue
            try:
                wd = datetime.date.fromisoformat(d).weekday()
            except ValueError:
                continue
            if wd in OPEN_DAYS:
                past.setdefault(s, {}).setdefault(d, []).append(v)
                days.add(d)

        def avg(xs):
            return round(sum(xs) / len(xs), 1) if xs else None

        out = []
        for s in labels:
            per_day = [avg(v) for v in past.get(s, {}).values()]
            out.append({"t": s, "actual": avg(actual.get(s, [])), "forecast": avg(per_day),
                        "lo": (round(min(per_day), 1) if per_day else None),
                        "hi": (round(max(per_day), 1) if per_day else None)})
        return {"window": name, "now_slot": slot_of(now.strftime("%H:%M")), "capacity": CAPACITY,
                "days": len(days), "day_list": sorted(days), "slots": out}
    return jsonify(cached("slots", build))


@app.get("/api/history")
def history():
    """최근 평일 며칠의 날짜별 15분 칸 인원 (지난날 비교용)."""
    def build():
        now = datetime.datetime.now()
        name, start, end = WINDOWS[0] if now.strftime("%H:%M") < WINDOW_SWITCH else WINDOWS[1]
        with db() as con:
            rows = con.execute(
                "SELECT ts, cams FROM counts WHERE substr(ts, 12, 5) >= ? AND substr(ts, 12, 5) < ?",
                (start, end)).fetchall()
        by_day = {}
        for r in rows:
            v = seat_total(json.loads(r["cams"]))
            if v is None:
                continue
            d, s = r["ts"][:10], slot_of(r["ts"][11:16])
            by_day.setdefault(d, {}).setdefault(s, []).append(v)
        days = sorted(by_day)[-7:]
        return {"window": name, "days": [
            {"date": d, "weekday": "월화수목금토일"[datetime.date.fromisoformat(d).weekday()],
             "slots": {s: round(sum(v) / len(v), 1) for s, v in by_day[d].items()}}
            for d in days]}
    return jsonify(cached("history", build))


@app.get("/api/layout")
def get_layout():
    try:
        with open(LAYOUT_PATH, encoding="utf-8") as f:
            return Response(f.read(), mimetype="application/json")
    except FileNotFoundError:
        return jsonify(w=1000, h=600, fixtures=[], seats={})


@app.post("/api/layout")
def set_layout():
    if request.headers.get("X-API-Key") != TOKEN:
        return jsonify(error="unauthorized"), 401
    obj = request.get_json(force=True)
    if not isinstance(obj, dict) or "seats" not in obj:
        return jsonify(error="bad layout"), 400
    if os.path.exists(LAYOUT_PATH):
        os.replace(LAYOUT_PATH, LAYOUT_PATH + ".bak")
    with open(LAYOUT_PATH, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    return jsonify(ok=True, seats=len(obj["seats"]))


@app.get("/layout")
def layout_editor():
    with open(os.path.join(BASE, "layout.html"), encoding="utf-8") as f:
        return Response(f.read(), mimetype="text/html")


@app.get("/")
def index():
    with open(os.path.join(BASE, "index.html"), encoding="utf-8") as f:
        return Response(f.read(), mimetype="text/html")
