from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
import logging
import os
from pathlib import Path
import platform
import sys

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import uvicorn

SERVICES_ROOT = Path(__file__).resolve().parents[1]
if (SERVICES_ROOT / "common").exists():
    sys.path.insert(0, str(SERVICES_ROOT))

from beacon import BeaconConfig, BeaconWorker
from common.scenario_logger import ScenarioLogger
from poller import PollerConfig, TaskPoller
from source import create_camera_source
from state import CameraState
from streamer import StreamerConfig, StreamerSupervisor


logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("camera-app")


def resolve_repo_root() -> Path:
    current_file = Path(__file__).resolve()
    for candidate in (current_file.parent, *current_file.parents):
        if (candidate / "samples" / "demo.mp4").exists():
            return candidate
    return current_file.parent


REPO_ROOT = resolve_repo_root()


@dataclass(slots=True)
class AppConfig:
    run_mode: str
    camera_id: str
    lab_mode: str
    source_type: str
    input_source: str
    webcam_backend: str
    webcam_device: str
    webcam_framerate: int
    webcam_resolution: str
    webcam_input_format: str | None
    overlay_fontfile: str | None
    rtsp_url: str
    rtsp_transport: str
    primary_control_url: str
    primary_beacon_enabled: bool
    primary_poll_enabled: bool
    api_host: str
    api_port: int
    ffmpeg_binary: str
    restart_delay_seconds: float
    publish_probe_seconds: float
    primary_beacon_interval_seconds: float
    primary_beacon_timeout_seconds: float
    primary_poll_interval_seconds: float
    primary_poll_timeout_seconds: float

    @classmethod
    def from_env(cls) -> "AppConfig":
        run_mode = parse_run_mode(os.getenv("RUN_MODE", "docker"))
        lab_mode = normalize_lab_mode(os.getenv("LAB_MODE", "none"))
        source_type = os.getenv("SOURCE_TYPE", "file").strip().lower()
        webcam_backend = os.getenv("WEBCAM_BACKEND", default_webcam_backend(run_mode)).strip().lower()
        webcam_device = os.getenv("WEBCAM_DEVICE", default_webcam_device(webcam_backend)).strip()
        webcam_input_format = os.getenv("WEBCAM_INPUT_FORMAT", "").strip() or None
        overlay_fontfile = os.getenv("OVERLAY_FONTFILE", default_overlay_fontfile(run_mode)).strip() or None

        return cls(
            run_mode=run_mode,
            camera_id=os.getenv("CAMERA_ID", "camera-app-001"),
            lab_mode=lab_mode,
            source_type=source_type,
            input_source=os.getenv("INPUT_SOURCE", default_input_source(run_mode)),
            webcam_backend=webcam_backend,
            webcam_device=webcam_device,
            webcam_framerate=int(os.getenv("WEBCAM_FRAMERATE", "30")),
            webcam_resolution=os.getenv("WEBCAM_RESOLUTION", "1280x720"),
            webcam_input_format=webcam_input_format,
            overlay_fontfile=overlay_fontfile,
            rtsp_url=os.getenv("RTSP_URL", default_rtsp_url(run_mode)),
            rtsp_transport=parse_rtsp_transport(
                os.getenv("RTSP_TRANSPORT", default_rtsp_transport(run_mode))
            ),
            primary_control_url=os.getenv(
                "PRIMARY_CONTROL_URL",
                os.getenv("CONTROL_URL", default_control_url(run_mode)),
            ),
            primary_beacon_enabled=parse_env_bool("PRIMARY_BEACON_ENABLED", default=True),
            primary_poll_enabled=parse_env_bool("PRIMARY_POLL_ENABLED", default=True),
            api_host=os.getenv("API_HOST", default_api_host(run_mode)),
            api_port=int(os.getenv("API_PORT", "8090")),
            ffmpeg_binary=os.getenv("FFMPEG_BIN", "ffmpeg"),
            restart_delay_seconds=float(os.getenv("RESTART_DELAY_SECONDS", "3")),
            publish_probe_seconds=float(os.getenv("PUBLISH_PROBE_SECONDS", "1.5")),
            primary_beacon_interval_seconds=float(
                os.getenv(
                    "PRIMARY_BEACON_INTERVAL_SECONDS",
                    os.getenv("BEACON_INTERVAL_SECONDS", "10"),
                )
            ),
            primary_beacon_timeout_seconds=float(
                os.getenv(
                    "PRIMARY_BEACON_TIMEOUT_SECONDS",
                    os.getenv("BEACON_TIMEOUT_SECONDS", "3"),
                )
            ),
            primary_poll_interval_seconds=float(
                os.getenv(
                    "PRIMARY_POLL_INTERVAL_SECONDS",
                    os.getenv("POLL_INTERVAL_SECONDS", "10"),
                )
            ),
            primary_poll_timeout_seconds=float(
                os.getenv(
                    "PRIMARY_POLL_TIMEOUT_SECONDS",
                    os.getenv("POLL_TIMEOUT_SECONDS", "3"),
                )
            ),
        )


