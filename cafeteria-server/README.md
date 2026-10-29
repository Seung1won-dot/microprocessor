# cafeteria-server — 맥미니 서버와 학생용 웹

라즈베리파이가 보낸 숫자를 받아 저장하고, 학생용 혼잡도 웹을 보여줍니다.
맥미니 `~/3학년 2학기/캡스톤/cafeteria-server/`에서 gunicorn으로 포트 8000에 떠 있습니다 (launchd 자동 실행).

## 파일

| 파일 | 내용 | 고치면 |
|---|---|---|
| `server.py` | Flask 서버. 데이터 받기(`/api/ingest`, `/api/state`), 조회 API, 웹 화면 제공 | 맥미니에 복사 후 **서버 재시작** |
| `index.html` | 학생용 웹 화면 전체 (HTML·CSS·JS 한 파일, 외부 라이브러리 없음) | 맥미니에 복사만 하면 됨 (재시작 불필요) |
| `backup_pi.sh` | 라즈베리파이 기록을 `../cafeteria-backup/`으로 rsync 백업 | 맥미니에 복사 |
| `launchd/kr.sch.cafeteria.plist` | 서버 자동 실행 (부팅 시 시작, 죽으면 재시작) | `~/Library/LaunchAgents/`에 복사 후 다시 load |
| `launchd/kr.sch.cafeteria.backup.plist` | 백업 30분마다 실행 | 위와 같음 |
| `bench_ncnn.py` | OpenCV DNN과 NCNN 속도·검출 수 비교 실험 (10/27, 라즈베리파이에서 실행) | 운영과 무관 |
| `layout.html`, `layout.json` | 예전 배치도 편집기(`/layout`). **지금은 안 씀** — 배치도는 `index.html`의 `LAYOUT`에 직접 있음 | — |

맥미니에만 있고 저장소에는 없는 것: `token.txt`(전송 토큰), `counts.db`(SQLite 기록), `state_latest.json`, `venv/`, `logs/`.

## API

| 주소 | 내용 |
|---|---|
| `POST /api/ingest` | 라즈베리파이 인원 기록 저장 (토큰 + 라즈베리파이 IP만 허용) |
| `POST /api/state` | 테이블별 상태·키오스크 대기 저장 |
| `GET /api/latest` | 지금 혼잡 단계, 인원, 빈 테이블, 테이블별 상태, 키오스크 |
| `GET /api/slots` | 운영 시간 전체(08:00~19:30) 15분 칸별 오늘 인원과 평소(지난 평일 평균·최소·최대) |
| `GET /api/today` | 오늘 기록 (최근 10분 추세 계산용) |
| `GET /api/history` | 최근 평일 날짜별 15분 칸 인원 ("최근 5일, 가장 붐빈 때") |
| `POST /api/feedback` | 이용자 의견 "실제랑 비슷했나요?" (맞아요 / 더 붐볐어요 / 더 한산했어요 + 한 줄 의견). 그때 화면 값과 함께 `feedback` 표에 저장. 같은 기기 10분에 한 번, 전체 1분 60건 상한 |
| `GET /api/feedback` | 팀용 의견 조회 (토큰 필요): 경로·단계별 응답 수, 최근 200건 |
| `POST /api/visit` | 접속 수: 학생용 화면을 열 때 한 번 (익명 기기 id, 들어온 사이트 도메인. IP 저장 안 함, 같은 기기 1분 안 재방문은 안 셈, 예시 화면 제외) |
| `GET /stats` | **팀용 통계 화면** — 오늘·날짜별·시간대별 방문자, 들어온 곳, 의견 경로·단계별, 한 줄 의견. Tailscale 기기에서 `http://<맥미니 Tailscale IP>:8000/stats` 로만 열림 (공개 주소로는 404) |

## index.html 구조

화면 위에서부터: 지금 혼잡도와 게이지 → 빈 자리 · 식당 예상 인원 · 주문 대기 → 시간대별 그래프(오늘 막대 + 평소 옅은 막대) → 최근 5일 가장 붐빈 때(접힘) → 자리 현황 배치도 → 개인정보 안내 · 서비스 소개.

자주 고치는 곳:
- **배치도:** 스크립트의 `const LAYOUT = {...}` — 테이블 좌표(`seats`), 구역(`zones`), 카메라 밖(`blind`), 배식대·출입구·기둥(`fixtures`). 테이블 번호와 좌석 수는 라즈베리파이 `tables.json`과 같아야 합니다.
- **문구:** 혼잡 단계별 문장은 `COPY`, 키오스크 카메라 꺼지는 시각은 `KIOSK_OFF`.
- **예시 데이터로 보기:** 주소 끝에 `?demo=1`, 시각을 바꿔 보려면 `?demo=1&at=16:25`. 실제 데이터 없이 화면을 확인할 수 있습니다.
- **어두운 화면으로 열기:** `?dark=1`
- **처음 온 사람 튜토리얼·돌아왔을 때 묻기:** 스크립트의 `TOUR`(3단계 안내 문구), `AWAY_MIN`·`AWAY_MAX`(5분~90분 떠났다 오면 묻기). 예시 화면에서 `&notour`는 튜토리얼 끄기, `&away=10`은 10분 뒤 돌아온 상황

## 반영 방법

저장소 `server.py`는 라즈베리파이 IP를 `<라즈베리파이 Tailscale IP>`로 가려 두었습니다. **그대로 덮어쓰면 데이터 수신이 막히니** 맥미니 쪽 실제 값을 유지하세요. 자세한 순서는 [HANDOFF 14장](../docs/HANDOFF.md#코드-수정-반영-저장소--기기).

```bash
# 맥미니에서: 웹 화면만 바꿀 때
cp index.html ~/"3학년 2학기"/캡스톤/cafeteria-server/index.html
# server.py를 바꿨을 때는 복사 후 재시작
launchctl kickstart -k gui/$(id -u)/kr.sch.cafeteria
```
