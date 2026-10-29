# pi — 라즈베리파이 인원 계산과 전송

식당 안 라즈베리파이 5에서 카메라 4대 영상을 받아 사람 수와 테이블별 사용 여부를 계산하고, 숫자만 맥미니로 보냅니다.
파일은 라즈베리파이 `/home/seung/`에 있고, systemd 서비스로 부팅 시 자동 실행됩니다.

## 파일

| 파일 | 내용 | 서비스 |
|---|---|---|
| `people_count.py` | 카메라 수신 → YOLO11n(NCNN) 사람 검출 → 집계 구역 안 인원 → 테이블 판정 → 기록. 현장 관리 웹(`:5000`)도 이 파일에 들어 있음 | `people-count` |
| `push_counts.py` | `people_log.csv`의 새 줄과 `tables_state.json`을 5초마다 맥미니로 전송. 끊기면 이어서 보냄 | `push-counts` |
| `tables.json` | 카메라별 테이블 영역 51곳 (T1~T47, S1~S4의 꼭짓점·좌석 수). 관리 화면 `/tables`에서 저장 | — |
| `zones.json` | 카메라별 집계 구역 (발 위치가 이 안인 사람만 셈). 관리 화면 `/`의 설정 모드에서 저장 | — |

저장소의 `tables.json`·`zones.json`은 2026-10-29 운영본 기준입니다. 관리 화면에서 다시 저장하면 라즈베리파이 쪽이 바뀌니, 맥미니 백업 폴더(`cafeteria-backup/`)에서 받아 저장소를 갱신하세요.

## people_count.py 안에서 자주 보는 곳

| 위치 | 내용 |
|---|---|
| 파일 위쪽 설정 | `CAMS`(카메라 주소·계정, 저장소에서는 가림), `IMG`·`CONF`·`NMS`, 피크/평상시 주기, `OPEN_HOURS` |
| `TBL_WIN` · `TBL_MIN_HITS` · `TBL_AWAY` · `TBL_EMPTY` | 테이블 판정 기준: 30초 안에 3번 보이면 사용 중, 60초 안 보이면 자리 비움, 240초 안 보이면 빈 자리 |
| `class TableTracker` | 테이블 상태 판정. 10/29 수정: 30초 안에 2번 이상 보였을 때만 "마지막으로 본 시각"을 갱신 (한 번 스친 오탐이 자리 비움을 붙잡지 않게) |
| `KIOSK_CAMS` · `KIOSK_N` · `KIOSK_RATE` | cam4는 좌석이 아니라 키오스크 줄을 셈. 줄 인원과 1분당 처리 인원으로 대기 시간 추정 |
| `PAGE` · `TABLE_PAGE` | 현장 관리 화면 `/`와 `/tables`의 HTML. 학생용 웹과 같은 디자인·밝은/어두운 화면 |

## 만드는 기록 (라즈베리파이에만 있고 저장소에는 없음)

| 파일 | 내용 |
|---|---|
| `people_log.csv` | 시각, 카메라별 인원, 합계 (서버로 보내는 원본) |
| `detections/*.jsonl` | 회차별 사람 박스 좌표 (분석용, 영상 아님) |
| `tables_state.json` / `tables_log.jsonl` | 지금 테이블 상태 / 상태가 바뀔 때마다 한 줄 |
| `zones_history.jsonl` / `tables_history.jsonl` | 구역·테이블 영역을 저장할 때마다 남는 이력 |

## 반영 방법

저장소 `people_count.py`는 카메라 IP·비밀번호를 가려 두었으므로 **라즈베리파이 파일에 그대로 덮어쓰면 카메라 연결이 끊깁니다.** 바뀐 부분만 적용하는 패치 방식으로 반영합니다 ([HANDOFF 14장](../docs/HANDOFF.md#코드-수정-반영-저장소--기기)).

```bash
# 적용 후 확인과 재시작 (sudo 비밀번호는 seung 계정)
python3 -m py_compile ~/people_count.py && sudo systemctl restart people-count
systemctl is-active people-count
sudo journalctl -u people-count -n 10 --no-pager
```
