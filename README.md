# ipcam-backdoor-test-environment

연구용 IP 카메라 테스트베드다. 실제 악성 백도어는 구현하지 않고, lab-only / explicit opt-in 조건에서만 동작하는 안전한 control-plane을 가진 카메라 환경을 만든다.

현재 단계의 목표는 `camera-app`을 중심으로 카메라 본체를 먼저 완성하는 것이다.

- 영상 입력을 받아 RTSP로 송출
- 상태 API 제공
- 선택적으로 beacon / poll-task 활성화
- safe command를 실제 스트림 변화로 연결

## 프로젝트 범위

포함:

- 파일 소스 또는 웹캠 소스 입력
- RTSP 송출
- 상태 조회
- safe beacon
- safe poll-task
- 화질 변경
- overlay on/off
- marker 기록

제외:

- 원격 쉘
- 인증 우회
- 지속성
- 탐지 회피
- 임의 명령 실행
- 외부 임의 서버로의 비밀 유출

## 현재 구조

```text
ipcam-backdoor-test-environment/
├─ .devcontainer/
│  └─ devcontainer.json
├─ docs/
│  └─ architecture.md
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
│  │  ├─ app.py
│  │  ├─ Dockerfile
│  │  └─ requirements.txt
│  ├─ mediamtx/
│  │  └─ mediamtx.yml
│  └─ mock-camera/
│     ├─ Dockerfile
│     └─ start.sh
├─ compose.hardware.yaml
├─ compose.yaml
└─ README.md
```

## 구성요소 역할

- `.devcontainer`
  - 팀 공용 개발환경 정의
- `samples/demo.mp4`
  - file source 테스트 입력 영상
- `services/mediamtx`
  - RTSP 허브
- `services/control-server`
  - 연구용 control API 서버
- `services/camera-app`
  - 현재 카메라 본체
- `compose.yaml`
  - 공용 기본 실행 기준
- `compose.hardware.yaml`
  - Linux webcam 하드웨어 override

## 아키텍처

### 영상 경로

```text
input source
  -> camera-app / streamer.py
  -> ffmpeg
  -> mediamtx
  -> VLC
```

### control-plane 경로

```text
camera-app
  -> /health, /status

camera-app
  -> beacon
  -> control-server

camera-app
  -> poll / task / result
  -> control-server
```

## camera-app 내부 구조

- `main.py`
  - 실행 진입점
  - 환경변수 로드
  - FastAPI 앱 생성
  - streamer / beacon / poller lifecycle 관리
- `state.py`
  - 중앙 상태 저장소
  - stream 상태
  - control 상태
  - beacon / poller 상태
  - safe command 적용
- `source.py`
  - 입력 소스 추상화
  - `FileSource`
  - `WebcamSource`
  - Linux `v4l2`, Windows `dshow` 지원
- `streamer.py`
  - ffmpeg supervisor
  - RTSP publish
  - `set_quality`, `toggle_overlay` 반영 시 ffmpeg 재시작
- `beacon.py`
  - `LAB_MODE=beacon` 일 때 상태 beacon 전송
- `poller.py`
  - `LAB_MODE=poll` 일 때 task polling 및 result 보고

## 현재 지원 기능

### source

- `file`
  - `samples/demo.mp4` 사용
- `webcam`
  - Linux `v4l2`
  - Windows `dshow`

### API

- `GET /health`
- `GET /status`

### beacon

- `LAB_MODE=none`
- `LAB_MODE=beacon`

### poll-task

- `LAB_MODE=poll`
- `LAB_MODE=beacon,poll`

### safe command

- `noop`
- `get_status`
- `set_quality`
- `toggle_overlay`
- `record_marker`

## 실제 반영되는 command

### 1. set_quality

`low`, `medium`, `high` 중 하나를 적용한다.

현재 구현에서는:

- 상태 변경
- ffmpeg 재시작
- bitrate profile 변경

까지 실제로 연결되어 있다.

확인 포인트:

- `controls.quality`
- `stream.applied_quality`
- `stream.status`

### 2. toggle_overlay

영상 좌상단에 연구용 텍스트 overlay를 켜고 끈다.

표시 예시:

