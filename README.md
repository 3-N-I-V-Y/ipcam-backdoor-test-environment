# ipcam-backdoor-test-environment

연구용 IP 카메라 테스트베드다. 실제 악성 백도어는 구현하지 않고, lab-only / explicit opt-in 조건에서만 동작하는 안전한 control-plane을 가진 카메라 환경을 만든다.

현재 단계의 초점은 `camera-app` 자체를 안정적으로 구성하는 것이다.

- 파일 또는 웹캠 입력을 받아 RTSP로 송출
- 로컬 상태 API 제공
- 선택적으로 beacon / safe poll-task 활성화
- 공용 Docker mock 환경과 로컬 디버깅 모드 둘 다 지원

## 구성요소

```text
ipcam-backdoor-test-environment/
├─ .devcontainer/
├─ docs/
├─ samples/
│  └─ demo.mp4
├─ services/
│  ├─ camera-app/
│  │  ├─ Dockerfile
│  │  ├─ beacon.py
│  │  ├─ main.py
│  │  ├─ poller.py
│  │  ├─ requirements.txt
│  │  ├─ run-local.ps1
│  │  ├─ run-local.sh
│  │  ├─ source.py
│  │  ├─ state.py
│  │  └─ streamer.py
│  ├─ control-server/
│  └─ mediamtx/
├─ compose.hardware.yaml
├─ compose.yaml
└─ README.md
```

## 실행 모드

### 1. Full Docker Mode

팀 공용 기본 실행 방식이다.

- `mediamtx`, `control-server`, `camera-app` 전부 Docker로 실행
- 기본 입력 소스는 `samples/demo.mp4`
- `camera-app`은 `RUN_MODE=docker` 로 동작
- RTSP publish 대상은 `rtsp://mediamtx:8554/cam1`

실행:

```powershell
docker compose up -d --build
```

### 2. Local Camera-App Mode

개발/디버깅용 실행 방식이다.

- `mediamtx`, `control-server`만 Docker로 실행
- `camera-app`은 호스트에서 직접 실행
- `camera-app`은 `RUN_MODE=local` 로 동작
- 기본 RTSP publish 대상은 `rtsp://localhost:8554/cam1`
- 기본 RTSP publish transport는 `tcp`
- 기본 control API 대상은 `http://localhost:8080`

이 모드는 웹캠 접근, ffmpeg 인자, OS별 장치 문제를 디버깅할 때 유리하다.

중요:

- `camera-app`을 로컬로 실행할 때는 Docker의 `camera-app` 컨테이너를 같이 띄우지 않는다.
- 그렇지 않으면 `8090` 포트 충돌이 난다.

## 빠른 시작

### Docker 전체 실행

```powershell
docker compose up -d --build
```

확인:

- Control API: `http://localhost:8080/health`
- Camera API: `http://localhost:8090/health`
- RTSP: `rtsp://localhost:8554/cam1`

### 로컬 camera-app 실행

1. 인프라만 Docker로 올린다.

```powershell
docker compose up -d mediamtx control-server
```

2. `camera-app` 전용 가상환경을 만든다. 처음 한 번만 하면 된다.

```powershell
py -3 -m venv services/camera-app/.venv
```

3. 가상환경에 로컬 Python 의존성을 설치한다.

```powershell
.\services\camera-app\.venv\Scripts\python.exe -m pip install -r services/camera-app/requirements.txt
```

`py` 또는 `python` 실행기를 직접 지정해야 하면 `PYTHON_BIN`에 경로를 넣어도 된다.

```powershell
$env:PYTHON_BIN="C:\path\to\python.exe"
& $env:PYTHON_BIN -m pip install -r services/camera-app/requirements.txt
```

4. `camera-app`을 로컬로 실행한다.

Windows PowerShell:

```powershell
powershell -ExecutionPolicy Bypass -File ".\services\camera-app\run-local.ps1"
```

Linux/macOS:

```bash
./services/camera-app/run-local.sh
```

