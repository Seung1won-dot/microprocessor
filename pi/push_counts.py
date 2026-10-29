import csv, json, os, time, urllib.request

SERVER = "http://<맥미니 Tailscale IP>:8000/api/ingest"
HOME = "/home/seung"
TOKEN = open(f"{HOME}/.push_token").read().strip()
LOG = f"{HOME}/people_log.csv"
STATE = f"{HOME}/.push_offset"
BATCH = 500
STATE_FILE = f"{HOME}/tables_state.json"
STATE_URL = SERVER.replace("/api/ingest", "/api/state")
INTERVAL = 5

def load_offset():
    try:
        return int(open(STATE).read())
    except Exception:
        return 0

def save_offset(o):
    with open(STATE, "w") as f:
        f.write(str(o))

def post(rows, url=SERVER):
    req = urllib.request.Request(
        url, data=json.dumps(rows).encode(),
        headers={"Content-Type": "application/json", "X-API-Key": TOKEN})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r)

def main():
    offset = load_offset()
    header = None
    last_m = 0
    while True:
        try:
            if os.path.getsize(LOG) < offset:
                offset = 0
            with open(LOG, "rb") as f:
                if header is None:
                    header = next(csv.reader([f.readline().decode()]))
                f.seek(offset)
                chunk = f.read()
            chunk = chunk[:chunk.rfind(b"\n") + 1]
            pos, batch = offset, []
            for raw in chunk.splitlines(keepends=True):
                pos += len(raw)
                vals = next(csv.reader([raw.decode("utf-8", "replace")]), [])
                if vals and vals[0] != header[0]:
                    batch.append(dict(zip(header, vals)))
                if len(batch) >= BATCH:
                    post(batch)
                    offset = pos
                    save_offset(offset)
                    batch = []
            if batch:
                res = post(batch)
                print("sent", len(batch), res, flush=True)
            offset = pos
            save_offset(offset)
            try:
                m = os.path.getmtime(STATE_FILE)
                if m != last_m:
                    with open(STATE_FILE) as f:
                        obj = json.load(f)
                    post(obj, STATE_URL)
                    last_m = m
            except FileNotFoundError:
                pass
        except Exception as e:
            print("push error:", e, flush=True)
        time.sleep(INTERVAL)

if __name__ == "__main__":
    main()