def parse_run_mode(raw_value: str) -> str:
    normalized = raw_value.strip().lower() or "docker"
    if normalized not in {"docker", "local"}:
        raise ValueError("RUN_MODE must be one of: docker, local")
    return normalized


def normalize_lab_mode(raw_value: str) -> str:
    normalized = raw_value.strip().lower()
    if not normalized or normalized == "none":
        return "none"

    tokens = sorted({token.strip() for token in normalized.split(",") if token.strip()})
    return ",".join(tokens) if tokens else "none"


def parse_env_bool(name: str, *, default: bool) -> bool:
    raw_value = os.getenv(name)
    if raw_value is None or not raw_value.strip():
        return default

    normalized = raw_value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean-like value")


def default_input_source(run_mode: str) -> str:
    if run_mode == "local":
        return str(REPO_ROOT / "samples" / "demo.mp4")
    return "/samples/demo.mp4"


def default_webcam_backend(run_mode: str) -> str:
    if run_mode == "local" and platform.system().lower() == "windows":
        return "dshow"
    return "v4l2"


def default_webcam_device(webcam_backend: str) -> str:
    if webcam_backend == "dshow":
        return ""
    return "/dev/video0"


def default_rtsp_url(run_mode: str) -> str:
    if run_mode == "local":
        return "rtsp://localhost:8554/cam1"
    return "rtsp://mediamtx:8554/cam1"


def default_overlay_fontfile(run_mode: str) -> str:
    candidates: list[Path] = []
    if run_mode == "docker":
        candidates.append(Path("/usr/share/fonts/TTF/DejaVuSans.ttf"))
    elif platform.system().lower() == "windows":
        candidates.append(Path("C:/Windows/Fonts/arial.ttf"))
    else:
        candidates.extend(
            [
                Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
                Path("/usr/share/fonts/TTF/DejaVuSans.ttf"),
                Path("/usr/share/fonts/dejavu/DejaVuSans.ttf"),
            ]
        )

    for candidate in candidates:
        if candidate.exists():
            return str(candidate)

    return ""


def parse_rtsp_transport(raw_value: str) -> str:
    normalized = raw_value.strip().lower() or "tcp"
    if normalized not in {"tcp", "udp"}:
        raise ValueError("RTSP_TRANSPORT must be one of: tcp, udp")
    return normalized


def default_rtsp_transport(run_mode: str) -> str:
    if run_mode == "local":
        return "tcp"
    return "udp"


def default_control_url(run_mode: str) -> str:
    if run_mode == "local":
        return "http://localhost:8080"
    return "http://control-server:8080"


def default_api_host(run_mode: str) -> str:
    if run_mode == "local":
        return "127.0.0.1"
    return "0.0.0.0"


