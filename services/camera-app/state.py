from __future__ import annotations

from collections import deque
from copy import deepcopy
from datetime import UTC, datetime
import threading
import time
from typing import Any


VALID_QUALITY_LEVELS = {"low", "medium", "high"}


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


class CameraState:
    def __init__(
        self,
        *,
        camera_id: str,
        source_kind: str,
        source_uri: str,
        rtsp_url: str,
        lab_mode: str,
    ) -> None:
        self._lock = threading.RLock()
        self._started_at_monotonic = time.monotonic()
        self._status: dict[str, Any] = {
            "camera_id": camera_id,
            "lab_mode": lab_mode,
            "started_at": utc_now(),
            "source": {
                "kind": source_kind,
                "uri": source_uri,
            },
            "stream": {
                "status": "idle",
                "target_url": rtsp_url,
                "ffmpeg_pid": None,
                "restart_count": 0,
                "last_start_at": None,
                "last_exit_at": None,
                "last_exit_code": None,
                "last_error": None,
            },
            "controls": {
                "quality": "high",
                "overlay_enabled": False,
            },
            "beacon": {
                "enabled": False,
                "status": "disabled",
                "target_url": None,
                "interval_seconds": None,
                "last_attempt_at": None,
                "last_sent_at": None,
                "last_response_status": None,
                "last_error": None,
                "consecutive_failures": 0,
            },
            "markers": deque(maxlen=50),
            "updated_at": utc_now(),
        }

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            snapshot = deepcopy(self._status)
            snapshot["markers"] = list(snapshot["markers"])
            snapshot["uptime_seconds"] = round(
                time.monotonic() - self._started_at_monotonic,
                3,
            )
            return snapshot

    def mark_stream_starting(self, pid: int | None = None) -> None:
        with self._lock:
            stream = self._status["stream"]
            stream["status"] = "starting"
            stream["ffmpeg_pid"] = pid
            stream["last_start_at"] = utc_now()
            stream["last_error"] = None
            self._touch()

    def mark_stream_publishing(self, pid: int | None = None) -> None:
        with self._lock:
            stream = self._status["stream"]
            stream["status"] = "publishing"
            stream["ffmpeg_pid"] = pid
            stream["last_error"] = None
            self._touch()

    def mark_stream_retrying(self, *, exit_code: int | None, error: str) -> None:
        with self._lock:
            stream = self._status["stream"]
            stream["status"] = "retrying"
            stream["ffmpeg_pid"] = None
            stream["last_exit_at"] = utc_now()
            stream["last_exit_code"] = exit_code
            stream["last_error"] = error
            stream["restart_count"] += 1
            self._touch()

    def mark_stream_stopped(
        self,
        *,
        exit_code: int | None,
        error: str | None = None,
    ) -> None:
        with self._lock:
            stream = self._status["stream"]
            stream["status"] = "stopped"
            stream["ffmpeg_pid"] = None
            stream["last_exit_at"] = utc_now()
            stream["last_exit_code"] = exit_code
            stream["last_error"] = error
            self._touch()

    def configure_beacon(
        self,
        *,
        enabled: bool,
        target_url: str | None,
        interval_seconds: float | None,
    ) -> None:
        with self._lock:
            beacon = self._status["beacon"]
            beacon["enabled"] = enabled
            beacon["status"] = "idle" if enabled else "disabled"
            beacon["target_url"] = target_url if enabled else None
            beacon["interval_seconds"] = interval_seconds if enabled else None
            beacon["last_error"] = None
            self._touch()

    def mark_beacon_sending(self) -> None:
        with self._lock:
            beacon = self._status["beacon"]
            if not beacon["enabled"]:
                return
            beacon["status"] = "sending"
            beacon["last_attempt_at"] = utc_now()
            beacon["last_error"] = None
            self._touch()

    def mark_beacon_sent(self, *, response_status: int) -> None:
        with self._lock:
            beacon = self._status["beacon"]
            if not beacon["enabled"]:
                return
            beacon["status"] = "ok"
            beacon["last_sent_at"] = utc_now()
            beacon["last_response_status"] = response_status
            beacon["last_error"] = None
            beacon["consecutive_failures"] = 0
            self._touch()

    def mark_beacon_failed(
        self,
        *,
        error: str,
        response_status: int | None = None,
    ) -> None:
        with self._lock:
            beacon = self._status["beacon"]
            if not beacon["enabled"]:
                return
            beacon["status"] = "error"
            beacon["last_response_status"] = response_status
            beacon["last_error"] = error
            beacon["consecutive_failures"] += 1
            self._touch()

    def mark_beacon_stopped(self) -> None:
        with self._lock:
            beacon = self._status["beacon"]
            if not beacon["enabled"]:
                return
            beacon["status"] = "stopped"
            self._touch()

    def apply_safe_command(self, command: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        params = params or {}
        normalized_command = {
            "status": "get_status",
            "marker": "record_marker",
        }.get(command, command)

        with self._lock:
            if normalized_command == "noop":
                self._touch()
                return {"accepted": True, "command": normalized_command}

            if normalized_command == "get_status":
                return {
                    "accepted": True,
                    "command": normalized_command,
                    "status": self.snapshot(),
                }

            if normalized_command == "set_quality":
                quality = params.get("quality")
                if quality not in VALID_QUALITY_LEVELS:
                    raise ValueError(f"quality must be one of {sorted(VALID_QUALITY_LEVELS)}")
                self._status["controls"]["quality"] = quality
                self._touch()
                return {
                    "accepted": True,
                    "command": normalized_command,
                    "quality": quality,
                }

            if normalized_command == "toggle_overlay":
                enabled = params.get("enabled")
                if enabled is None:
                    enabled = not self._status["controls"]["overlay_enabled"]
                if not isinstance(enabled, bool):
                    raise ValueError("enabled must be a boolean")
                self._status["controls"]["overlay_enabled"] = enabled
                self._touch()
                return {
                    "accepted": True,
                    "command": normalized_command,
                    "overlay_enabled": enabled,
                }

            if normalized_command == "record_marker":
                note = str(params.get("note", "")).strip()
                if not note:
                    raise ValueError("note is required")
                marker = {
                    "at": utc_now(),
                    "note": note[:200],
                }
                self._status["markers"].append(marker)
                self._touch()
                return {
                    "accepted": True,
                    "command": normalized_command,
                    "marker": marker,
                }

            raise ValueError(f"unsupported safe command: {command}")

    def _touch(self) -> None:
        self._status["updated_at"] = utc_now()
