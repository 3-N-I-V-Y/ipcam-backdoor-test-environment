from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import threading
from urllib import error, request

from state import CameraState


@dataclass(slots=True)
class BeaconConfig:
    enabled: bool
    control_url: str
    interval_seconds: float = 10.0
    request_timeout_seconds: float = 3.0


class BeaconWorker:
    def __init__(
        self,
        *,
        config: BeaconConfig,
        state: CameraState,
        logger: logging.Logger | None = None,
    ) -> None:
        self._config = config
        self._state = state
        self._logger = logger or logging.getLogger(__name__)
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._endpoint = f"{self._config.control_url.rstrip('/')}/beacon"

    def start(self) -> None:
        if not self._config.enabled:
            self._logger.info("beacon mode disabled")
            return

        if self._thread and self._thread.is_alive():
            return

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop,
            name="camera-beacon",
            daemon=True,
        )
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        if not self._config.enabled:
            return

        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=timeout)
        self._state.mark_beacon_stopped()

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

        self._state.mark_beacon_sending()

        try:
            with request.urlopen(req, timeout=self._config.request_timeout_seconds) as response:
                self._state.mark_beacon_sent(response_status=response.status)
        except error.HTTPError as exc:
            message = f"beacon HTTP error: {exc.code}"
            self._logger.warning(message)
            self._state.mark_beacon_failed(
                error=message,
                response_status=exc.code,
            )
        except error.URLError as exc:
            message = f"beacon connection error: {exc.reason}"
            self._logger.warning(message)
            self._state.mark_beacon_failed(error=message)
        except Exception as exc:
            message = f"beacon unexpected error: {exc}"
            self._logger.exception(message)
            self._state.mark_beacon_failed(error=message)

    def _build_payload(self) -> dict:
        snapshot = self._state.snapshot()
        return {
            "camera_id": snapshot["camera_id"],
            "lab_mode": snapshot["lab_mode"],
            "stream_state": snapshot["stream"]["status"],
            "source_kind": snapshot["source"]["kind"],
            "controls": snapshot["controls"],
            "uptime_seconds": snapshot["uptime_seconds"],
            "updated_at": snapshot["updated_at"],
        }
