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
    # 이용자 의견: 그때 화면이 보여준 값과 함께 저장 (이름·연락처·IP는 저장하지 않음)
    con.execute("""CREATE TABLE IF NOT EXISTS feedback(
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, answer TEXT, text TEXT,
        level TEXT, free_pct INTEGER, people INTEGER, used_seats INTEGER, open INTEGER, cid TEXT)""")
    # 접속 기록: 페이지를 열 때 한 줄 (익명 기기 id, 들어온 사이트 도메인만. IP는 저장하지 않음)
    con.execute("""CREATE TABLE IF NOT EXISTS visits(
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, day TEXT, cid TEXT, ref TEXT)""")
    if "source" not in [r[1] for r in con.execute("PRAGMA table_info(feedback)")]:
        con.execute("ALTER TABLE feedback ADD COLUMN source TEXT")     # button / return (돌아왔을 때 알림)

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
    return jsonify(status="stale" if age > STALE_SEC else "ok", open=is_open(now),
                   ts=r["ts"], people=people, total=people,
                   cams={k: v for k, v in cams.items() if k not in KIOSK_CAMS},
                   kiosk=kiosk, tables=(st.get("tables") if fresh_state else None), summary=summ,
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
    source = obj.get("source") if obj.get("source") in FEEDBACK_SOURCES else "button"
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


@app.get("/stats")
def stats():
    """팀용 접속·의견 통계. Tailscale로 연결된 기기에서 http://<맥미니 Tailscale IP>:8000/stats 로만 열림."""
    if not from_tailnet():
        return Response("not found", status=404)
    today = datetime.date.today().isoformat()
    with db() as con:
        days = con.execute("""SELECT day, COUNT(DISTINCT cid) u, COUNT(*) n FROM visits
                              GROUP BY day ORDER BY day DESC LIMIT 14""").fetchall()
        hours = con.execute("""SELECT substr(ts, 12, 2) h, COUNT(DISTINCT cid) u, COUNT(*) n FROM visits
                               WHERE day = ? GROUP BY h ORDER BY h""", (today,)).fetchall()
        refs = con.execute("""SELECT CASE WHEN ref = '' THEN '(직접 · QR)' ELSE ref END r, COUNT(DISTINCT cid) u
                              FROM visits GROUP BY r ORDER BY u DESC LIMIT 8""").fetchall()
        total = con.execute("SELECT COUNT(DISTINCT cid), COUNT(*) FROM visits").fetchone()
        fb = con.execute("""SELECT COALESCE(source, 'button') s, answer, COUNT(*) n FROM feedback
                            GROUP BY s, answer ORDER BY s, answer""").fetchall()
        fb_level = con.execute("""SELECT level, answer, COUNT(*) n FROM feedback WHERE level IS NOT NULL
                                  GROUP BY level, answer ORDER BY level, answer""").fetchall()
        texts = con.execute("""SELECT ts, COALESCE(source, 'button'), answer, level, text FROM feedback
                               WHERE text IS NOT NULL AND text != '' ORDER BY id DESC LIMIT 30""").fetchall()

    def esc(v):
        return (str(v if v is not None else "")).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    def table(head, rows):
        th = "".join(f"<th>{h}</th>" for h in head)
        tr = "".join("<tr>" + "".join(f"<td>{esc(c)}</td>" for c in r) + "</tr>" for r in rows) or f"<tr><td colspan='{len(head)}' class='m'>아직 없어요</td></tr>"
        return f"<table><tr>{th}</tr>{tr}</table>"

    src = {"button": "카드 버튼", "return": "돌아왔을 때 알림"}
    today_row = next((r for r in days if r["day"] == today), None)
    html = f"""<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>밥먹을래말래 · 팀 통계</title><meta http-equiv="refresh" content="60">
<style>body{{font-family:Pretendard,-apple-system,"Malgun Gothic",sans-serif;margin:0;padding:24px;background:#f6f7f9;color:#111418}}
h1{{font-size:22px;margin:0 0 4px}} h2{{font-size:16px;margin:24px 0 8px}} .m{{color:#5f6368}}
.k{{display:flex;gap:12px;flex-wrap:wrap;margin-top:16px}} .k div{{background:#fff;border:1px solid #e6e8eb;border-radius:8px;padding:12px 16px;min-width:140px}}
.k b{{display:block;font-size:26px;color:#2f6db5}} table{{border-collapse:collapse;background:#fff;border:1px solid #e6e8eb;border-radius:8px;min-width:320px}}
th,td{{padding:6px 12px;border-bottom:1px solid #eef0f2;text-align:left;font-size:14px}} th{{color:#5f6368;font-weight:600}}</style></head><body>
<h1>밥먹을래말래 · 팀 통계</h1><div class="m">팀 기기(Tailscale)에서만 열려요 · 1분마다 새로 고침 · 접속 수는 같은 기기를 1명으로 셉니다 (예시 화면 제외)</div>
<div class="k"><div><span class="m">오늘 방문자</span><b>{today_row["u"] if today_row else 0}명</b></div>
<div><span class="m">오늘 페이지 열기</span><b>{today_row["n"] if today_row else 0}회</b></div>
<div><span class="m">누적 방문자</span><b>{total[0]}명</b></div>
<div><span class="m">의견 응답</span><b>{sum(r["n"] for r in fb)}건</b></div></div>
<h2>날짜별</h2>{table(["날짜", "방문자", "페이지 열기"], [(r["day"], r["u"], r["n"]) for r in days])}
<h2>오늘 시간대별</h2>{table(["시", "방문자", "페이지 열기"], [(r["h"] + "시", r["u"], r["n"]) for r in hours])}
<h2>어디서 들어왔나 (누적 방문자)</h2>{table(["들어온 곳", "방문자"], [(r["r"], r["u"]) for r in refs])}
<h2>의견: 경로별</h2>{table(["경로", "응답", "건수"], [(src.get(r["s"], r["s"]), r["answer"], r["n"]) for r in fb])}
<h2>의견: 화면 단계별</h2>{table(["그때 화면", "응답", "건수"], [(r["level"], r["answer"], r["n"]) for r in fb_level])}
<h2>한 줄 의견 (최근 30개)</h2>{table(["시각", "경로", "응답", "그때 화면", "의견"], [(r[0][5:16].replace("T", " "), src.get(r[1], r[1]), r[2], r[3], r[4]) for r in texts])}
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


@app.get("/")
def index():
    with open(os.path.join(BASE, "index.html"), encoding="utf-8") as f:
        return Response(f.read(), mimetype="text/html")
