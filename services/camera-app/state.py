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


def default_beacon_state() -> dict[str, Any]:
    return {
        "enabled": False,
        "status": "disabled",
        "target_url": None,
        "interval_seconds": None,
        "last_attempt_at": None,
        "last_sent_at": None,
        "last_response_status": None,
        "last_error": None,
        "consecutive_failures": 0,
    }


def default_poller_state() -> dict[str, Any]:
    return {
        "enabled": False,
        "status": "disabled",
        "target_url": None,
        "interval_seconds": None,
        "last_poll_at": None,
        "last_result_posted_at": None,
        "last_result_response_status": None,
        "last_error": None,
        "consecutive_failures": 0,
        "last_task_id": None,
        "last_task_command": None,
        "last_task_status": None,
        "last_task_received_at": None,
        "last_task_completed_at": None,
        "last_task_result": None,
    }


def default_infected_scan_state() -> dict[str, Any]:
    return {
        "enabled": False,
        "status": "disabled",
        "targets": [],
        "ports": [],
        "interval_seconds": None,
        "connect_timeout_seconds": None,
        "block_external": True,
        "last_attempt_at": None,
        "last_target": None,
        "last_port": None,
        "last_result": None,
        "last_elapsed_ms": None,
        "last_error": None,
        "attempt_count": 0,
        "open_count": 0,
        "closed_count": 0,
        "timeout_count": 0,
        "blocked_count": 0,
        "error_count": 0,
    }


