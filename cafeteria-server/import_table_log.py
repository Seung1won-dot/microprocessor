"""라즈베리파이의 테이블 상태 기록(tables_log.jsonl)을 서버 DB의 table_events로 한 번 옮긴다.
서버가 직접 기록하기 시작한 때(table_events의 가장 이른 시각) 이전 것만 넣으므로 여러 번 돌려도 겹치지 않는다.

    venv/bin/python import_table_log.py ../cafeteria-backup/tables_log.jsonl ../cafeteria-backup/tables.json
"""
import json, sqlite3, sys, os

log_path, tables_path = sys.argv[1], sys.argv[2]
db = sqlite3.connect(os.path.join(os.path.dirname(os.path.abspath(__file__)), "counts.db"))
db.execute("""CREATE TABLE IF NOT EXISTS table_events(
    ts TEXT, tid TEXT, st TEXT, n INTEGER, zone TEXT, PRIMARY KEY(ts, tid))""")
first = db.execute("SELECT MIN(ts) FROM table_events").fetchone()[0] or "~"

# 구역 = 테이블 전체가 보이는 첫 카메라 (라즈베리파이 table_registry와 같은 규칙, 지금 등록 기준)
tables = json.load(open(tables_path))
zone, full = {}, set()
for cam in sorted(tables):
    if cam == "cam4":
        continue
    for t in tables[cam]:
        tid = t["id"]
        if tid not in zone or (not t.get("partial") and tid not in full):
            zone[tid] = cam
        if not t.get("partial"):
            full.add(tid)

rows = []
for line in open(log_path):
    try:
        r = json.loads(line)
    except ValueError:
        continue
    if r["t"] >= first:
        continue
    for tid, st in r["changes"].items():
        rows.append((r["t"], tid, st, None, zone.get(tid)))
with db:
    db.executemany("INSERT OR IGNORE INTO table_events VALUES (?,?,?,?,?)", rows)
print(f"{len(rows)} events before {first}")