```text
LAB MODE | camera-app-001 | quality=low
```

현재 구현에서는:

- 상태 변경
- ffmpeg 재시작
- `drawtext` overlay 반영

까지 실제로 연결되어 있다.

확인 포인트:

- `controls.overlay_enabled`
- `stream.applied_overlay_enabled`
- `stream.status`

### 3. record_marker

상태 저장소의 marker 목록에 메모를 남긴다.

확인 포인트:

- `markers`

## 실행 모드

### 1. Full Docker Mode

공용 기본 실행 방식이다.

- `mediamtx`, `control-server`, `camera-app` 전부 Docker로 실행
- 기본 source는 `file`
- 기본 입력은 `/samples/demo.mp4`

기본 RTSP URL:

```text
rtsp://localhost:8554/cam1
```

### 2. Local Camera-App Mode

개발/디버깅용 실행 방식이다.

- `mediamtx`, `control-server`만 Docker로 실행
- `camera-app`은 로컬 프로세스로 직접 실행
- Windows webcam, ffmpeg 입력 인자, 장치 문제를 디버깅할 때 사용

로컬 모드 기본값:

- `RUN_MODE=local`
- `RTSP_URL=rtsp://localhost:8554/cam1`
- `CONTROL_URL=http://localhost:8080`
- `API_HOST=127.0.0.1`
- `RTSP_TRANSPORT=tcp`

## 실행 방법

### 1. Docker 전체 실행

가장 기본적인 공용 실행 방식이다.

```powershell
docker compose up -d --build
```

이 방식은 아래를 한 번에 실행한다.

- `mediamtx`
- `control-server`
- `camera-app`

기본값:

- `RUN_MODE=docker`
- `LAB_MODE=none`
- `SOURCE_TYPE=file`
- `INPUT_SOURCE=/samples/demo.mp4`

확인:

- Control API: `http://localhost:8080/health`
- Camera API: `http://localhost:8090/health`
- RTSP: `rtsp://localhost:8554/cam1`

### 2. Docker 실행 시 LAB_MODE 설정

`compose.yaml`에서 `camera-app`은 아래 환경변수를 사용한다.

- `CAMERA_APP_LAB_MODE`
- `CAMERA_APP_SOURCE_TYPE`
- `CAMERA_APP_CONTROL_URL`
- `CAMERA_APP_WEBCAM_BACKEND`
- `CAMERA_APP_WEBCAM_DEVICE`
- `CAMERA_APP_WEBCAM_FRAMERATE`
- `CAMERA_APP_WEBCAM_RESOLUTION`
- `CAMERA_APP_WEBCAM_INPUT_FORMAT`

#### beacon만 켜기

```powershell
$env:CAMERA_APP_LAB_MODE="beacon"
docker compose up -d --build camera-app
```

#### poll만 켜기

```powershell
$env:CAMERA_APP_LAB_MODE="poll"
docker compose up -d --build camera-app
```

#### beacon + poll 같이 켜기

```powershell
$env:CAMERA_APP_LAB_MODE="beacon,poll"
docker compose up -d --build camera-app
```

#### 다시 기본값으로 되돌리기

```powershell
$env:CAMERA_APP_LAB_MODE="none"
docker compose up -d --build camera-app
```

확인 포인트:

- `/status` 의 `lab_mode`
- `/status` 의 `beacon.enabled`
- `/status` 의 `poller.enabled`

### 3. Docker + Linux webcam 실행

이 방식은 Linux `v4l2` 환경에서만 사용한다.

```powershell
$env:CAMERA_APP_LAB_MODE="poll"
$env:CAMERA_APP_SOURCE_TYPE="webcam"
$env:CAMERA_APP_WEBCAM_BACKEND="v4l2"
$env:CAMERA_APP_WEBCAM_DEVICE="/dev/video0"
docker compose -f compose.yaml -f compose.hardware.yaml up -d --build
```

설명:

- 기본 `compose.yaml` 위에 `compose.hardware.yaml`을 추가 적용
- `camera-app`이 webcam source로 실행
- `devices` 매핑을 통해 Linux 카메라 장치를 컨테이너에 연결

### 4. Local camera-app 실행

