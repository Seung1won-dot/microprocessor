import json, os, sqlite3, datetime, threading, time
from flask import Flask, request, jsonify, Response

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(BASE, "counts.db")
STATE_PATH = os.path.join(BASE, "state_latest.json")
LAYOUT_PATH = os.path.join(BASE, "layout.json")
ZONE_SEATS_PATH = os.path.join(BASE, "zone_seats.json")
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
TBL_EMPTY = 240                  # 라즈베리파이 설정과 같게: 마지막으로 본 뒤 이만큼 지나야 "빈 자리"가 됨 (이용 시간에서 뺌)
# 구역(카메라)별 좌석 수: 라즈베리파이 상태에 구역 합계가 오면 zone_seats.json으로 갱신 (11/3 재배치 기준 기본값)
ZONE_SEATS_DEFAULT = {"cam1": 36, "cam2": 58, "cam3": 66}

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
    # 이용자 의견: 그때 화면이 보여준 값과 함께 저장 (이름·연락처·IP는 저장하지 않음)
    con.execute("""CREATE TABLE IF NOT EXISTS feedback(
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, answer TEXT, text TEXT,
        level TEXT, free_pct INTEGER, people INTEGER, used_seats INTEGER, open INTEGER, cid TEXT)""")
    # 접속 기록: 페이지를 열 때 한 줄 (익명 기기 id, 들어온 사이트 도메인만. IP는 저장하지 않음)
    con.execute("""CREATE TABLE IF NOT EXISTS visits(
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, day TEXT, cid TEXT, ref TEXT)""")
    if "source" not in [r[1] for r in con.execute("PRAGMA table_info(feedback)")]:
        con.execute("ALTER TABLE feedback ADD COLUMN source TEXT")     # button / return (돌아왔을 때 알림)
    cols = [r[1] for r in con.execute("PRAGMA table_info(state_log)")]
    if "zones" not in cols:
        con.execute("ALTER TABLE state_log ADD COLUMN zones TEXT")            # 구역별 합계 JSON
        con.execute("ALTER TABLE state_log ADD COLUMN unknown_seats INTEGER")  # 카메라가 꺼져 판정 못 한 좌석
    # 테이블 상태가 바뀐 순간 한 줄 (테이블별 이용 횟수·이용 시간 계산용)
    con.execute("""CREATE TABLE IF NOT EXISTS table_events(
        ts TEXT, tid TEXT, st TEXT, n INTEGER, zone TEXT, PRIMARY KEY(ts, tid))""")

try:
    with open(STATE_PATH) as f:
        latest_state["obj"] = json.load(f)
except Exception:
    pass

zone_seats = dict(ZONE_SEATS_DEFAULT)
try:
    with open(ZONE_SEATS_PATH) as f:
        zone_seats.update(json.load(f))
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


ZONE_NAMES = {"cam3": "왼쪽 구역", "cam2": "가운데 구역", "cam1": "오른쪽 구역"}   # index.html의 ZONES와 같게


def zone_label(z):
    return "키오스크 줄" if z in KIOSK_CAMS else ZONE_NAMES.get(z, "구역 " + z.replace("cam", ""))


def zone_status(z, v):
    """구역 하나의 지금 상태: 전체와 같은 계산 (사용 좌석 = max(점유 테이블 좌석, 그 카메라 인원))."""
    seats, people = v.get("seats") or 0, v.get("people")
    out = {"zone": z, "label": zone_label(z), "seats": seats, "free_seats": v.get("free_seats"),
           "unknown_seats": v.get("unknown_seats") or 0, "people": people,
           "occupied": v.get("occupied"), "away": v.get("away"), "empty": v.get("empty")}
    if seats:
        used = max(seats - (v.get("free_seats") or 0), people or 0)
        ratio = max(0.0, (seats - used) / seats)
        out.update(used_seats=used, free_pct=round(ratio * 100), level=level_from_ratio(ratio))
    return out


