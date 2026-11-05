# 설치 가이드

카메라 4대 → 라즈베리파이 5 → 서버 구성을 처음부터 다시 만들 때 필요한 순서입니다. 주소·계정·비밀번호는 모두 `<...>` 자리 표시로 적었습니다.

## 1. 라즈베리파이 준비

- OS: Raspberry Pi OS (64-bit, Debian 13 기반). Raspberry Pi Imager에서 사용자 계정과 SSH를 켜고 굽습니다.
- 전원: 3A 이상 USB-PD 어댑터. 공식 27W(5V 5A) 어댑터를 권장합니다. 3A 어댑터를 쓸 때는 `/boot/firmware/config.txt`에 `usb_max_current_enable=1`을 넣어야 USB 장치가 안정적입니다.
- 방열: 피크 시간에 4대를 계속 추론하므로 방열 케이스가 필요합니다. 알루미늄 케이스로 부하 시 약 60℃, 스로틀링 없음(`vcgencmd get_throttled` → `0x0`)을 유지했습니다.

```bash
sudo apt update
sudo apt install -y python3-opencv python3-flask python3-numpy
pip install --break-system-packages ncnn     # NCNN 추론 (없으면 OpenCV DNN으로 자동 대체)
```

## 2. 네트워크: 카메라 전용 핫스팟 + 인터넷 분리

내장 Wi-Fi(`wlan0`)는 **카메라 전용 핫스팟**, USB 무선랜 동글(`wlan1`)은 **인터넷 전용**으로 나눕니다.

- 카메라 영상이 외부 네트워크를 지나지 않습니다.
- 학교·회사 Wi-Fi처럼 기기 간 통신이 막힌 망에서도 카메라를 붙일 수 있습니다.
- Tapo C200은 2.4GHz만 지원하므로 핫스팟을 2.4GHz로 둡니다.

```bash
# 카메라 핫스팟 (2.4GHz 채널 11, 라즈베리파이가 DHCP 서버 역할)
sudo nmcli connection add type wifi ifname wlan0 con-name cam-ap autoconnect yes ssid <핫스팟 이름> \
  802-11-wireless.mode ap 802-11-wireless.band bg 802-11-wireless.channel 11 \
  ipv4.method shared \
  wifi-sec.key-mgmt wpa-psk wifi-sec.proto rsn wifi-sec.pairwise ccmp wifi-sec.pmf disable \
  wifi-sec.psk "<핫스팟 비밀번호>"
sudo nmcli connection up cam-ap

# 카메라 IP 고정 (카메라 MAC은 /var/lib/NetworkManager/dnsmasq-wlan0.leases 에서 확인)
sudo tee /etc/NetworkManager/dnsmasq-shared.d/cams.conf <<'EOF'
dhcp-host=<cam1 MAC>,<cam1 IP>
dhcp-host=<cam2 MAC>,<cam2 IP>
dhcp-host=<cam3 MAC>,<cam3 IP>
dhcp-host=<cam4 MAC>,<cam4 IP>
EOF
sudo nmcli connection up cam-ap        # 다시 올려야 적용 (카메라가 잠깐 끊김)

# 인터넷은 USB 동글로 (프로필을 동글 MAC에 묶어 wlan0이 잡지 않게)
sudo nmcli connection add type wifi con-name internet ssid "<Wi-Fi 이름>" \
  802-11-wireless.mac-address <동글 MAC> wifi-sec.key-mgmt wpa-psk wifi-sec.psk "<Wi-Fi 비밀번호>"
```

### 끊김 방지 설정

```bash
# Wi-Fi 절전 끄기
printf '[connection]\nwifi.powersave = 2\n' | sudo tee /etc/NetworkManager/conf.d/wifi-powersave-off.conf

# RTL8822BU 동글: 깊은 절전 끄기, USB 3.0 전환 막기 (USB 3.0 모드는 2.4GHz 핫스팟에 간섭)
sudo tee /etc/modprobe.d/rtw88.conf <<'EOF'
options rtw88_core disable_lps_deep=y
options rtw88_usb switch_usb_mode=n
EOF
```

