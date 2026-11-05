"""공개용 사본 만들기: 코드와 프로젝트 설명만 골라 이름·학교·수업·주소를 지운 폴더를 만든다.

    python3 tools/make_public.py ../babmeok-public

- 이 저장소(비공개)의 기록은 가져가지 않는다. 결과 폴더에서 새로 git init 해서 올린다.
- README.md는 tools/public_README.md를 그대로 쓴다.
- 마지막에 금지어 검사를 해서 하나라도 남아 있으면 실패(종료 코드 1)한다.
"""
import os, re, shutil, sys

SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DST = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else os.path.join(SRC, "..", "babmeok-public"))

FILES = {  # 원본 경로 -> 공개본 경로
    "pi/people_count.py": "pi/people_count.py",
    "pi/push_counts.py": "pi/push_counts.py",
    "pi/compare_shadow.py": "pi/compare_shadow.py",
    "pi/tables.json": "pi/tables.json",
    "pi/zones.json": "pi/zones.json",
    "cafeteria-server/server.py": "cafeteria-server/server.py",
    "cafeteria-server/index.html": "cafeteria-server/index.html",
    "cafeteria-server/layout.html": "cafeteria-server/layout.html",
    "cafeteria-server/layout.json": "cafeteria-server/layout.json",
    "cafeteria-server/import_table_log.py": "cafeteria-server/import_table_log.py",
    "cafeteria-server/bench_ncnn.py": "cafeteria-server/bench_ncnn.py",
    "cafeteria-server/backup_pi.sh": "cafeteria-server/backup_pi.sh",
    "cafeteria-server/launchd/kr.sch.cafeteria.plist": "cafeteria-server/launchd/babmeok.server.plist",
    "cafeteria-server/launchd/kr.sch.cafeteria.backup.plist": "cafeteria-server/launchd/babmeok.backup.plist",
    "docs/혼잡도_판단_방식.md": "docs/혼잡도_판단_방식.md",
    "혼잡도-계산.md": "docs/혼잡도-계산.md",
    "tools/public_README.md": "README.md",
    "tools/public_gitignore": ".gitignore",
}
BINARY = {"tools/public_screenshot.png": "docs/screenshot.png"}

# 순서가 중요하다 (긴 것 먼저)
REPLACE = [
    ("/Users/seung1_won/3학년 2학기/캡스톤/cafeteria-server", "/path/to/cafeteria-server"),
    ('DEST="$HOME/3학년 2학기/캡스톤/cafeteria-backup"', 'DEST="$HOME/cafeteria-backup"'),
    ('HOME = "/home/seung"', 'HOME = os.path.expanduser("~")'),
    ("seung@<라즈베리파이 Tailscale IP>", "pi@<라즈베리파이 주소>"),
    ("kr.sch.cafeteria.backup", "local.babmeok.backup"),
    ("kr.sch.cafeteria", "local.babmeok.server"),
    ("https://babmeok.tail976ee0.ts.net", "https://<서버 주소>"),
    ("순천향대학교 3학년 학생들이 캡스톤디자인 수업 목적으로 시범 운영해요.", "대학생 프로젝트로 시범 운영했어요."),
    ("밥먹을래말래 · 순천향대학교 캡스톤디자인 2026", "밥먹을래말래 · 2026"),
    (" — 실제 값은 라즈베리파이 원본에만 (HANDOFF 18장)", " — 실제 값은 기기에만 두고 공개하지 않음"),
    ("계산식의 근거는 팀원이 정리한 [혼잡도-계산.md](../혼잡도-계산.md)입니다.", "계산식의 근거는 [혼잡도-계산.md](혼잡도-계산.md)입니다."),
    ("## 발표용 한 줄", "## 한 줄 요약"),
    ("학생회관 1층 식당", "학생 식당"),
    ("학생회관 안내 기준", "식당 안내 기준"),
    ("학생회관 식당", "학생 식당"),
    ("대한짜장", "배식대 A"), ("수오미엔", "배식대 B"), ("하즈벤", "배식대 C"), ("공여사맛집", "배식대 D"),
]

FORBIDDEN = re.compile(
    r"이승원|팀원|승원|팀원|지도교수|교수|캡스톤|수업|순천향|SCH\b|sch\.ac|kr\.sch|학생회관|HANDOFF|"
    r"seung|Seung|(아이디)|(아이디)|(아이디)|oserts|tail976|\.ts\.net|/Users/|"
    r"\b100\.\d+\.\d+\.\d+|192\.168\.|10\.42\.|"
    r"대한짜장|수오미엔|하즈벤|공여사|rtsp://[^\s\"'<{]+:[^\s\"'<@{]+@")
ALLOW = {"README.md": re.compile(r"학술제")}   # README의 수상 문구만 허용 (학교 이름 없이)

if os.path.exists(DST):
    for name in os.listdir(DST):
        if name != ".git":
            p = os.path.join(DST, name)
            shutil.rmtree(p) if os.path.isdir(p) else os.remove(p)
os.makedirs(DST, exist_ok=True)

for src, dst in FILES.items():
    s = open(os.path.join(SRC, src), encoding="utf-8").read()
    for a, b in REPLACE:
        s = s.replace(a, b)
    out = os.path.join(DST, dst)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    open(out, "w", encoding="utf-8").write(s)
for src, dst in BINARY.items():
    if os.path.exists(os.path.join(SRC, src)):
        os.makedirs(os.path.dirname(os.path.join(DST, dst)), exist_ok=True)
        shutil.copy(os.path.join(SRC, src), os.path.join(DST, dst))

bad = 0
for dst in FILES.values():
    for i, line in enumerate(open(os.path.join(DST, dst), encoding="utf-8"), 1):
        for m in FORBIDDEN.finditer(line):
            print(f"금지어 {dst}:{i}: {m.group(0)[:20]}")
            bad += 1
print(f"{DST} — 파일 {len(FILES) + len(BINARY)}개, 금지어 {bad}건")
sys.exit(1 if bad else 0)
