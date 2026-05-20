# ipcam-backdoor-test-environment

실제 IP 카메라 시스템처럼 보이는 baseline 환경을 먼저 만들고, 그 위에 취약점 시나리오와 NDR 탐지를 얹기 위한 프로젝트입니다.

현재는 `infected_scan 랩 모드`와 `baseline 데이터셋과 attack 데이터셋 수집 로직`까지 구현된 상태입니다.

## 현재 구현 범위

- `camera-app`
  - 카메라 역할
  - RTSP 송출
  - `/health`, `/status` API
  - 정상 관리 서버를 향한 primary beacon / task polling
  - safe command 처리
    - `get_status`
    - `set_quality`
    - `toggle_overlay`
    - `record_marker`

- `mediamtx`
  - RTSP/HLS 미디어 서버

- `control-server`
  - 정상 관리용 control plane
  - beacon 수신
  - task queue
  - result 수집

- `nvr-console`
  - 관리자 로그인
  - 카메라 목록 / 상세
  - 정상 control-plane 모니터링 화면
  - safe task 실행 UI
  - 녹화 archive
  - audit log

## 현재 구조

정상 baseline 경로:

```text
camera-app -> control-server + MediaMTX -> nvr-console/recorder -> recordings archive
```

운영자 경로:

```text
browser -> nvr-console -> cameras / control / recordings / audit
```

중요한 점:

- 정상 관리 채널은 `LAB_MODE`와 분리되어 항상 동작합니다.
- `camera-app`는 `PRIMARY_CONTROL_URL`을 향해 beacon/poll 합니다.
- `LAB_MODE`는 이후 rogue/C2 시나리오를 붙일 때 사용하는 라벨 성격입니다.

## 서비스와 포트

- `8080`: `control-server`
- `8090`: `camera-app` API
- `8091`: `nvr-console`
- `8554`: RTSP
- `8888`: HLS

## 빠른 시작

### 요구 사항

- Docker Desktop
- VLC 같은 RTSP 플레이어

### 전체 실행

```powershell
docker compose up -d --build
```

서비스 상태 확인:

```powershell
docker compose ps
```

정상이라면 아래 4개가 떠 있어야 합니다.

- `mediamtx`
- `control-server`
- `camera-app`
- `nvr-console`

### 전체 중지

```powershell
docker compose stop
docker compose down
```

### 로그 확인

```powershell
docker compose logs -f
docker compose logs camera-app
docker compose logs control-server
docker compose logs nvr-console
```

## 접속 주소

- NVR 로그인: `http://localhost:8091/login`
- NVR 대시보드: `http://localhost:8091/`
- 카메라 목록: `http://localhost:8091/cameras`
- 카메라 상세: `http://localhost:8091/cameras/camera-app-001`
- Control 화면: `http://localhost:8091/control`
- Recordings: `http://localhost:8091/recordings`
- Audit: `http://localhost:8091/audit`
- HLS: `http://localhost:8888/cam1/index.m3u8`

기본 관리자 계정:

- ID: `admin`
- PW: `lab-admin`

## 기본 확인 방법

### 1. 헬스체크

```powershell
Invoke-RestMethod http://localhost:8080/health
Invoke-RestMethod http://localhost:8090/health
Invoke-RestMethod http://localhost:8091/health
```

### 2. RTSP 재생

호스트에서 VLC로 확인할 때는 아래 주소를 사용합니다.

```text
rtsp://localhost:8554/cam1
```

`rtsp://mediamtx:8554/cam1` 는 컨테이너 내부 이름이라 호스트 VLC에서는 보통 쓰지 않습니다.

### 3. camera-app 상태 확인

```powershell
Invoke-RestMethod http://localhost:8090/status | ConvertTo-Json -Depth 10
```

주요 확인 필드:

- `stream.status`
- `controls.quality`
- `controls.overlay_enabled`
- `source.kind`
- `control_channels.primary.base_url`
- `control_channels.primary.beacon.status`
- `control_channels.primary.poller.status`
- `lab_mode`

정상 예시:

- `stream.status = publishing`
- `lab_mode = none`
- `control_channels.primary.base_url = http://control-server:8080`

### 4. 정상 control-plane 확인

```powershell
Invoke-RestMethod http://localhost:8080/beacons | ConvertTo-Json -Depth 6
Invoke-RestMethod http://localhost:8080/tasks | ConvertTo-Json -Depth 6
Invoke-RestMethod http://localhost:8080/results | ConvertTo-Json -Depth 8
```

최신 beacon/result에서 확인할 값:

- `camera_id = camera-app-001`
- `control_channel = primary`
- `lab_mode = none`

주의:

- `control-server`는 현재 메모리 저장이라 재시작 전의 예전 기록이 같이 보일 수 있습니다.

### 5. NVR 화면에서 확인

카메라 상세 페이지에서 다음을 볼 수 있습니다.

- 현재 스트림 상태
- 현재 품질
- 오버레이 on/off
- beacon 상태
- poller 상태
- 최근 beacon / pending task / recent result

## Safe task 테스트