def table_changes(prev, cur):
    """앞 상태와 비교해 바뀐 테이블: [(id, 상태, 인원, 구역)]. 처음 보는 빈 테이블은 기록하지 않음."""
    before = (prev or {}).get("tables") or {}
    out = []
    for tid, v in ((cur or {}).get("tables") or {}).items():
        if v.get("unknown") or not v.get("st"):
            continue
        p = before.get(tid)
        if (p is None and v["st"] != "empty") or (p is not None and p.get("st") != v["st"]):
            out.append((tid, v["st"], v.get("n"), v.get("zone")))
    return out


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
    prev, latest_state["obj"] = latest_state["obj"], obj
    try:
        with open(STATE_PATH + ".tmp", "w") as f:
            json.dump(obj, f, ensure_ascii=False)
        os.replace(STATE_PATH + ".tmp", STATE_PATH)
    except Exception:
        pass
    zones = obj.get("zones") or {}
    seats_now = {z: (v.get("seats") or 0) + (v.get("unknown_seats") or 0) for z, v in zones.items()}
    if seats_now and any(zone_seats.get(z) != n for z, n in seats_now.items()):
        zone_seats.update(seats_now)                   # 테이블 등록이 바뀌면 구역 좌석 수도 따라감
        try:
            with open(ZONE_SEATS_PATH, "w") as f:
                json.dump(zone_seats, f)
        except Exception:
            pass
    sm, k, seating = obj.get("summary") or {}, obj.get("kiosk") or {}, obj.get("seating") or {}
    with db() as con:
        con.execute("""INSERT OR IGNORE INTO state_log(ts, seat_people, tables_total, tables_empty, tables_occupied,
                       tables_away, free_seats, kiosk_people, kiosk_queue, kiosk_wait, received_at, zones, unknown_seats)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (obj["t"], seating.get("people"), sm.get("total"), sm.get("empty"), sm.get("occupied"),
                     sm.get("away"), sm.get("free_seats"), k.get("people"), k.get("queue"), k.get("wait_min"),
                     obj["received_at"], json.dumps(zones) if zones else None, sm.get("unknown_seats")))
        con.executemany("INSERT OR IGNORE INTO table_events VALUES (?,?,?,?,?)",
                        [(obj["t"],) + c for c in table_changes(prev, obj)])
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

    used_seats = seat_range = None
    if summ and summ.get("total"):
        # 좌석과 사람을 함께 봄 (docs: 혼잡도-계산.md): 사용 좌석 = max(점유 테이블 좌석 합, 전체 인원)
        # 점유 테이블은 통째로 쓴다고 보고(합석 안 함), 서 있는 사람도 곧 앉을 수요로 본다. 자리 비움 테이블도 점유로 셈
        capacity = summ.get("seats") or CAPACITY
        s_occ = capacity - (summ.get("free_seats") or 0)
        n_occ = summ["total"] - summ["empty"]
        used_seats = max(s_occ, people or 0)
        seat_range = [capacity - used_seats, capacity - max(n_occ, people or 0)]   # 빈 좌석 [보수, 낙관]
        free_ratio = (capacity - used_seats) / capacity
        basis = "seats+people"
    else:
        capacity = CAPACITY
        free_ratio = max(0.0, (capacity - (people or 0)) / capacity)
        basis = "people"
    zones = None
    if fresh_state and st.get("zones"):
        zones = [zone_status(z, v) for z, v in sorted(st["zones"].items())]
    return jsonify(status="stale" if age > STALE_SEC else "ok", open=is_open(now),
                   ts=r["ts"], people=people, total=people,
                   cams={k: v for k, v in cams.items() if k not in KIOSK_CAMS},
                   kiosk=kiosk, tables=(st.get("tables") if fresh_state else None), summary=summ, zones=zones,
                   level=level_from_ratio(free_ratio), free_pct=round(free_ratio * 100), basis=basis,
                   used_seats=used_seats, free_seat_range=seat_range,
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


def slot_series():
    """운영 시간 전체를 15분 칸으로 나눠, 전체(좌석 카메라 합)와 카메라(구역)마다 오늘 실제값과
    지난 평일 평균·최소·최대를 한 번에 계산한다. 평소 값은 날짜별 칸 평균을 다시 평균 (기록이 많은 날이 더 세지 않게).
    꺼져 있던 카메라는 그 칸에서 빠진다. 전체의 평소 값은 좌석 카메라가 모두 켜져 있던 기록만 써서
    카메라 하나가 꺼진 날 때문에 평소가 낮아지지 않게 한다."""
    def build():
        now = datetime.datetime.now()
        today_str = now.date().isoformat()
        labels = []
        t = datetime.datetime.strptime(OPEN_START, "%H:%M")
        e = datetime.datetime.strptime(OPEN_END, "%H:%M")
        while t < e:
            labels.append(t.strftime("%H:%M"))
            t += datetime.timedelta(minutes=SLOT_MIN)
        with db() as con:
            rows = con.execute(
                "SELECT ts, cams FROM counts WHERE substr(ts, 12, 5) >= ? AND substr(ts, 12, 5) < ?",
                (OPEN_START, OPEN_END)).fetchall()
        actual, past, days, weekday_ok = {}, {}, {}, {}
        for r in rows:
            cams = json.loads(r["cams"])
            d, s = r["ts"][:10], slot_of(r["ts"][11:16])
            vals = {k: v for k, v in cams.items() if v is not None}
            seat = [v for k, v in cams.items() if k not in KIOSK_CAMS]
            if d == today_str:
                if seat_total(cams) is not None:
                    vals["all"] = seat_total(cams)
                for k, v in vals.items():
                    actual.setdefault(k, {}).setdefault(s, []).append(v)
                continue
            if d not in weekday_ok:
                try:
                    weekday_ok[d] = datetime.date.fromisoformat(d).weekday() in OPEN_DAYS
                except ValueError:
                    weekday_ok[d] = False
            if not weekday_ok[d]:
                continue
            if seat and all(v is not None for v in seat):
                vals["all"] = sum(seat)
            for k, v in vals.items():
                past.setdefault(k, {}).setdefault(s, {}).setdefault(d, []).append(v)
                days.setdefault(k, set()).add(d)

        def avg(xs):
            return round(sum(xs) / len(xs), 1) if xs else None

        series = {}
        for k in set(past) | set(actual):
            out = []
            for s in labels:
                per_day = [avg(v) for v in past.get(k, {}).get(s, {}).values()]
                out.append({"t": s, "actual": avg(actual.get(k, {}).get(s, [])), "forecast": avg(per_day),
                            "lo": (round(min(per_day), 1) if per_day else None),
                            "hi": (round(max(per_day), 1) if per_day else None)})
            series[k] = {"days": len(days.get(k, ())), "day_list": sorted(days.get(k, ())), "slots": out}
        return {"now_slot": slot_of(now.strftime("%H:%M")), "series": series}
    return cached("slot_series", build)


def zone_names(series):
    return sorted(k for k in series if k.startswith("cam"))


@app.get("/api/slots")
def slots():
    """15분 칸별 오늘 실제값과 평소 값. ?zone=cam2 처럼 구역(카메라) 하나만 볼 수 있음 (없으면 전체)."""
    data = slot_series()
    zone = request.args.get("zone") or "all"
    if zone not in data["series"]:
        zone = "all"
    s = data["series"].get(zone) or {"days": 0, "day_list": [], "slots": []}
    cap = CAPACITY if zone == "all" else zone_seats.get(zone)
    return jsonify(window="하루", zone=zone, label=("전체" if zone == "all" else zone_label(zone)),
                   now_slot=data["now_slot"], capacity=cap, days=s["days"], day_list=s["day_list"],
                   slots=s["slots"], zones=zone_names(data["series"]))


def busy_span(slots, peak):
    """가장 붐비는 칸 앞뒤로 평소 값이 그 80% 이상인 칸이 이어지는 구간."""
    i = next(n for n, x in enumerate(slots) if x is peak)
    lo = hi = i
    cut = peak["forecast"] * 0.8
    while lo > 0 and (slots[lo - 1]["forecast"] or 0) >= cut:
        lo -= 1
    while hi < len(slots) - 1 and (slots[hi + 1]["forecast"] or 0) >= cut:
        hi += 1
    return slots[lo]["t"], slots[hi]["t"]


@app.get("/api/zones")
def zones_forecast():
    """구역(카메라)마다 평소 가장 붐비는 때: 시각, 인원, 좌석 대비 비율, 붐비는 구간, 지금 칸의 평소 값."""
    data = slot_series()
    now_slot = data["now_slot"]
    out = []
    for z in zone_names(data["series"]):
        s = data["series"][z]
        fc = [x for x in s["slots"] if x["forecast"] is not None]
        if not fc:
            continue
        peak = max(fc, key=lambda x: x["forecast"])
        start, end = busy_span(s["slots"], peak)
        seats = None if z in KIOSK_CAMS else zone_seats.get(z)
        now = next((x for x in s["slots"] if x["t"] == now_slot), None)
        out.append({"zone": z, "label": zone_label(z), "kiosk": z in KIOSK_CAMS, "seats": seats, "days": s["days"],
                    "peak": peak["t"], "peak_people": peak["forecast"],
                    "peak_pct": round(peak["forecast"] / seats * 100) if seats else None,
                    "busy_from": start, "busy_to": end,
                    "now_forecast": now["forecast"] if now else None, "now_actual": now["actual"] if now else None})
    return jsonify(now_slot=now_slot, zones=out)


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


# ---------- 이용자 의견 ----------
FEEDBACK_ANSWERS = {"맞아요", "더 붐볐어요", "더 한산했어요"}
FEEDBACK_SOURCES = {"button", "return"}   # 카드 아래 버튼 / 식당 다녀와 돌아왔을 때 알림
FEEDBACK_GAP = 600          # 같은 기기는 10분에 한 번
FEEDBACK_PER_MIN = 60       # 전체 1분 상한 (장난 방지)
_fb_last, _fb_recent, _fb_lock = {}, [], threading.Lock()


@app.post("/api/feedback")
def feedback():
    """한 번 누르는 의견. 처음에는 answer를 보내고, 한 줄 의견은 같은 id로 text를 덧붙인다."""
    obj = request.get_json(silent=True) or {}
    cid = str(obj.get("cid") or "")[:40]
    if len(cid) < 8:
        return jsonify(ok=False, error="bad request"), 400
    now_t = time.time()
    if obj.get("id") is not None:                      # 한 줄 의견 덧붙이기
        text = str(obj.get("text") or "").strip()[:200]
        if not text:
            return jsonify(ok=False, error="empty"), 400
        with db() as con:
            cur = con.execute("UPDATE feedback SET text=? WHERE id=? AND cid=? AND (text IS NULL OR text='')",
                              (text, int(obj["id"]), cid))
        return jsonify(ok=cur.rowcount == 1)
    answer = str(obj.get("answer") or "")
    if answer not in FEEDBACK_ANSWERS:
        return jsonify(ok=False, error="bad answer"), 400
    with _fb_lock:
        _fb_recent[:] = [t for t in _fb_recent if now_t - t < 60]
        if now_t - _fb_last.get(cid, 0) < FEEDBACK_GAP or len(_fb_recent) >= FEEDBACK_PER_MIN:
            return jsonify(ok=False, error="too many"), 429
        _fb_last[cid] = now_t
        _fb_recent.append(now_t)
    source = "team" if obj.get("team") else obj.get("source") if obj.get("source") in FEEDBACK_SOURCES else "button"
    snap = latest().get_json() or {}
    with db() as con:
        cur = con.execute("INSERT INTO feedback(ts, answer, level, free_pct, people, used_seats, open, cid, source) VALUES (?,?,?,?,?,?,?,?,?)",
                          (datetime.datetime.now().isoformat(timespec="seconds"), answer, snap.get("level"),
                           snap.get("free_pct"), snap.get("people"), snap.get("used_seats"),
                           1 if snap.get("open") else 0, cid, source))
    return jsonify(ok=True, id=cur.lastrowid)


@app.get("/api/feedback")
def feedback_list():
    """팀용 의견 조회 (토큰 필요): 단계별 응답 수와 최근 의견."""
    if request.headers.get("X-API-Key") != TOKEN:
        return jsonify(error="unauthorized"), 401
    with db() as con:
        summary = [dict(r) for r in con.execute(
            "SELECT source, level, answer, COUNT(*) n FROM feedback GROUP BY source, level, answer ORDER BY source, level, answer")]
        rows = [dict(r) for r in con.execute(
            "SELECT id, ts, source, answer, text, level, free_pct, people, used_seats, open FROM feedback ORDER BY id DESC LIMIT 200")]
    return jsonify(total=sum(r["n"] for r in summary), summary=summary, recent=rows)


# ---------- 접속 수 ----------
_visit_last, _visit_recent, _visit_lock = {}, [], threading.Lock()


@app.post("/api/visit")
def visit():
    """학생용 화면을 열 때 한 번. 같은 기기가 1분 안에 다시 열면 세지 않음."""
    obj = request.get_json(silent=True) or {}
    cid = str(obj.get("cid") or "")[:40]
    if len(cid) < 8:
        return jsonify(ok=False), 400
    ref = str(obj.get("ref") or "")[:60]
    if obj.get("team"):                                   # 팀 기기 (?team=1로 표시)는 세지 않음
        return jsonify(ok=True, counted=False)
    now_t = time.time()
    with _visit_lock:
        _visit_recent[:] = [t for t in _visit_recent if now_t - t < 60]
        if now_t - _visit_last.get(cid, 0) < 60 or len(_visit_recent) >= 600:
            return jsonify(ok=True, counted=False)
        _visit_last[cid] = now_t
        _visit_recent.append(now_t)
    now = datetime.datetime.now()
    with db() as con:
        con.execute("INSERT INTO visits(ts, day, cid, ref) VALUES (?,?,?,?)",
                    (now.isoformat(timespec="seconds"), now.date().isoformat(), cid, ref))
    return jsonify(ok=True, counted=True)


def from_tailnet():
    # 공개 주소(Funnel)로 들어온 요청은 127.0.0.1에서 오고, 팀 기기는 Tailscale 주소(100.x)에서 직접 온다
    return (request.remote_addr or "").startswith("100.")


def _daily_rows():
    """날짜별 요약 (팀 테스트 의견 제외). 최신 날짜가 위."""
    with db() as con:
        v = {r["day"]: r for r in con.execute("""SELECT day, COUNT(DISTINCT cid) u, COUNT(*) n,
                    COUNT(DISTINCT CASE WHEN ref LIKE '%everytime%' THEN cid END) et FROM visits GROUP BY day""")}
        f = {r["d"]: r for r in con.execute("""SELECT substr(ts, 1, 10) d, COUNT(*) n,
                    SUM(answer = '맞아요') a0, SUM(answer = '더 붐볐어요') a1, SUM(answer = '더 한산했어요') a2,
                    SUM(source = 'return') rt, SUM(text IS NOT NULL AND text != '') tx
                    FROM feedback WHERE COALESCE(source, '') != 'team' GROUP BY d""")}
    out = []
    for d in sorted(set(v) | set(f), reverse=True):
        vv, ff = v.get(d), f.get(d)
        n = ff["n"] if ff else 0
        out.append({"day": d, "weekday": "월화수목금토일"[datetime.date.fromisoformat(d).weekday()],
                    "visitors": vv["u"] if vv else 0, "page_loads": vv["n"] if vv else 0, "from_everytime": vv["et"] if vv else 0,
                    "feedback": n, "ok": ff["a0"] if ff else 0, "busier": ff["a1"] if ff else 0, "calmer": ff["a2"] if ff else 0,
                    "ok_pct": round(ff["a0"] / n * 100) if n else None, "via_return": ff["rt"] if ff else 0, "comments": ff["tx"] if ff else 0})
    return out


def table_usage(day):
    """그날 테이블별 이용 횟수와 이용 시간(분). 이용 한 번 = 사용 중이 된 때부터 빈 자리가 될 때까지
    (빈 자리로 바뀌기까지 기다린 TBL_EMPTY초는 뺌). 자리 비움에서 다시 사용 중이 되면 같은 이용으로 본다."""
    with db() as con:
        rows = con.execute("SELECT ts, tid, st, zone FROM table_events WHERE ts >= ? AND ts < ? ORDER BY ts",
                           (day, day + "~")).fetchall()
    sec = lambda ts: datetime.datetime.fromisoformat(ts).timestamp()
    lunch = (sec(day + " 11:00:00"), sec(day + " 14:00:00"))
    started, out = {}, {}
    for r in rows:
        u = out.setdefault(r["tid"], {"tid": r["tid"], "zone": r["zone"], "uses": 0, "minutes": 0.0, "lunch": 0.0, "now": False})
        u["zone"] = r["zone"] or u["zone"]
        if r["st"] == "occupied" and r["tid"] not in started:
            started[r["tid"]] = sec(r["ts"])
        elif r["st"] == "empty" and r["tid"] in started:
            a, b = started.pop(r["tid"]), sec(r["ts"]) - TBL_EMPTY
            u["uses"] += 1
            u["minutes"] += max(0.0, b - a) / 60
            u["lunch"] += max(0.0, min(b, lunch[1]) - max(a, lunch[0])) / 60
    for tid in started:
        out[tid]["now"] = True                         # 아직 이용 중 (오늘)
    key = lambda u: (u["zone"] or "~", u["tid"][:1], int("".join(c for c in u["tid"] if c.isdigit()) or 0))
    return sorted(out.values(), key=key)


def _csv(rows, header, keys, name):
    import csv, io
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    for r in rows:
        w.writerow([r[k] if r[k] is not None else "" for k in keys])
    return Response("﻿" + buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={name}"})


@app.get("/stats/daily.csv")
def stats_daily_csv():
    if not from_tailnet():
        return Response("not found", status=404)
    return _csv(_daily_rows(),
                ["날짜", "요일", "방문자", "페이지 열기", "에브리타임 유입", "의견", "맞아요", "더 붐볐어요", "더 한산했어요", "맞아요 %", "돌아왔을 때 알림 응답", "한 줄 의견"],
                ["day", "weekday", "visitors", "page_loads", "from_everytime", "feedback", "ok", "busier", "calmer", "ok_pct", "via_return", "comments"],
                "babmeok_daily.csv")


@app.get("/stats/feedback.csv")
def stats_feedback_csv():
    if not from_tailnet():
        return Response("not found", status=404)
    with db() as con:
        rows = [dict(r) for r in con.execute("""SELECT ts, COALESCE(source, 'button') source, answer, text, level, free_pct, people, used_seats, open
                                                FROM feedback ORDER BY id""")]
    return _csv(rows, ["시각", "경로", "응답", "한 줄 의견", "그때 단계", "남은 비율 %", "인원", "사용 좌석", "운영 중"],
                ["ts", "source", "answer", "text", "level", "free_pct", "people", "used_seats", "open"], "babmeok_feedback.csv")


@app.get("/stats")
def stats():
    """팀용 접속·의견 통계. Tailscale로 연결된 기기에서 http://<맥미니 Tailscale IP>:8000/stats 로만 열림."""
    if not from_tailnet():
        return Response("not found", status=404)
    now = datetime.datetime.now()
    today = now.date().isoformat()
    sel = request.args.get("day") or today
    try:
        datetime.date.fromisoformat(sel)
    except ValueError:
        sel = today
    daily = _daily_rows()
    with db() as con:
        hours = {r["h"]: r["u"] for r in con.execute("""SELECT CAST(substr(ts, 12, 2) AS INTEGER) h, COUNT(DISTINCT cid) u
                                                          FROM visits WHERE day = ? GROUP BY h""", (sel,))}
        refs = con.execute("""SELECT CASE WHEN ref = '' THEN '직접 입력 · QR' ELSE ref END r, COUNT(DISTINCT cid) u
                              FROM visits GROUP BY r ORDER BY u DESC LIMIT 8""").fetchall()
        total_u = con.execute("SELECT COUNT(DISTINCT cid) FROM visits").fetchone()[0]
        fb = con.execute("SELECT COALESCE(source, 'button') s, level, answer FROM feedback").fetchall()
        texts = con.execute("""SELECT ts, COALESCE(source, 'button') s, answer, level, text FROM feedback
                               WHERE text IS NOT NULL AND text != '' AND COALESCE(source, '') != 'team' AND substr(ts, 1, 10) = ?
                               ORDER BY id DESC""", (sel,)).fetchall()
    by_day = {r["day"]: r for r in daily}
    t_row, y_row = by_day.get(today), by_day.get((now.date() - datetime.timedelta(days=1)).isoformat())
    s_row = by_day.get(sel)
    real = [r for r in fb if r["s"] != "team"]
    team_n = len(fb) - len(real)
    ans_keys = ["맞아요", "더 붐볐어요", "더 한산했어요"]
    ok_pct = round(sum(1 for r in real if r["answer"] == "맞아요") / len(real) * 100) if real else None

    def esc(v):
        return str(v if v is not None else "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")

    def bars(items, hl=None):
        top = max([v for _, v in items] + [1])
        return '<div class="vbars">' + "".join(
            f'<div class="vb{" hl" if lab == hl else ""}" title="{esc(lab)} · {v}명"><span class="v">{v or ""}</span>'
            f'<i style="height:{max(2, v / top * 100) if v else 0:.1f}%"></i><span class="l">{esc(lab)}</span></div>'
            for lab, v in items) + "</div>"

    def hbars(items):
        top = max([v for _, v in items] + [1])
        return "".join(f'<div class="hb"><span class="hl">{esc(lab)}</span><span class="ht"><i style="width:{v / top * 100:.1f}%"></i></span>'
                       f'<b>{v}명</b></div>' for lab, v in items) or '<p class="empty">아직 없어요</p>'

    def stack(rows):
        n = len(rows)
        if not n:
            return '<span class="st empty-st">응답 없음</span>'
        return '<span class="st">' + "".join(
            f'<i class="a{k}" style="width:{sum(1 for r in rows if r["answer"] == a) / n * 100:.1f}%"></i>' for k, a in enumerate(ans_keys)) + "</span>"

    src_label = {"button": "카드 버튼", "return": "돌아왔을 때 알림"}
    level_rows = "".join(f'<div class="lv-row"><span class="chip c-{lv}">{lv}</span>{stack([r for r in real if r["level"] == lv])}'
                         f'<span class="cnt">{sum(1 for r in real if r["level"] == lv)}건</span></div>' for lv in ["여유", "보통", "혼잡"])
    src_rows = "".join(f'<div class="lv-row"><span class="src">{src_label[s]}</span>{stack([r for r in real if r["s"] == s])}'
                       f'<span class="cnt">{sum(1 for r in real if r["s"] == s)}건</span></div>' for s in ["button", "return"])
    ans_cls = {"맞아요": "a0", "더 붐볐어요": "a1", "더 한산했어요": "a2"}
    comments = "".join(
        f'<li><div class="meta"><span>{esc(r[0][11:16])}</span><span class="chip c-{esc(r[3])}">{esc(r[3] or "-")}</span>'
        f'<span class="ans {ans_cls.get(r[2], "")}">{esc(r[2])}</span><span class="via">{src_label.get(r[1], r[1])}</span></div>'
        f'<p>{esc(r[4])}</p></li>' for r in texts) or '<p class="empty">이 날 남긴 한 줄 의견이 없어요</p>'
    hrs = sorted(set(range(7, 21)) | set(hours))
    pct = lambda v: f"{v}%" if v is not None else "—"
    table_rows = "".join(
        f'<tr class="{"sel" if r["day"] == sel else ""}" onclick="location.href=\'?day={r["day"]}\'">'
        f'<td><a href="?day={r["day"]}">{r["day"][5:].replace("-", "/")}</a> <span class="wd">{r["weekday"]}</span></td>'
        f'<td class="n"><b>{r["visitors"]}</b></td><td class="n">{r["page_loads"]}</td><td class="n">{r["from_everytime"]}</td>'
        f'<td class="n"><b>{r["feedback"]}</b></td><td class="n g">{r["ok"]}</td><td class="n r">{r["busier"]}</td><td class="n b">{r["calmer"]}</td>'
        f'<td class="n"><b>{pct(r["ok_pct"])}</b></td><td class="n">{r["via_return"]}</td><td class="n">{r["comments"]}</td></tr>'
        for r in daily) or '<tr><td colspan="11" class="empty">아직 기록이 없어요</td></tr>'
    sel_label = f'{sel[5:].replace("-", "/")}({"월화수목금토일"[datetime.date.fromisoformat(sel).weekday()]})'
    usage = table_usage(sel)
    done = [u for u in usage if u["uses"]]
    avg_min = sum(u["minutes"] for u in done) / sum(u["uses"] for u in done) if done else None
    usage_rows = "".join(
        f'<tr><td><b>{esc(u["tid"])}</b></td><td>{esc(zone_label(u["zone"]) if u["zone"] else "—")}</td>'
        f'<td class="n"><b>{u["uses"]}</b>{" +이용 중" if u["now"] else ""}</td>'
        f'<td class="n">{round(u["minutes"] / u["uses"]) if u["uses"] else "—"}분</td><td class="n">{round(u["minutes"])}분</td>'
        f'<td class="n">{round(u["lunch"] / 180 * 100)}%</td></tr>' for u in usage
    ) or '<tr><td colspan="6" class="empty">이 날 테이블 기록이 없어요</td></tr>'
    usage_note = (f'이용 {sum(u["uses"] for u in done)}번 · 한 번 평균 {round(avg_min)}분'
                  + (f' · 가장 많이 쓰인 테이블 {max(done, key=lambda u: u["uses"])["tid"]}' if done else "")) if done else "아직 끝난 이용이 없어요"

    def delta(a, b):
        if b is None:
            return ""
        d = a - b
        return f'<small class="{"up" if d > 0 else "down" if d < 0 else ""}">어제보다 {"+" if d > 0 else ""}{d}</small>'

    html = f"""<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>밥먹을래말래 · 팀 통계</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/gh/orioncactus/pretendard@v1.3.9/dist/web/static/pretendard.min.css">
<style>
:root{{--ground:#f6f7f9;--surface:#fff;--raised:#f7f8fa;--border:#e6e8eb;--rule:#eef0f2;--text:#111418;--muted:#5f6368;--faint:#80868b;
--data:#2f6db5;--data-2:#9db8de;--data-tint:#eaf1fa;--ok:#247b50;--ok-fill:#2f9e63;--ok-bg:#e4f4ea;--mid:#92400e;--mid-bg:#fdf3e1;--busy:#c4242a;--busy-fill:#d64545;--busy-bg:#fdeaea;--calm:#2f6db5}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--ground);color:var(--text);font-family:Pretendard,-apple-system,"Malgun Gothic",sans-serif;font-size:14px;line-height:20px;word-break:keep-all}}
.wrap{{max-width:1100px;margin:0 auto;padding:24px 20px 40px}}
header{{display:flex;justify-content:space-between;align-items:flex-end;gap:12px;flex-wrap:wrap;margin-bottom:16px}}
h1{{margin:0;font-size:22px;line-height:30px}} h1 span{{color:var(--data)}}
.sub{{color:var(--muted);font-size:13px}}
.dl{{display:flex;gap:8px;flex-wrap:wrap}} .dl a{{display:inline-flex;align-items:center;height:32px;padding:0 12px;border:1px solid #d1d5db;border-radius:6px;background:#fff;color:var(--text);text-decoration:none;font-weight:600;font-size:13px}}
.dl a:hover{{background:var(--raised)}}
.grid{{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px}}
.card{{background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:16px 18px;min-width:0}}
.kpi small{{display:block;color:var(--muted);font-size:13px}}
.kpi b{{display:block;font-size:30px;line-height:38px;font-weight:700;letter-spacing:-.02em;margin-top:2px;font-variant-numeric:tabular-nums}}
.kpi small.up{{color:var(--ok);font-weight:600}} .kpi small.down{{color:var(--busy);font-weight:600}}
.kpi.main b{{color:var(--data)}}
.row2{{display:grid;grid-template-columns:3fr 2fr;gap:12px;margin-top:12px}}
.row1{{margin-top:12px}}
h2{{margin:0 0 12px;font-size:15px;line-height:22px;display:flex;justify-content:space-between;align-items:baseline;gap:8px}}
h2 span{{font-weight:400;color:var(--muted);font-size:12px}}
.tbl{{overflow-x:auto}}
table{{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums;font-size:13px;min-width:760px}}
th{{text-align:right;color:var(--muted);font-weight:600;font-size:12px;padding:8px 10px;border-bottom:1.5px solid var(--text);white-space:nowrap}}
th:first-child,td:first-child{{text-align:left}}
td{{padding:9px 10px;border-bottom:1px solid var(--rule);white-space:nowrap}}
td.n{{text-align:right}} td.g{{color:var(--ok)}} td.r{{color:var(--busy)}} td.b{{color:var(--calm)}}
tr{{cursor:pointer}} tbody tr:hover td{{background:var(--raised)}}
tr.sel td{{background:var(--data-tint)}}
td a{{color:var(--text);font-weight:700;text-decoration:none}} .wd{{color:var(--faint);font-size:12px}}
th.grp{{text-align:center;border-bottom:1px solid var(--border);color:var(--faint);font-weight:500}}
.vbars{{display:flex;align-items:flex-end;gap:6px;height:150px;padding-top:18px}}
.vb{{flex:1;min-width:0;height:100%;display:flex;flex-direction:column;justify-content:flex-end;align-items:center}}
.vb i{{display:block;width:100%;max-width:34px;background:var(--data-2);border-radius:4px 4px 0 0}}
.vb.hl i{{background:var(--data)}}
.vb .v{{font-size:11px;color:var(--muted);height:16px}} .vb .l{{font-size:11px;color:var(--faint);margin-top:4px;white-space:nowrap}}
.hb{{display:grid;grid-template-columns:130px minmax(0,1fr) 48px;align-items:center;gap:10px;height:30px}}
.hb .hl{{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;color:var(--muted)}}
.ht{{height:10px;background:var(--rule);border-radius:999px;overflow:hidden}} .ht i{{display:block;height:100%;background:var(--data);border-radius:999px}}
.hb b{{text-align:right}}
.legend{{display:flex;gap:14px;flex-wrap:wrap;font-size:12px;color:var(--muted);margin:0 0 10px}}
.legend i{{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:5px;vertical-align:-1px}}
.lv-row{{display:grid;grid-template-columns:110px minmax(0,1fr) 44px;align-items:center;gap:10px;height:34px}}
.st{{display:flex;height:14px;border-radius:999px;overflow:hidden;background:var(--rule)}} .st i{{display:block;height:100%}}
.empty-st{{background:none;color:var(--faint);font-size:12px;height:auto}}
.a0{{background:var(--ok-fill)}} .a1{{background:var(--busy-fill)}} .a2{{background:var(--calm)}}
.cnt{{text-align:right;color:var(--muted)}}
.chip{{display:inline-flex;justify-content:center;border-radius:999px;padding:2px 10px;font-size:12px;font-weight:700;width:max-content}}
.c-여유{{background:var(--ok-bg);color:var(--ok)}} .c-보통{{background:var(--mid-bg);color:var(--mid)}} .c-혼잡{{background:var(--busy-bg);color:var(--busy)}}
.src{{color:var(--muted);font-size:13px}}
.note{{margin:10px 0 0;font-size:12px;color:var(--faint)}}
ul.cm{{list-style:none;margin:0;padding:0 4px 0 0;max-height:340px;overflow-y:auto}} ul.cm li{{padding:10px 0;border-top:1px solid var(--rule)}} ul.cm li:first-child{{border-top:0;padding-top:0}}
.meta{{display:flex;gap:8px;align-items:center;flex-wrap:wrap;font-size:12px;color:var(--faint)}}
.ans{{font-weight:700}} .ans.a0{{background:none;color:var(--ok)}} .ans.a1{{background:none;color:var(--busy)}} .ans.a2{{background:none;color:var(--calm)}}
ul.cm p{{margin:4px 0 0}} .empty{{color:var(--faint);margin:0}}
.how{{margin-top:12px;font-size:12px;color:var(--muted);background:var(--raised);border:1px dashed var(--border);border-radius:8px;padding:10px 14px}}
.how code{{background:#fff;border:1px solid var(--border);border-radius:4px;padding:1px 5px}}
@media (max-width:820px){{.grid{{grid-template-columns:repeat(2,minmax(0,1fr))}}.row2{{grid-template-columns:minmax(0,1fr)}}.hb{{grid-template-columns:100px minmax(0,1fr) 40px}}}}
</style></head><body><div class="wrap">
<header><div><h1><span>밥먹을래말래</span> · 팀 통계</h1><div class="sub">팀 기기(Tailscale)에서만 열려요 · {now.strftime("%m/%d %H:%M")} 기준 · 1분마다 새로 고침</div></div>
<div class="dl"><a href="/stats/daily.csv">날짜별 요약 CSV</a><a href="/stats/feedback.csv">의견 원본 CSV</a></div></header>

<section class="grid">
  <div class="card kpi main"><small>오늘 방문자</small><b>{t_row["visitors"] if t_row else 0}명</b>{delta(t_row["visitors"] if t_row else 0, y_row["visitors"] if y_row else None)}</div>
  <div class="card kpi"><small>누적 방문자</small><b>{total_u}명</b><small>기록한 날 {len(daily)}일</small></div>
  <div class="card kpi"><small>의견 응답 (누적)</small><b>{len(real)}건</b><small>오늘 {t_row["feedback"] if t_row else 0}건</small></div>
  <div class="card kpi"><small>"맞아요" 비율 (누적)</small><b style="color:var(--ok)">{pct(ok_pct)}</b><small>화면이 실제와 비슷했다는 응답</small></div>
</section>

<section class="row1 card"><h2>날짜별 정리 <span>날짜를 누르면 아래에 그날 자세히 · 팀 테스트 제외</span></h2>
<div class="tbl"><table>
<thead><tr><th></th><th class="grp" colspan="3">방문</th><th class="grp" colspan="5">의견</th><th class="grp" colspan="2">응답 방식</th></tr>
<tr><th>날짜</th><th>방문자</th><th>페이지 열기</th><th>에타 유입</th><th>의견</th><th>맞아요</th><th>더 붐볐어요</th><th>더 한산했어요</th><th>맞아요 %</th><th>돌아왔을 때</th><th>한 줄 의견</th></tr></thead>
<tbody>{table_rows}</tbody></table></div></section>

<section class="row2">
  <div class="card"><h2>{sel_label} 시간대별 방문자 <span>같은 기기는 1명</span></h2>{bars([(f"{h}시", hours.get(h, 0)) for h in hrs], hl=f"{now.hour}시" if sel == today else None)}
    <p class="note">이 날: 방문자 {s_row["visitors"] if s_row else 0}명 · 의견 {s_row["feedback"] if s_row else 0}건 · 맞아요 {pct(s_row["ok_pct"]) if s_row else "—"}</p></div>
  <div class="card"><h2>{sel_label} 한 줄 의견 <span>{len(texts)}개</span></h2><ul class="cm">{comments}</ul></div>
</section>

<section class="row2">
  <div class="card">
    <h2>화면이 실제와 맞았나 <span>누적 · 그때 화면 단계별</span></h2>
    <div class="legend"><span><i class="a0"></i>맞아요</span><span><i class="a1"></i>더 붐볐어요 (실제가 더 붐빔)</span><span><i class="a2"></i>더 한산했어요 (실제가 더 한산)</span></div>
    {level_rows}
    <p class="note">예: "혼잡"일 때 "더 한산했어요"가 많으면 혼잡 기준이 너무 빡빡하다는 뜻이에요.</p>
  </div>
  <div class="card">
    <h2>어떻게 응답했나 <span>누적 · 경로별</span></h2>{src_rows}
    <p class="note">"돌아왔을 때 알림"은 사이트를 보고 나갔다가 5~90분 뒤 돌아온 사람, 즉 실제로 다녀왔을 가능성이 높은 응답이에요.{f" 팀 테스트 응답 {team_n}건은 제외했어요." if team_n else ""}</p>
    <h2 style="margin-top:16px">어디서 들어왔나 <span>누적 방문자</span></h2>{hbars([(r["r"], r["u"]) for r in refs])}
  </div>
</section>

<section class="row1 card"><h2>{sel_label} 테이블별 이용 <span>{usage_note}</span></h2>
<div class="tbl"><table>
<thead><tr><th>테이블</th><th>구역</th><th>이용 횟수</th><th>평균 이용 시간</th><th>합계</th><th>점심(11~14시) 사용 비율</th></tr></thead>
<tbody>{usage_rows}</tbody></table></div>
<p class="note">이용 한 번 = 사용 중이 된 때부터 빈 자리가 될 때까지예요. 잠깐 자리를 비웠다가 돌아오면 같은 이용으로 셉니다. 빈 자리로 바뀌기까지 기다리는 {TBL_EMPTY // 60}분은 뺐어요.</p></section>

<div class="how">우리 기기의 방문·의견을 빼려면 각 기기에서 한 번만 <code>https://babmeok.tail976ee0.ts.net/?team=1</code> 을 열어 두세요. 그 기기의 방문은 세지 않고, 의견은 "팀 테스트"로 따로 저장돼요.</div>
</div>
<script>setTimeout(() => location.reload(), 60000);</script>
</body></html>"""
    return Response(html, mimetype="text/html")


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


@app.get("/favicon.ico")
def favicon():
    # 브라우저가 기본으로 찾는 주소 (아이콘·홈 화면 파일은 static/ 폴더, Flask가 /static/으로 제공)
    return app.send_static_file("icon-32.png")


@app.get("/")
def index():
    with open(os.path.join(BASE, "index.html"), encoding="utf-8") as f:
        return Response(f.read(), mimetype="text/html")
