"""
OpenCV DNN(ONNX) vs NCNN 처리 시간·검출 수 비교
- 카메라 화면은 메모리에서만 사용하고 저장하지 않습니다.
- 운영 중인 people_count.py는 건드리지 않습니다 (설정값만 읽어 옴).

사용 예:
  python3 bench_ncnn.py --cam cam2
  python3 bench_ncnn.py --cam cam2 --img 960 --onnx ~/yolo11n_960.onnx --ncnn ~/yolo11n_960_ncnn_model
"""
import argparse, os, re, time
import numpy as np

os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")
import cv2

HOME = os.path.expanduser("~")
p = argparse.ArgumentParser()
p.add_argument("--cam", default="cam2", help="비교에 쓸 카메라 (신호가 좋은 cam2·cam3 추천)")
p.add_argument("--stream", default="stream1")
p.add_argument("--img", type=int, default=1280)
p.add_argument("--onnx", default=f"{HOME}/yolo11n_1280.onnx")
p.add_argument("--ncnn", default=f"{HOME}/yolo11n_1280_ncnn_model")
p.add_argument("--frames", type=int, default=3, help="비교할 장면 수")
p.add_argument("--runs", type=int, default=5, help="장면마다 반복 횟수")
p.add_argument("--threads", type=int, default=4)
a = p.parse_args()

# people_count.py에서 카메라 주소와 기준값 읽기 (비밀번호는 출력하지 않음)
src = open(f"{HOME}/people_count.py", encoding="utf-8").read()
m = re.search(r'"%s"\s*:\s*\(\s*"([^"]+)"\s*,\s*"([^"]+)"\s*,\s*"([^"]+)"' % re.escape(a.cam), src)
if not m:
    raise SystemExit(f"people_count.py에서 {a.cam} 설정을 찾지 못했어요")
ip, user, pw = m.groups()
def num(name, default):
    mm = re.search(rf"^{name}\s*=\s*([0-9.]+)", src, re.M)
    return float(mm.group(1)) if mm else default
CONF, NMS = num("CONF", 0.2), num("NMS", 0.6)
url = f"rtsp://{user}:{pw}@{ip}:554/{a.stream}"
print(f"[설정] {a.cam} ({ip}, {a.stream}) · 입력 {a.img} · CONF {CONF} · NMS {NMS} · 스레드 {a.threads}")

# 장면 받아오기 (메모리에만 보관)
cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
if not cap.isOpened():
    raise SystemExit("카메라에 연결하지 못했어요. 카메라 이름과 연결 상태를 확인하세요")
frames = []
t_end = time.time() + 30
while len(frames) < a.frames and time.time() < t_end:
    for _ in range(15):          # 버퍼에 쌓인 옛 장면 버리기
        cap.grab()
    ok, f = cap.read()
    if ok and f is not None:
        frames.append(f)
        time.sleep(1.5)
cap.release()
if not frames:
    raise SystemExit("장면을 받지 못했어요")
H, W = frames[0].shape[:2]
print(f"[장면] {len(frames)}장 · {W}x{H}")

def prep(frame):
    return cv2.dnn.blobFromImage(frame, 1 / 255.0, (a.img, a.img), swapRB=True, crop=False)

def post(out):
    out = np.squeeze(np.asarray(out, dtype=np.float32))
    if out.shape[0] != 84 and out.shape[-1] == 84:
        out = out.T
    sc = out[4]                                   # class 0 = person
    k = sc >= CONF
    cx, cy, bw, bh, s = out[0][k], out[1][k], out[2][k], out[3][k], sc[k]
    sx, sy = W / a.img, H / a.img
    boxes = [[float((x - w / 2) * sx), float((y - h / 2) * sy), float(w * sx), float(h * sy)]
             for x, y, w, h in zip(cx, cy, bw, bh)]
    idx = cv2.dnn.NMSBoxes(boxes, s.tolist(), CONF, NMS) if boxes else []
    idx = np.array(idx).flatten().astype(int) if len(idx) else np.array([], int)
    return len(idx), (float(s[idx].mean()) if len(idx) else 0.0)

def bench(name, infer):
    infer(prep(frames[0]))                        # 예열
    times, counts, scores = [], [], []
    for f in frames:
        blob = prep(f)
        for _ in range(a.runs):
            t = time.perf_counter()
            out = infer(blob)
            times.append(time.perf_counter() - t)
        c, sm = post(out)
        counts.append(c); scores.append(sm)
    ms = np.array(times) * 1000
    print(f"{name:<14} 평균 {ms.mean():7.0f} ms  (최소 {ms.min():.0f} / 최대 {ms.max():.0f})"
          f"  ·  장면별 인원 {counts}  ·  평균 점수 {np.mean(scores):.2f}")
    return ms.mean(), counts

results = {}

# 1) 지금 방식: OpenCV DNN + ONNX
cv2.setNumThreads(a.threads)
dnn = cv2.dnn.readNetFromONNX(a.onnx)
def run_dnn(blob):
    dnn.setInput(blob)
    return dnn.forward()
results["OpenCV DNN"] = bench("OpenCV DNN", run_dnn)

# 2) NCNN
try:
    import ncnn
    net = ncnn.Net()
    net.opt.use_vulkan_compute = False
    net.opt.num_threads = a.threads
    net.load_param(os.path.join(a.ncnn, "model.ncnn.param"))
    net.load_model(os.path.join(a.ncnn, "model.ncnn.bin"))
    in_name = net.input_names()[0]
    out_name = sorted(net.output_names())[0]
    def run_ncnn(blob):
        ex = net.create_extractor()
        ex.input(in_name, ncnn.Mat(np.ascontiguousarray(blob[0])))
        _, out = ex.extract(out_name)
        return np.array(out)
    results["NCNN"] = bench("NCNN", run_ncnn)
except ImportError:
    print("NCNN: ncnn 라이브러리가 없어요 (pip3 install --user --break-system-packages ncnn)")
except Exception as e:
    print(f"NCNN 실행 실패: {e}")

if "NCNN" in results:
    d, n = results["OpenCV DNN"][0], results["NCNN"][0]
    same = results["OpenCV DNN"][1] == results["NCNN"][1]
    print(f"\n[결과] NCNN이 {d / n:.1f}배 {'빠름' if n < d else '느림'} "
          f"(장당 {d:.0f} → {n:.0f} ms) · 검출 인원 {'같음' if same else '다름 → 차이 확인 필요'}")
    print(f"       카메라 4대 기준 예상: 1회 약 {d * 4 / 1000:.1f}초 → {n * 4 / 1000:.1f}초")