class CameraState:
    def __init__(
        self,
        *,
        camera_id: str,
        run_mode: str,
        source_kind: str,
        source_uri: str,
        source_details: dict[str, Any] | None = None,
        rtsp_url: str,
        lab_mode: str,
    ) -> None:
        self._lock = threading.RLock()
        self._started_at_monotonic = time.monotonic()
        source = {
            "kind": source_kind,
            "uri": source_uri,
        }
        if source_details:
            source.update(source_details)
        self._status: dict[str, Any] = {
            "camera_id": camera_id,
            "run_mode": run_mode,
            "lab_mode": lab_mode,
            "started_at": utc_now(),
            "source": source,
            "stream": {
                "status": "idle",
                "target_url": rtsp_url,
                "ffmpeg_pid": None,
                "restart_count": 0,
                "config_revision": 0,
                "applied_quality": None,
                "applied_overlay_enabled": False,
                "last_start_at": None,
                "last_exit_at": None,
                "last_exit_code": None,
                "last_error": None,
                "last_restart_reason": None,
            },
            "controls": {
                "quality": "high",
                "overlay_enabled": False,
            },
            "control_channels": {
                "primary": {
                    "name": "primary",
                    "role": "normal-management",
                    "base_url": None,
                    "beacon": default_beacon_state(),
                    "poller": default_poller_state(),
                },
            },
            "beacon": default_beacon_state(),
            "poller": default_poller_state(),
            "infected_scan": default_infected_scan_state(),
            "markers": deque(maxlen=50),
            "updated_at": utc_now(),
        }
        self._sync_primary_aliases()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            snapshot = deepcopy(self._status)
            snapshot["markers"] = list(snapshot["markers"])
            snapshot["uptime_seconds"] = round(
                time.monotonic() - self._started_at_monotonic,
                3,
            )
            return snapshot

    def current_stream_settings(self) -> dict[str, Any]:
        with self._lock:
            return {
                "camera_id": self._status["camera_id"],
                "quality": self._status["controls"]["quality"],
                "overlay_enabled": self._status["controls"]["overlay_enabled"],
                "config_revision": self._status["stream"]["config_revision"],
                "restart_reason": self._status["stream"]["last_restart_reason"],
            }

    def mark_stream_starting(
        self,
        pid: int | None = None,
        *,
        quality: str | None = None,
        overlay_enabled: bool | None = None,
    ) -> None:
        with self._lock:
            stream = self._status["stream"]
            stream["status"] = "starting"
            stream["ffmpeg_pid"] = pid
            stream["applied_quality"] = quality
            if overlay_enabled is not None:
                stream["applied_overlay_enabled"] = overlay_enabled
            stream["last_start_at"] = utc_now()
            stream["last_error"] = None
            stream["last_restart_reason"] = None
            self._touch()

    def mark_stream_publishing(
        self,
        pid: int | None = None,
        *,
        quality: str | None = None,
        overlay_enabled: bool | None = None,
    ) -> None:
        with self._lock:
            stream = self._status["stream"]
            stream["status"] = "publishing"
            stream["ffmpeg_pid"] = pid
            stream["applied_quality"] = quality
            if overlay_enabled is not None:
                stream["applied_overlay_enabled"] = overlay_enabled
            stream["last_error"] = None
            stream["last_restart_reason"] = None
            self._touch()

    def mark_stream_restarting(self, *, reason: str) -> None:
        with self._lock:
            stream = self._status["stream"]
            stream["status"] = "restarting"
            stream["ffmpeg_pid"] = None
            stream["last_error"] = None
            stream["last_restart_reason"] = reason
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
        channel: str = "primary",
        enabled: bool,
        target_url: str | None,
        interval_seconds: float | None,
    ) -> None:
        with self._lock:
            control_channel = self._ensure_control_channel(channel)
            control_channel["base_url"] = target_url
            beacon = control_channel["beacon"]
            beacon["enabled"] = enabled
            beacon["status"] = "idle" if enabled else "disabled"
            beacon["target_url"] = target_url if enabled else None
            beacon["interval_seconds"] = interval_seconds if enabled else None
            beacon["last_error"] = None
            self._sync_channel_aliases(channel)
            self._touch()

    def mark_beacon_sending(self, *, channel: str = "primary") -> None:
        with self._lock:
            beacon = self._ensure_control_channel(channel)["beacon"]
            if not beacon["enabled"]:
                return
            beacon["status"] = "sending"
            beacon["last_attempt_at"] = utc_now()
            beacon["last_error"] = None
            self._sync_channel_aliases(channel)
            self._touch()

    def mark_beacon_sent(self, *, channel: str = "primary", response_status: int) -> None:
        with self._lock:
            beacon = self._ensure_control_channel(channel)["beacon"]
            if not beacon["enabled"]:
                return
            beacon["status"] = "ok"
            beacon["last_sent_at"] = utc_now()
            beacon["last_response_status"] = response_status
            beacon["last_error"] = None
            beacon["consecutive_failures"] = 0
            self._sync_channel_aliases(channel)
            self._touch()

    def mark_beacon_failed(
        self,
        *,
        channel: str = "primary",
        error: str,
        response_status: int | None = None,
    ) -> None:
        with self._lock:
            beacon = self._ensure_control_channel(channel)["beacon"]
            if not beacon["enabled"]:
                return
            beacon["status"] = "error"
            beacon["last_response_status"] = response_status
            beacon["last_error"] = error
            beacon["consecutive_failures"] += 1
            self._sync_channel_aliases(channel)
            self._touch()

    def mark_beacon_stopped(self, *, channel: str = "primary") -> None:
        with self._lock:
            beacon = self._ensure_control_channel(channel)["beacon"]
            if not beacon["enabled"]:
                return
            beacon["status"] = "stopped"
            self._sync_channel_aliases(channel)
            self._touch()

    def configure_poller(
        self,
        *,
        channel: str = "primary",
        enabled: bool,
        target_url: str | None,
        interval_seconds: float | None,
    ) -> None:
        with self._lock:
            control_channel = self._ensure_control_channel(channel)
            control_channel["base_url"] = target_url
            poller = control_channel["poller"]
            poller["enabled"] = enabled
            poller["status"] = "idle" if enabled else "disabled"
            poller["target_url"] = target_url if enabled else None
            poller["interval_seconds"] = interval_seconds if enabled else None
            poller["last_error"] = None
            self._sync_channel_aliases(channel)
            self._touch()

    def mark_polling(self, *, channel: str = "primary") -> None:
        with self._lock:
            poller = self._ensure_control_channel(channel)["poller"]
            if not poller["enabled"]:
                return
            poller["status"] = "polling"
            poller["last_poll_at"] = utc_now()
            poller["last_error"] = None
            self._sync_channel_aliases(channel)
            self._touch()

    def mark_poll_idle(self, *, channel: str = "primary") -> None:
        with self._lock:
            poller = self._ensure_control_channel(channel)["poller"]
            if not poller["enabled"]:
                return
            poller["status"] = "idle"
            poller["last_error"] = None
            poller["consecutive_failures"] = 0
            self._sync_channel_aliases(channel)
            self._touch()

    def mark_task_received(self, *, channel: str = "primary", task_id: str, command: str) -> None:
        with self._lock:
            poller = self._ensure_control_channel(channel)["poller"]
            if not poller["enabled"]:
                return
            poller["status"] = "executing"
            poller["last_task_id"] = task_id
            poller["last_task_command"] = command
            poller["last_task_status"] = "received"
            poller["last_task_received_at"] = utc_now()
            poller["last_task_result"] = None
            poller["last_error"] = None
            self._sync_channel_aliases(channel)
            self._touch()

    def mark_task_finished(
        self,
        *,
        channel: str = "primary",
        task_id: str,
        command: str,
        result: dict[str, Any],
    ) -> None:
        with self._lock:
            poller = self._ensure_control_channel(channel)["poller"]
            if not poller["enabled"]:
                return
            poller["status"] = "idle"
            poller["last_task_id"] = task_id
            poller["last_task_command"] = command
            poller["last_task_status"] = "completed" if result.get("success") else "failed"
            poller["last_task_completed_at"] = utc_now()
            poller["last_task_result"] = result
            self._sync_channel_aliases(channel)
            self._touch()

    def mark_result_reported(self, *, channel: str = "primary", response_status: int) -> None:
        with self._lock:
            poller = self._ensure_control_channel(channel)["poller"]
            if not poller["enabled"]:
                return
            poller["last_result_posted_at"] = utc_now()
            poller["last_result_response_status"] = response_status
            poller["last_error"] = None
            poller["consecutive_failures"] = 0
            self._sync_channel_aliases(channel)
            self._touch()

    def mark_poll_failed(
        self,
        *,
        channel: str = "primary",
        error: str,
        response_status: int | None = None,
    ) -> None:
        with self._lock:
            poller = self._ensure_control_channel(channel)["poller"]
            if not poller["enabled"]:
                return
            poller["status"] = "error"
            poller["last_result_response_status"] = response_status
            poller["last_error"] = error
            poller["consecutive_failures"] += 1
            self._sync_channel_aliases(channel)
            self._touch()

    def mark_poller_stopped(self, *, channel: str = "primary") -> None:
        with self._lock:
            poller = self._ensure_control_channel(channel)["poller"]
            if not poller["enabled"]:
                return
            poller["status"] = "stopped"
            self._sync_channel_aliases(channel)
            self._touch()

    def configure_infected_scan(
        self,
        *,
        enabled: bool,
        targets: list[str],
        ports: list[int],
        interval_seconds: float,
        connect_timeout_seconds: float,
        block_external: bool,
    ) -> None:
        with self._lock:
            scan = self._status["infected_scan"]
            scan["enabled"] = enabled
            scan["status"] = "idle" if enabled else "disabled"
            scan["targets"] = targets
            scan["ports"] = ports
            scan["interval_seconds"] = interval_seconds if enabled else None
            scan["connect_timeout_seconds"] = connect_timeout_seconds if enabled else None
            scan["block_external"] = block_external
            scan["last_error"] = None
            self._touch()

    def mark_infected_scan_attempt(self, *, target: str, port: int) -> None:
        with self._lock:
            scan = self._status["infected_scan"]
            if not scan["enabled"]:
                return
            scan["status"] = "scanning"
            scan["last_attempt_at"] = utc_now()
            scan["last_target"] = target
            scan["last_port"] = port
            scan["last_result"] = None
            scan["last_elapsed_ms"] = None
            scan["last_error"] = None
            self._touch()

    def mark_infected_scan_result(
        self,
        *,
        target: str,
        port: int,
        result: str,
        elapsed_ms: int,
        error: str | None = None,
    ) -> None:
        with self._lock:
            scan = self._status["infected_scan"]
            if not scan["enabled"]:
                return
            scan["status"] = "idle"
            scan["last_target"] = target
            scan["last_port"] = port
            scan["last_result"] = result
            scan["last_elapsed_ms"] = elapsed_ms
            scan["last_error"] = error
            scan["attempt_count"] += 1

            counter_name = f"{result}_count"
            if counter_name in scan:
                scan[counter_name] += 1
            self._touch()

    def mark_infected_scan_stopped(self, *, error: str | None = None) -> None:
        with self._lock:
            scan = self._status["infected_scan"]
            if not scan["enabled"]:
                return
            scan["status"] = "stopped"
            if error is not None:
                scan["last_error"] = error
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
                previous_quality = self._status["controls"]["quality"]
                self._status["controls"]["quality"] = quality
                restart_requested = quality != previous_quality
                if restart_requested:
                    stream = self._status["stream"]
                    stream["config_revision"] += 1
                    stream["last_restart_reason"] = f"quality changed to {quality}"
                self._touch()
                return {
                    "accepted": True,
                    "command": normalized_command,
                    "quality": quality,
                    "restart_requested": restart_requested,
                }

            if normalized_command == "toggle_overlay":
                enabled = params.get("enabled")
                if enabled is None:
                    enabled = not self._status["controls"]["overlay_enabled"]
                if not isinstance(enabled, bool):
                    raise ValueError("enabled must be a boolean")
                previous_enabled = self._status["controls"]["overlay_enabled"]
                self._status["controls"]["overlay_enabled"] = enabled
                restart_requested = enabled != previous_enabled
                if restart_requested:
                    stream = self._status["stream"]
                    stream["config_revision"] += 1
                    state_label = "enabled" if enabled else "disabled"
                    stream["last_restart_reason"] = f"overlay {state_label}"
                self._touch()
                return {
                    "accepted": True,
                    "command": normalized_command,
                    "overlay_enabled": enabled,
                    "restart_requested": restart_requested,
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

    def _ensure_control_channel(self, channel: str) -> dict[str, Any]:
        channels = self._status.setdefault("control_channels", {})
        if channel not in channels:
            channels[channel] = {
                "name": channel,
                "role": "scenario" if channel != "primary" else "normal-management",
                "base_url": None,
                "beacon": default_beacon_state(),
                "poller": default_poller_state(),
            }
        return channels[channel]

    def _sync_primary_aliases(self) -> None:
        self._sync_channel_aliases("primary")

    def _sync_channel_aliases(self, channel: str) -> None:
        if channel != "primary":
            return

        primary = self._ensure_control_channel("primary")
        self._status["beacon"] = deepcopy(primary["beacon"])
        self._status["poller"] = deepcopy(primary["poller"])
