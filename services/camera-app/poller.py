from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import threading
from urllib import error, parse, request

from state import CameraState


SAFE_POLL_COMMANDS = {
    "noop",
    "get_status",
    "status",
    "record_marker",
    "marker",
    "set_quality",
    "toggle_overlay",
}


@dataclass(slots=True)
class PollerConfig:
    enabled: bool
    control_url: str
    camera_id: str
    channel_name: str = "primary"
    interval_seconds: float = 10.0
    request_timeout_seconds: float = 3.0


class TaskPoller:
    def __init__(
        self,
        *,
        config: PollerConfig,
        state: CameraState,
        logger: logging.Logger | None = None,
    ) -> None:
        self._config = config
        self._state = state
        self._logger = logger or logging.getLogger(__name__)
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._task_endpoint = f"{self._config.control_url.rstrip('/')}/task"
        self._result_endpoint = f"{self._config.control_url.rstrip('/')}/result"

    def start(self) -> None:
        if not self._config.enabled:
            self._logger.info("%s poller disabled", self._config.channel_name)
            return

        if self._thread and self._thread.is_alive():
            return

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop,
            name=f"camera-poller-{self._config.channel_name}",
            daemon=True,
        )
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        if not self._config.enabled:
            return

        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=timeout)
        self._state.mark_poller_stopped(channel=self._config.channel_name)

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            self._poll_once()
            if self._stop_event.wait(self._config.interval_seconds):
                break

    def _poll_once(self) -> None:
        self._state.mark_polling(channel=self._config.channel_name)
        url = f"{self._task_endpoint}?{parse.urlencode({'camera_id': self._config.camera_id})}"

        try:
            payload = self._get_json(url)
        except error.HTTPError as exc:
            message = f"{self._config.channel_name} task poll HTTP error: {exc.code}"
            self._logger.warning(message)
            self._state.mark_poll_failed(
                channel=self._config.channel_name,
                error=message,
                response_status=exc.code,
            )
            return
        except error.URLError as exc:
            message = f"{self._config.channel_name} task poll connection error: {exc.reason}"
            self._logger.warning(message)
            self._state.mark_poll_failed(
                channel=self._config.channel_name,
                error=message,
            )
            return
        except Exception as exc:
            message = f"{self._config.channel_name} task poll unexpected error: {exc}"
            self._logger.exception(message)
            self._state.mark_poll_failed(
                channel=self._config.channel_name,
                error=message,
            )
            return

        task = payload.get("task")
        if task is None:
            self._state.mark_poll_idle(channel=self._config.channel_name)
            return

        if not isinstance(task, dict):
            self._state.mark_poll_failed(
                channel=self._config.channel_name,
                error="task payload must be an object",
            )
            return

        task_id = str(task.get("id") or "")
        command = str(task.get("command") or "").strip()
        params = task.get("params") if isinstance(task.get("params"), dict) else {}

        self._state.mark_task_received(
            channel=self._config.channel_name,
            task_id=task_id,
            command=command,
        )

        if command not in SAFE_POLL_COMMANDS:
            error_message = f"unsupported polled command: {command}"
            self._logger.warning(error_message)
            result_payload = self._build_result_payload(
                task_id=task_id,
                command=command,
                params=params,
                success=False,
                output=None,
                error_message=error_message,
            )
            self._state.mark_task_finished(
                channel=self._config.channel_name,
                task_id=task_id,
                command=command,
                result=result_payload,
            )
            self._post_result(result_payload)
            return

        try:
            output = self._state.apply_safe_command(command, params)
            result_payload = self._build_result_payload(
                task_id=task_id,
                command=command,
                params=params,
                success=True,
                output=output,
                error_message=None,
            )
        except Exception as exc:
            error_message = str(exc)
            self._logger.warning("task execution failed: %s", error_message)
            result_payload = self._build_result_payload(
                task_id=task_id,
                command=command,
                params=params,
                success=False,
                output=None,
                error_message=error_message,
            )

        self._state.mark_task_finished(
            channel=self._config.channel_name,
            task_id=task_id,
            command=command,
            result=result_payload,
        )
        self._post_result(result_payload)

    def _post_result(self, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Content-Length": str(len(body)),
        }
        req = request.Request(
            self._result_endpoint,
            data=body,
            headers=headers,
            method="POST",
        )

        try:
            with request.urlopen(req, timeout=self._config.request_timeout_seconds) as response:
                self._state.mark_result_reported(
                    channel=self._config.channel_name,
                    response_status=response.status,
                )
        except error.HTTPError as exc:
            message = f"{self._config.channel_name} result report HTTP error: {exc.code}"
            self._logger.warning(message)
            self._state.mark_poll_failed(
                channel=self._config.channel_name,
                error=message,
                response_status=exc.code,
            )
        except error.URLError as exc:
            message = f"{self._config.channel_name} result report connection error: {exc.reason}"
            self._logger.warning(message)
            self._state.mark_poll_failed(
                channel=self._config.channel_name,
                error=message,
            )
        except Exception as exc:
            message = f"{self._config.channel_name} result report unexpected error: {exc}"
            self._logger.exception(message)
            self._state.mark_poll_failed(
                channel=self._config.channel_name,
                error=message,
            )

    def _get_json(self, url: str) -> dict:
        req = request.Request(url, method="GET")
        with request.urlopen(req, timeout=self._config.request_timeout_seconds) as response:
            body = response.read().decode("utf-8")
        payload = json.loads(body or "{}")
        if not isinstance(payload, dict):
            raise ValueError("response payload must be an object")
        return payload

    def _build_result_payload(
        self,
        *,
        task_id: str,
        command: str,
        params: dict,
        success: bool,
        output: dict | None,
        error_message: str | None,
    ) -> dict:
        snapshot = self._state.snapshot()
        return {
            "camera_id": snapshot["camera_id"],
            "control_channel": self._config.channel_name,
            "task_id": task_id,
            "command": command,
            "params": params,
            "success": success,
            "output": output,
            "error": error_message,
            "lab_mode": snapshot["lab_mode"],
            "stream_state": snapshot["stream"]["status"],
            "updated_at": snapshot["updated_at"],
        }
