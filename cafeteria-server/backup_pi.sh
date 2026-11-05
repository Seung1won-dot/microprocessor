#!/bin/bash
DEST="$HOME/3학년 2학기/캡스톤/cafeteria-backup"
PI="seung@<라즈베리파이 Tailscale IP>"
mkdir -p "$DEST"
echo "=== $(date '+%F %T') 백업 시작"
for p in detections shadow people_log.csv zones.json zones_history.jsonl tables.json tables_history.jsonl tables_state.json tables_log.jsonl people_count.py; do
  /usr/bin/rsync -az --timeout=60 \
    -e "/usr/bin/ssh -o BatchMode=yes -o ConnectTimeout=20" \
    "$PI:$p" "$DEST/" && echo "ok  $p" || echo "실패 $p"
done