def create_app() -> FastAPI:
    config = AppConfig.from_env()
    source = create_camera_source(
        source_type=config.source_type,
        file_path=config.input_source,
        webcam_device=config.webcam_device,
        webcam_backend=config.webcam_backend,
        webcam_framerate=config.webcam_framerate,
        webcam_resolution=config.webcam_resolution,
        webcam_input_format=config.webcam_input_format,
    )
    state = CameraState(
        camera_id=config.camera_id,
        run_mode=config.run_mode,
        source_kind=source.kind,
        source_uri=source.uri,
        source_details=source.state_fields(),
        rtsp_url=config.rtsp_url,
        lab_mode=config.lab_mode,
    )
    state.configure_beacon(
        channel="primary",
        enabled=config.primary_beacon_enabled,
        target_url=config.primary_control_url,
        interval_seconds=config.primary_beacon_interval_seconds,
    )
    state.configure_poller(
        channel="primary",
        enabled=config.primary_poll_enabled,
        target_url=config.primary_control_url,
        interval_seconds=config.primary_poll_interval_seconds,
    )
    scenario_logger = ScenarioLogger.from_env(service_name="camera-app")
    streamer = StreamerSupervisor(
        config=StreamerConfig(
            source=source,
            camera_id=config.camera_id,
            rtsp_url=config.rtsp_url,
            rtsp_transport=config.rtsp_transport,
            ffmpeg_binary=config.ffmpeg_binary,
            overlay_fontfile=config.overlay_fontfile,
            restart_delay_seconds=config.restart_delay_seconds,
            publish_probe_seconds=config.publish_probe_seconds,
        ),
        state=state,
        logger=logging.getLogger("camera-app.streamer"),
    )
    beacon = BeaconWorker(
        config=BeaconConfig(
            channel_name="primary",
            enabled=config.primary_beacon_enabled,
            control_url=config.primary_control_url,
            interval_seconds=config.primary_beacon_interval_seconds,
            request_timeout_seconds=config.primary_beacon_timeout_seconds,
        ),
        state=state,
        scenario_logger=scenario_logger,
        logger=logging.getLogger("camera-app.beacon"),
    )
    poller = TaskPoller(
        config=PollerConfig(
            channel_name="primary",
            enabled=config.primary_poll_enabled,
            control_url=config.primary_control_url,
            camera_id=config.camera_id,
            interval_seconds=config.primary_poll_interval_seconds,
            request_timeout_seconds=config.primary_poll_timeout_seconds,
        ),
        state=state,
        scenario_logger=scenario_logger,
        logger=logging.getLogger("camera-app.poller"),
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        logger.info(
            "camera-app starting with run_mode=%s lab_mode=%s source_type=%s rtsp_transport=%s primary_control_url=%s",
            config.run_mode,
            config.lab_mode,
            config.source_type,
            config.rtsp_transport,
            config.primary_control_url,
        )
        scenario_logger.event(
            event_type="camera.lifecycle.start",
            phase="startup",
            camera_id=config.camera_id,
            source=config.camera_id,
            target=config.primary_control_url,
            result="starting",
            details={
                "run_mode": config.run_mode,
                "lab_mode": config.lab_mode,
                "source_type": config.source_type,
                "rtsp_url": config.rtsp_url,
                "primary_beacon_enabled": config.primary_beacon_enabled,
                "primary_poll_enabled": config.primary_poll_enabled,
            },
        )
        streamer.start()
        beacon.start()
        poller.start()
        try:
            yield
        finally:
            logger.info("camera-app shutting down")
            scenario_logger.event(
                event_type="camera.lifecycle.stop",
                phase="shutdown",
                camera_id=config.camera_id,
                source=config.camera_id,
                target=config.primary_control_url,
                result="stopping",
                details={"lab_mode": config.lab_mode},
            )
            poller.stop()
            beacon.stop()
            streamer.stop()

    app = FastAPI(
        title="camera-app",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.runtime_config = config
    app.state.camera_state = state

    @app.get("/health")
    def health(request: Request) -> JSONResponse:
        snapshot = request.app.state.camera_state.snapshot()
        stream_status = snapshot["stream"]["status"]
        is_healthy = stream_status in {"starting", "publishing"}
        payload = {
            "status": "ok" if is_healthy else "degraded",
            "camera_id": snapshot["camera_id"],
            "stream_status": stream_status,
            "lab_mode": snapshot["lab_mode"],
            "primary_control_url": snapshot["control_channels"]["primary"]["base_url"],
        }
        return JSONResponse(
            payload,
            status_code=200 if is_healthy else 503,
        )

    @app.get("/status")
    def status(request: Request) -> dict:
        return request.app.state.camera_state.snapshot()

    return app


app = create_app()


if __name__ == "__main__":
    config: AppConfig = app.state.runtime_config
    uvicorn.run(
        app,
        host=config.api_host,
        port=config.api_port,
        log_level=os.getenv("UVICORN_LOG_LEVEL", "info"),
    )