동글은 **USB 2.0 포트**에 연장선으로 꽂습니다. 숫자만 보내므로 USB 2.0 속도로 충분합니다.

## 3. 카메라

1. Tapo 앱으로 카메라를 핫스팟에 연결합니다.
2. 앱의 고급 설정에서 카메라 계정(RTSP용 아이디·비밀번호)을 만듭니다.
3. 영상 주소는 `rtsp://<아이디>:<비밀번호>@<카메라 IP>:554/stream1` (1080p) 또는 `stream2` (720p)입니다.
4. 클라우드 녹화와 앱 알림은 끕니다.

`pi/people_count.py`의 `CAMS`에 카메라마다 IP·아이디·비밀번호를 넣습니다.

## 4. 모델 준비

모델 변환은 성능 좋은 컴퓨터에서 하고, 결과 폴더만 라즈베리파이로 복사합니다.

```bash
pip install ultralytics
yolo export model=yolo11n.pt format=ncnn imgsz=1280              # → yolo11n_ncnn_model/
yolo export model=yolo11n.pt format=onnx imgsz=1280 opset=12     # OpenCV DNN 대체용
```

결과물을 `people_count.py`의 `NCNN_MODEL`, `MODEL` 이름에 맞춰 라즈베리파이 홈 폴더에 둡니다. 우리 환경(라즈베리파이 5)에서 NCNN이 OpenCV DNN보다 약 2배 빨랐습니다. 1280 입력, 카메라 4대 기준 한 회차 약 2.6초입니다.

## 5. 자동 실행 (systemd)

[pi/systemd/](../pi/systemd/)의 두 파일을 `/etc/systemd/system/`에 복사합니다. 사용자 이름과 경로는 환경에 맞게 고칩니다.

```bash
sudo cp pi/systemd/*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now people-count push-counts
journalctl -u people-count -f      # 로그 보기
```

처음 실행한 뒤 `http://<라즈베리파이 주소>:5000`에서 카메라마다 **집계 구역**을 그리고, `:5000/tables`에서 **테이블 영역과 좌석 수**를 등록합니다.

## 6. 서버

```bash
cd cafeteria-server
python3 -m venv venv && venv/bin/pip install flask gunicorn
openssl rand -hex 24 > token.txt          # 라즈베리파이 ~/.push_token 에도 같은 값
venv/bin/gunicorn -w 1 --threads 4 -b 0.0.0.0:8000 server:app
```

- `server.py`의 `ALLOWED_INGEST`에 라즈베리파이 주소를 넣습니다.
- 라즈베리파이 `push_counts.py`의 `SERVER`에 서버 주소를 넣습니다.
- macOS에서는 `launchd/`의 plist를 `~/Library/LaunchAgents/`에 복사해 자동 실행합니다. 경로는 `/path/to/cafeteria-server`를 실제 경로로 바꿉니다.

## 7. 원격 접속과 외부 공개 (Tailscale)

```bash
curl -fsSL https://tailscale.com/install.sh | sh      # 라즈베리파이와 서버 모두
sudo tailscale up
tailscale funnel --bg 8000                             # 서버에서: 학생용 화면만 HTTPS로 공개
```

- Tailscale을 쓰면 Wi-Fi가 바뀌어도 기기 주소가 그대로이고, 포트를 열 필요가 없습니다.
- Funnel로는 학생용 화면만 공개됩니다. 통계(`/stats`)는 서버 코드에서 외부 요청을 막아 두어 VPN 안에서만 열립니다.

## 8. 백업

서버에서 `backup_pi.sh`를 30분마다 실행해 라즈베리파이 기록을 rsync로 복사합니다. 그러려면 SSH 키를 먼저 등록해 둡니다 (`ssh-copy-id <사용자>@<라즈베리파이 주소>`).