개발/디버깅용 권장 방식이다.

이 방식은:

- `mediamtx`, `control-server`는 Docker로 실행
- `camera-app`은 로컬 Python 프로세스로 실행

먼저 인프라만 실행한다.

```powershell
docker compose up -d mediamtx control-server
```

그 다음 `camera-app`만 로컬로 실행한다.

Windows:

```powershell
$env:RUN_MODE="local"
.\services\camera-app\run-local.ps1
```

Linux/macOS:

```bash
RUN_MODE=local ./services/camera-app/run-local.sh
```

기본값:

- `RUN_MODE=local`
- `LAB_MODE=none`
- `SOURCE_TYPE=file`
- `INPUT_SOURCE=<repo>/samples/demo.mp4`
- `RTSP_URL=rtsp://localhost:8554/cam1`
- `CONTROL_URL=http://localhost:8080`
- `API_HOST=127.0.0.1`
- `RTSP_TRANSPORT=tcp`

### 5. Local 실행 시 LAB_MODE 설정

로컬 실행에서는 `CAMERA_APP_` 접두사가 아니라 실제 런타임 환경변수 이름을 직접 사용한다.

#### beacon만 켜기

```powershell
$env:RUN_MODE="local"
$env:LAB_MODE="beacon"
.\services\camera-app\run-local.ps1
```

#### poll만 켜기

```powershell
$env:RUN_MODE="local"
$env:LAB_MODE="poll"
.\services\camera-app\run-local.ps1
```

#### beacon + poll 같이 켜기

```powershell
$env:RUN_MODE="local"
$env:LAB_MODE="beacon,poll"
.\services\camera-app\run-local.ps1
```

#### file source + poll 예시

```powershell
$env:RUN_MODE="local"
$env:LAB_MODE="poll"
$env:SOURCE_TYPE="file"
.\services\camera-app\run-local.ps1
```

### 6. Local Windows webcam 실행

Windows 내장 웹캠은 `dshow`로 사용한다.

1. FFmpeg로 장치 이름을 확인한다.

```powershell
ffmpeg -list_devices true -f dshow -i dummy
```

2. 장치 이름을 `WEBCAM_DEVICE`에 넣어서 실행한다.

```powershell
$env:RUN_MODE="local"
$env:LAB_MODE="poll"
$env:SOURCE_TYPE="webcam"
$env:WEBCAM_BACKEND="dshow"
$env:WEBCAM_DEVICE="Integrated Camera"
.\services\camera-app\run-local.ps1
```

필요하면 추가 설정:

```powershell
$env:WEBCAM_RESOLUTION="1280x720"
$env:WEBCAM_FRAMERATE="30"
$env:WEBCAM_INPUT_FORMAT="mjpeg"
```

### 7. Local Linux webcam 실행

```bash
RUN_MODE=local \
LAB_MODE=poll \
SOURCE_TYPE=webcam \
WEBCAM_BACKEND=v4l2 \
WEBCAM_DEVICE=/dev/video0 \
./services/camera-app/run-local.sh
```

### 8. 실행 후 확인 방법

#### camera-app 상태

```powershell
Invoke-RestMethod http://localhost:8090/status | ConvertTo-Json -Depth 10
```

주요 필드:

- `lab_mode`
- `source.kind`
- `stream.status`
- `stream.applied_quality`
- `stream.applied_overlay_enabled`
- `stream.last_error`
- `beacon.enabled`
- `poller.enabled`

#### control-server task / result

```powershell
Invoke-RestMethod http://localhost:8080/tasks
Invoke-RestMethod http://localhost:8080/results
```

#### VLC 재생

```text
rtsp://localhost:8554/cam1
```

## LAB_MODE 요약

- `none`
  - beacon, poll-task 모두 끔
- `beacon`
  - 상태 beacon만 전송
- `poll`
  - task polling 및 result 보고만 수행
- `beacon,poll`
  - beacon과 poll-task 둘 다 수행

## 환경변수 정리

### 실행 모드

- `RUN_MODE`
  - `docker`
  - `local`
- `LAB_MODE`
  - `none`
  - `beacon`
  - `poll`
  - `beacon,poll`

### source 관련

