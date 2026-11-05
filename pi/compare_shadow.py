"""YOLO11n(지금 쓰는 모델)과 YOLO26n(그림자 실행) 비교 요약.

people_count.py가 한 회차에 카메라 하나씩, 같은 프레임을 두 모델에 넣고 shadow/shadow_YYYY-MM-DD.jsonl에
사람마다 [발 x, 발 y, 점수, 집계 구역 안(1/0)]을 남긴다. 이 스크립트는 그 기록으로
  1) 카메라별로 같은 기준(지금 카메라 기준 점수)에서 구역 안 인원 평균이 얼마나 다른지
  2) YOLO26n이 지금 모델과 같은 인원을 내려면 기준 점수를 얼마로 둬야 하는지
  3) 한 장 처리 시간
을 보여 준다. 정답(실제 인원)이 없으므로 "어느 쪽이 더 정확한가"는 수기 계수와 함께 봐야 한다.

    python3 compare_shadow.py shadow/shadow_2026-11-05.jsonl [더 많은 파일 ...] [--from 11:00 --to 14:00]
"""
import json, sys
from collections import defaultdict

args = sys.argv[1:]
t_from = t_to = None
if "--from" in args:
    i = args.index("--from"); t_from = args[i + 1]; del args[i:i + 2]
if "--to" in args:
    i = args.index("--to"); t_to = args[i + 1]; del args[i:i + 2]

THS = [0.05, 0.08, 0.1, 0.15, 0.2, 0.25, 0.3, 0.4]
rows = defaultdict(list)
for path in args:
    for line in open(path):
        try:
            r = json.loads(line)
        except ValueError:
            continue
        hm = r["t"][11:16]
        if (t_from and hm < t_from) or (t_to and hm >= t_to):
            continue
        rows[r["cam"]].append(r)


def n_in(people, th):
    return sum(1 for p in people if p[2] > th and p[3])


def mean(xs):
    return sum(xs) / len(xs) if xs else float("nan")


print(f"{'카메라':6} {'회차':>5} {'기준':>5} {'11n 평균':>8} {'26n 평균':>8} {'차이':>6} {'|차이| 평균':>9} {'26n 더 많음':>9} {'26n ms':>7}")
for cam in sorted(rows):
    rs = rows[cam]
    th = rs[0]["th"]
    a = [n_in(r["main"], th) for r in rs]
    b = [n_in(r["shadow"], th) for r in rs]
    more = sum(1 for x, y in zip(a, b) if y > x) / len(rs) * 100
    print(f"{cam:6} {len(rs):>5} {th:>5} {mean(a):>8.2f} {mean(b):>8.2f} {mean(b) - mean(a):>+6.2f} "
          f"{mean([abs(x - y) for x, y in zip(a, b)]):>9.2f} {more:>8.0f}% {mean([r['ms'] for r in rs]):>7.0f}")

print("\nYOLO26n 기준 점수별 구역 안 인원 평균 (오른쪽 끝 = 지금 모델, 지금 기준)")
print(f"{'카메라':6} " + " ".join(f"{t:>6}" for t in THS) + f" {'11n':>6}")
for cam in sorted(rows):
    rs = rows[cam]
    th = rs[0]["th"]
    line = " ".join(f"{mean([n_in(r['shadow'], t) for r in rs]):>6.2f}" for t in THS)
    print(f"{cam:6} {line} {mean([n_in(r['main'], th) for r in rs]):>6.2f}")