NVR 카메라 상세 화면에서 직접 실행하거나, API로 테스트할 수 있습니다.

### API 예시

```powershell
Invoke-RestMethod `
  -Method Post `
  -Uri http://localhost:8080/tasks `
  -ContentType "application/json" `
  -Body '{"camera_id":"camera-app-001","command":"get_status","params":{}}'
```

몇 초 뒤 결과 확인:

```powershell
$env:RUN_MODE="local"
$env:SOURCE_TYPE="webcam"
$env:WEBCAM_BACKEND="dshow"
$env:WEBCAM_DEVICE="Integrated Webcam"
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

## 녹화

### 녹화 확인

- 페이지: `http://localhost:8091/recordings`
- 실제 저장 경로: `data/recordings`

기본 segment 길이:

- `60초`

짧게 테스트하려면:

```powershell
$env:NVR_RECORDING_SEGMENT_SECONDS="15"
docker compose up -d --build nvr-console
```

### 녹화 끄기 / 켜기

1. `http://localhost:8091/cameras/camera-app-001`
2. `Recording Mode`
3. `Continuous` 또는 `Disabled`
4. `Save camera settings`

설명:

- `Continuous`: 계속 녹화
- `Disabled`: 새 녹화 중지

주의:

- 기존 파일은 자동 삭제되지 않습니다.
- `retention_days`는 현재 메타데이터만 저장하고 실제 자동 삭제는 아직 없습니다.

## 로컬 실행

### Windows

```powershell
$env:RUN_MODE="local"
.\services\camera-app\run-local.ps1
```

### Linux / macOS

```bash
RUN_MODE=local ./services/camera-app/run-local.sh
```

로컬 기본값:

- `SOURCE_TYPE=file`
- `INPUT_SOURCE=<repo>/samples/demo.mp4`
- `RTSP_URL=rtsp://localhost:8554/cam1`
- `PRIMARY_CONTROL_URL=http://localhost:8080`

## 주요 환경변수

### camera-app

- `CAMERA_APP_LAB_MODE`
- `CAMERA_APP_SOURCE_TYPE`
- `CAMERA_APP_PRIMARY_CONTROL_URL`
- `CAMERA_APP_PRIMARY_BEACON_ENABLED`
- `CAMERA_APP_PRIMARY_POLL_ENABLED`
- `CAMERA_APP_PRIMARY_BEACON_INTERVAL_SECONDS`
- `CAMERA_APP_PRIMARY_BEACON_TIMEOUT_SECONDS`
- `CAMERA_APP_PRIMARY_POLL_INTERVAL_SECONDS`
- `CAMERA_APP_PRIMARY_POLL_TIMEOUT_SECONDS`
- `CAMERA_APP_WEBCAM_BACKEND`
- `CAMERA_APP_WEBCAM_DEVICE`
- `CAMERA_APP_WEBCAM_FRAMERATE`
- `CAMERA_APP_WEBCAM_RESOLUTION`
- `CAMERA_APP_WEBCAM_INPUT_FORMAT`

### nvr-console

- `NVR_ADMIN_USERNAME`
- `NVR_ADMIN_PASSWORD`
- `NVR_RECORDING_SEGMENT_SECONDS`
- `NVR_DEFAULT_RETENTION_DAYS`

## 현재 한계

- `control-server`는 메모리 기반이라 재시작 시 beacon/task/result 이력이 사라짐
- NVR 페이지 내장 영상 재생 UI 없음
- retention 기반 자동 삭제 없음
- 다중 카메라 등록 UI 없음
- `rogue-control-server` 없음
- abnormal control channel 시나리오 아직 없음
- NDR 탐지 엔진 아직 없음

## 권장 다음 단계

가장 먼저 추천하는 단계는 `rogue-control-server 추가`입니다.

이유:

- 지금은 정상 관리 채널(primary)은 완성되어 있습니다.
- 다음부터는 `정상과 동일한 task/beacon 메커니즘`을 쓰는 비인가 서버가 필요합니다.
- 그래야 NDR이 `task/beacon 자체`가 아니라 `비인가 목적지와의 task/beacon`을 탐지할 수 있습니다.

추천 순서:

1. `rogue-control-server` 추가
2. `camera-app`에 secondary/rogue 채널 붙이기
3. `scenario_id`, `run_id` 같은 ground truth 저장
4. abnormal beacon/poll 시나리오 추가
5. NDR 규칙 또는 MVP 구현
6. 탐지 검증

## 체크리스트

1. `docker compose ps`에서 4개 서비스가 모두 `Up`
2. `http://localhost:8090/health` 응답 확인
3. `http://localhost:8091/login` 로그인 성공
4. VLC에서 `rtsp://localhost:8554/cam1` 재생 성공
5. `/status`에서 `control_channels.primary` 확인
6. `/beacons`에서 최신 항목 `control_channel=primary` 확인
7. `get_status` task 후 `/results`에서 성공 결과 확인
8. `/recordings` 또는 `data/recordings`에서 녹화 파일 생성 확인

이 체크리스트가 통과되면 현재 baseline과 정상 primary control 채널은 정상 동작 중입니다.