- `SOURCE_TYPE`
  - `file`
  - `webcam`
- `INPUT_SOURCE`
  - file source 경로
- `WEBCAM_BACKEND`
  - `v4l2`
  - `dshow`
- `WEBCAM_DEVICE`
  - `v4l2`: `/dev/video0`
  - `dshow`: `Integrated Camera` 같은 장치 이름
- `WEBCAM_FRAMERATE`
- `WEBCAM_RESOLUTION`
- `WEBCAM_INPUT_FORMAT`

### stream 관련

- `RTSP_URL`
- `RTSP_TRANSPORT`
  - `tcp`
  - `udp`
- `FFMPEG_BIN`
- `OVERLAY_FONTFILE`

### control 관련

- `CONTROL_URL`
- `BEACON_INTERVAL_SECONDS`
- `BEACON_TIMEOUT_SECONDS`
- `POLL_INTERVAL_SECONDS`
- `POLL_TIMEOUT_SECONDS`

## task 등록 예시

### set_quality

```powershell
Invoke-RestMethod `
  -Method Post `
  -Uri http://localhost:8080/tasks `
  -ContentType "application/json" `
  -Body '{"camera_id":"camera-app-001","command":"set_quality","params":{"quality":"low"}}'
```

### toggle_overlay

```powershell
Invoke-RestMethod `
  -Method Post `
  -Uri http://localhost:8080/tasks `
  -ContentType "application/json" `
  -Body '{"camera_id":"camera-app-001","command":"toggle_overlay","params":{"enabled":true}}'
```

### record_marker

```powershell
Invoke-RestMethod `
  -Method Post `
  -Uri http://localhost:8080/tasks `
  -ContentType "application/json" `
  -Body '{"camera_id":"camera-app-001","command":"record_marker","params":{"note":"lab marker 001"}}'
```

## 상태 확인 방법

### camera-app 상태

```powershell
Invoke-RestMethod http://localhost:8090/status | ConvertTo-Json -Depth 10
```

중요하게 볼 필드:

- `lab_mode`
- `source`
- `stream.status`
- `stream.applied_quality`
- `stream.applied_overlay_enabled`
- `stream.last_error`
- `controls.quality`
- `controls.overlay_enabled`
- `beacon`
- `poller`
- `markers`

### control-server 대기 task

```powershell
Invoke-RestMethod http://localhost:8080/tasks
```

### control-server 실행 결과

```powershell
Invoke-RestMethod http://localhost:8080/results
```

## 검증 기준

### stream 확인

- VLC에서 `rtsp://localhost:8554/cam1` 재생

### quality 변경 확인

- `controls.quality` 변경
- `stream.applied_quality` 변경
- VLC 화질 변화 확인

### overlay 확인

- `controls.overlay_enabled=true`
- `stream.applied_overlay_enabled=true`
- VLC 화면에 overlay 표시 확인

### poll 모드 확인

- `lab_mode` 가 `poll` 또는 `beacon,poll`
- `poller.enabled=true`

## 현재 권장 워크플로우

### 공용 기본 테스트

- `compose.yaml`
- file source
- VLC 재생
- `/status` 확인

### 로컬 하드웨어 테스트

- `camera-app` 로컬 실행
- Windows `dshow` 또는 Linux `v4l2`
- `mediamtx`, `control-server`는 Docker 유지

### control-plane 테스트

1. `LAB_MODE=poll` 또는 `beacon,poll`로 실행
2. `/tasks`로 safe command 등록
3. `/results` 확인
4. `/status` 확인
5. VLC에서 실제 스트림 변화 확인

## 현재 상태 요약

현재 프로젝트는 아래 단계까지 완료된 상태다.

- `camera-app` 기반 스트리밍
- file source / webcam source
- Docker / local 실행 모드
- Windows `dshow` webcam 지원
- beacon
- safe poll-task
- `set_quality` 실제 반영
- `toggle_overlay` 실제 반영
- `record_marker`

즉 지금은 `카메라 시스템 자체를 먼저 구축한다`는 목표 기준에서, 연구용 safe control channel을 가진 카메라 테스트베드의 핵심 뼈대가 갖춰진 상태다.