현재 PowerShell 세션에서만 스크립트 실행을 허용하고 싶으면 아래처럼 실행해도 된다.

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\services\camera-app\run-local.ps1
```

스크립트를 쓰지 않고 직접 실행해도 된다.

```powershell
.\services\camera-app\.venv\Scripts\python.exe .\services\camera-app\main.py
```

기본값:

- `RUN_MODE=local`
- `INPUT_SOURCE=<repo>/samples/demo.mp4`
- `RTSP_URL=rtsp://localhost:8554/cam1`
- `CONTROL_URL=http://localhost:8080`
- `API_HOST=127.0.0.1`
- `API_PORT=8090`
- `RTSP_TRANSPORT=tcp`

`run-local.ps1` / `run-local.sh`는 아래 우선순서로 Python 실행기를 찾는다.

1. `PYTHON_BIN`
2. `services/camera-app/.venv`
3. 시스템 `py` 또는 `python`

로컬 모드에서는 `camera-app`이 기본적으로 `RTSP_TRANSPORT=tcp`로 publish한다. 호스트에서 실행한 `ffmpeg`가 Docker의 `mediamtx`로 붙을 때 가장 안정적이다.

## 로컬 웹캠 사용

로컬 실행 모드에서는 Linux `v4l2`와 Windows `dshow`를 모두 지원한다.

### Linux webcam (`v4l2`)

```bash
SOURCE_TYPE=webcam \
WEBCAM_BACKEND=v4l2 \
WEBCAM_DEVICE=/dev/video0 \
./services/camera-app/run-local.sh
```

### Windows 내장 webcam (`dshow`)

1. 먼저 FFmpeg로 장치 이름을 확인한다.

```powershell
ffmpeg -list_devices true -f dshow -i dummy
```

2. 확인한 장치 이름으로 `camera-app`을 실행한다.

`ffmpeg -list_devices` 출력의 `(video)` 표시는 장치 타입 설명일 뿐 장치 이름 일부가 아니다. `Integrated Webcam (video)` 전체를 넣지 말고 장치 이름만 넣는다.

```powershell
$env:RUN_MODE="local"
$env:SOURCE_TYPE="webcam"
$env:WEBCAM_BACKEND="dshow"
$env:WEBCAM_DEVICE="Integrated Webcam"
$env:LAB_MODE="beacon,poll"
powershell -ExecutionPolicy Bypass -File ".\services\camera-app\run-local.ps1"
```

선택적으로 픽셀 포맷을 지정할 수 있다.

```powershell
$env:WEBCAM_INPUT_FORMAT="mjpeg"
```

## 주요 환경변수

- `RUN_MODE`
  - `docker`: 컨테이너 기본값 사용
  - `local`: localhost / repo 경로 기본값 사용
- `LAB_MODE`
  - `none`
  - `beacon`
  - `poll`
  - `beacon,poll`
- `SOURCE_TYPE`
  - `file`
  - `webcam`
- `WEBCAM_BACKEND`
  - `v4l2`
  - `dshow`
- `WEBCAM_DEVICE`
  - `v4l2`: `/dev/video0` 같은 장치 경로
  - `dshow`: `Integrated Webcam` 같은 Windows 장치 이름
- `INPUT_SOURCE`
  - `file` 소스일 때 사용할 입력 파일
- `RTSP_URL`
  - mediamtx publish 대상
- `RTSP_TRANSPORT`
  - `tcp`: interleaved RTSP over TCP. 로컬 host -> Docker `mediamtx` 조합의 기본값
  - `udp`: RTP/RTCP over UDP. 필요하면 `mediamtx`의 UDP 포트(`8000/8001`)도 열려 있어야 한다.
- `CONTROL_URL`
  - beacon / poll-task 대상 control server 주소

## 검증 방법

### 상태 API

```powershell
Invoke-RestMethod http://localhost:8090/status | ConvertTo-Json -Depth 10
```

확인 포인트:

- `run_mode`
- `lab_mode`
- `source.kind`
- `stream.status`
- `beacon`
- `poller`

### RTSP 재생

VLC에서 아래 주소를 연다.

```text
rtsp://localhost:8554/cam1
```

## 안전성 범위

이 프로젝트는 아래 기능을 의도적으로 제외한다.

- 원격 쉘
- 인증 우회
- 지속성
- 탐지 회피
- 외부 임의 서버로의 비밀 유출
- 임의 명령 실행

허용 제어는 앱 상태 변경 수준으로만 제한한다.

- `noop`
- `get_status`
- `set_quality`
- `toggle_overlay`
- `record_marker`
