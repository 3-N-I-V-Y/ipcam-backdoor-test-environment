from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
import logging
import os

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import uvicorn

from beacon import BeaconConfig, BeaconWorker
from poller import PollerConfig, TaskPoller
from state import CameraState
from streamer import StreamerConfig, StreamerSupervisor


logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("camera-app")


@dataclass(slots=True)
class AppConfig:
    camera_id: str
    lab_mode: str
    beacon_enabled: bool
    poll_enabled: bool
    input_source: str
    rtsp_url: str
    control_url: str
    api_host: str
    api_port: int
    ffmpeg_binary: str
    restart_delay_seconds: float
    publish_probe_seconds: float
    beacon_interval_seconds: float
    beacon_timeout_seconds: float
    poll_interval_seconds: float
    poll_timeout_seconds: float

    @classmethod
    def from_env(cls) -> "AppConfig":
        lab_mode, features = parse_lab_mode(os.getenv("LAB_MODE", "none"))

        return cls(
            camera_id=os.getenv("CAMERA_ID", "camera-app-001"),
            lab_mode=lab_mode,
            beacon_enabled="beacon" in features,
            poll_enabled="poll" in features,
            input_source=os.getenv("INPUT_SOURCE", "/samples/demo.mp4"),
            rtsp_url=os.getenv("RTSP_URL", "rtsp://mediamtx:8554/cam1"),
            control_url=os.getenv("CONTROL_URL", "http://control-server:8080"),
            api_host=os.getenv("API_HOST", "0.0.0.0"),
            api_port=int(os.getenv("API_PORT", "8090")),
            ffmpeg_binary=os.getenv("FFMPEG_BIN", "ffmpeg"),
            restart_delay_seconds=float(os.getenv("RESTART_DELAY_SECONDS", "3")),
            publish_probe_seconds=float(os.getenv("PUBLISH_PROBE_SECONDS", "1.5")),
            beacon_interval_seconds=float(os.getenv("BEACON_INTERVAL_SECONDS", "10")),
            beacon_timeout_seconds=float(os.getenv("BEACON_TIMEOUT_SECONDS", "3")),
            poll_interval_seconds=float(os.getenv("POLL_INTERVAL_SECONDS", "10")),
            poll_timeout_seconds=float(os.getenv("POLL_TIMEOUT_SECONDS", "3")),
        )


def parse_lab_mode(raw_value: str) -> tuple[str, set[str]]:
    normalized = raw_value.strip().lower()
    if not normalized or normalized == "none":
        return "none", set()

    tokens = {token.strip() for token in normalized.split(",") if token.strip()}
    allowed = {"beacon", "poll"}
    invalid = sorted(tokens - allowed)
    if invalid:
        raise ValueError("LAB_MODE must contain only: none, beacon, poll")

    return ",".join(sorted(tokens)), tokens


def create_app() -> FastAPI:
    config = AppConfig.from_env()
    state = CameraState(
        camera_id=config.camera_id,
        source_kind="file",
        source_uri=config.input_source,
        rtsp_url=config.rtsp_url,
        lab_mode=config.lab_mode,
    )
    state.configure_beacon(
        enabled=config.beacon_enabled,
        target_url=config.control_url,
        interval_seconds=config.beacon_interval_seconds,
    )
    state.configure_poller(
        enabled=config.poll_enabled,
        target_url=config.control_url,
        interval_seconds=config.poll_interval_seconds,
    )
    streamer = StreamerSupervisor(
        config=StreamerConfig(
            input_source=config.input_source,
            rtsp_url=config.rtsp_url,
            ffmpeg_binary=config.ffmpeg_binary,
            restart_delay_seconds=config.restart_delay_seconds,
            publish_probe_seconds=config.publish_probe_seconds,
        ),
        state=state,
        logger=logging.getLogger("camera-app.streamer"),
    )
    beacon = BeaconWorker(
        config=BeaconConfig(
            enabled=config.beacon_enabled,
            control_url=config.control_url,
            interval_seconds=config.beacon_interval_seconds,
            request_timeout_seconds=config.beacon_timeout_seconds,
        ),
        state=state,
        logger=logging.getLogger("camera-app.beacon"),
    )
    poller = TaskPoller(
        config=PollerConfig(
            enabled=config.poll_enabled,
            control_url=config.control_url,
            camera_id=config.camera_id,
            interval_seconds=config.poll_interval_seconds,
            request_timeout_seconds=config.poll_timeout_seconds,
        ),
        state=state,
        logger=logging.getLogger("camera-app.poller"),
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        logger.info("camera-app starting with lab_mode=%s", config.lab_mode)
        streamer.start()
        beacon.start()
        poller.start()
        try:
            yield
        finally:
            logger.info("camera-app shutting down")
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
