from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import threading
from urllib import error, request

from common.scenario_logger import ScenarioLogger
from state import CameraState


@dataclass(slots=True)
class BeaconConfig:
    enabled: bool
    control_url: str
    channel_name: str = "primary"
    interval_seconds: float = 10.0
    request_timeout_seconds: float = 3.0


class BeaconWorker:
    def __init__(
        self,
        *,
        config: BeaconConfig,
        state: CameraState,
        scenario_logger: ScenarioLogger | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._config = config
        self._state = state
        self._scenario_logger = scenario_logger
        self._logger = logger or logging.getLogger(__name__)
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._endpoint = f"{self._config.control_url.rstrip('/')}/beacon"

    def start(self) -> None:
        if not self._config.enabled:
            self._logger.info("%s beacon disabled", self._config.channel_name)
            return

        if self._thread and self._thread.is_alive():
            return

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop,
            name=f"camera-beacon-{self._config.channel_name}",
            daemon=True,
        )
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        if not self._config.enabled:
            return

        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=timeout)
        self._state.mark_beacon_stopped(channel=self._config.channel_name)

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            self._send_once()
            if self._stop_event.wait(self._config.interval_seconds):
                break

    def _send_once(self) -> None:
        payload = self._build_payload()
        body = json.dumps(payload).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Content-Length": str(len(body)),
        }
        req = request.Request(
            self._endpoint,
            data=body,
            headers=headers,
            method="POST",
        )

        self._state.mark_beacon_sending(channel=self._config.channel_name)

        try:
            with request.urlopen(req, timeout=self._config.request_timeout_seconds) as response:
                self._state.mark_beacon_sent(
                    channel=self._config.channel_name,
                    response_status=response.status,
                )
                self._log_beacon_event(
                    payload=payload,
                    result="success",
                    details={"response_status": response.status},
                )
        except error.HTTPError as exc:
            message = f"{self._config.channel_name} beacon HTTP error: {exc.code}"
            self._logger.warning(message)
            self._state.mark_beacon_failed(
                channel=self._config.channel_name,
                error=message,
                response_status=exc.code,
            )
            self._log_beacon_event(
                payload=payload,
                result="failed",
                details={"error": message, "response_status": exc.code},
            )
        except error.URLError as exc:
            message = f"{self._config.channel_name} beacon connection error: {exc.reason}"
            self._logger.warning(message)
            self._state.mark_beacon_failed(
                channel=self._config.channel_name,
                error=message,
            )
            self._log_beacon_event(payload=payload, result="failed", details={"error": message})
        except Exception as exc:
            message = f"{self._config.channel_name} beacon unexpected error: {exc}"
            self._logger.exception(message)
            self._state.mark_beacon_failed(
                channel=self._config.channel_name,
                error=message,
            )
            self._log_beacon_event(payload=payload, result="failed", details={"error": message})

    def _build_payload(self) -> dict:
        snapshot = self._state.snapshot()
        return {
            "camera_id": snapshot["camera_id"],
            "control_channel": self._config.channel_name,
            "lab_mode": snapshot["lab_mode"],
            "stream_state": snapshot["stream"]["status"],
            "source_kind": snapshot["source"]["kind"],
            "controls": snapshot["controls"],
            "uptime_seconds": snapshot["uptime_seconds"],
            "updated_at": snapshot["updated_at"],
        }

    def _log_beacon_event(self, *, payload: dict, result: str, details: dict) -> None:
        if not self._scenario_logger:
            return

        self._scenario_logger.event(
            event_type="camera.beacon.sent",
            phase="control_beacon",
            camera_id=str(payload.get("camera_id") or ""),
            source=str(payload.get("camera_id") or ""),
            target=self._config.control_url,
            result=result,
            details={
                "control_channel": self._config.channel_name,
                "lab_mode": payload.get("lab_mode"),
                "stream_state": payload.get("stream_state"),
                **details,
            },
        )
