from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
import logging
import os
from pathlib import Path
import secrets
import sqlite3
import subprocess
import sys
import threading
import time
from typing import Any
from urllib import error, parse as urlparse, request as urllib_request

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import uvicorn

SERVICES_ROOT = Path(__file__).resolve().parents[1]
if (SERVICES_ROOT / "common").exists():
    sys.path.insert(0, str(SERVICES_ROOT))

from common.scenario_logger import ScenarioLogger


logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("nvr-console")


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def password_hash(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        240000,
    ).hex()


def format_bytes(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


def parse_iso_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None

    raw_value = value.strip()
    if raw_value.endswith("Z"):
        raw_value = f"{raw_value[:-1]}+00:00"

    try:
        parsed = datetime.fromisoformat(raw_value)
    except ValueError:
        return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def format_age_label(age_seconds: float | None) -> str:
    if age_seconds is None:
        return "never"

    age_seconds = max(0, int(age_seconds))
    if age_seconds < 5:
        return "just now"
    if age_seconds < 60:
        return f"{age_seconds}s ago"

    minutes, seconds = divmod(age_seconds, 60)
    if minutes < 60:
        return f"{minutes}m {seconds:02d}s ago"

    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m ago"


def sort_by_timestamp_desc(items: list[dict[str, Any]], timestamp_key: str) -> list[dict[str, Any]]:
    return sorted(
        items,
        key=lambda item: parse_iso_timestamp(item.get(timestamp_key)) or datetime.min.replace(tzinfo=UTC),
        reverse=True,
    )


def fragment_safe_id(value: str) -> str:
    cleaned = "".join(
        char if char.isascii() and (char.isalnum() or char in {"-", "_"}) else "-"
        for char in value
    ).strip("-")
    return cleaned or "device"


def summarize_camera_health(
    beacons: list[dict[str, Any]],
    *,
    camera_ids: list[str],
    heartbeat_ttl_seconds: float,
) -> dict[str, Any]:
    now = datetime.now(UTC)
    latest_by_camera: dict[str, tuple[datetime, dict[str, Any]]] = {}
    for beacon in beacons:
        camera_id = str(beacon.get("camera_id") or "").strip()
        received_at = parse_iso_timestamp(beacon.get("received_at"))
        if not camera_id or received_at is None:
            continue

        existing = latest_by_camera.get(camera_id)
        if existing is None or received_at > existing[0]:
            latest_by_camera[camera_id] = (received_at, beacon)

    tracked_camera_ids = [str(camera_id) for camera_id in camera_ids if str(camera_id).strip()]
    if not tracked_camera_ids:
        tracked_camera_ids = sorted(latest_by_camera)

    cameras: list[dict[str, Any]] = []
    for camera_id in tracked_camera_ids:
        latest = latest_by_camera.get(camera_id)
        received_at = latest[0] if latest else None
        age_seconds = (now - received_at).total_seconds() if received_at else None
        alive = age_seconds is not None and age_seconds <= heartbeat_ttl_seconds
        cameras.append(
            {
                "camera_id": camera_id,
                "status": "alive" if alive else "offline",
                "label": "Alive" if alive else "Offline",
                "alive": alive,
                "last_beacon_at": received_at.isoformat() if received_at else None,
                "last_beacon_age_seconds": age_seconds,
                "last_beacon_age_label": format_age_label(age_seconds),
            }
        )

    alive_count = sum(1 for camera in cameras if camera["alive"])
    total_count = len(cameras)
    if total_count == 0:
        status = "unknown"
        label = "Unknown"
        hint = "no cameras registered"
    elif alive_count == total_count:
        status = "alive"
        label = "Alive"
        hint = (
            f"last beacon {cameras[0]['last_beacon_age_label']}"
            if total_count == 1
            else f"{alive_count}/{total_count} cameras alive"
        )
    elif alive_count > 0:
        status = "degraded"
        label = "Degraded"
        hint = f"{alive_count}/{total_count} cameras alive"
    else:
        status = "offline"
        label = "Offline"
        hint = (
            f"last beacon {cameras[0]['last_beacon_age_label']}"
            if total_count == 1
            else "no recent camera heartbeat"
        )

    return {
        "status": status,
        "label": label,
        "hint": hint,
        "alive_count": alive_count,
        "total_count": total_count,
        "cameras": cameras,
    }


def attach_camera_health_details(
    control_overview: dict[str, Any],
    cameras: list[dict[str, Any]],
) -> dict[str, Any]:
    camera_lookup = {str(camera.get("camera_id")): camera for camera in cameras}
    camera_health: list[dict[str, Any]] = []
    for health in control_overview.get("camera_health", []):
        camera_id = str(health.get("camera_id") or "")
        camera = camera_lookup.get(camera_id, {})
        enriched = dict(health)
        enriched["display_name"] = camera.get("display_name") or camera_id
        enriched["location"] = camera.get("location") or ""
        enriched["stream_status"] = camera.get("stream_status") or "unknown"
        enriched["recording_mode"] = camera.get("recording_mode") or "unknown"
        enriched["rtsp_url"] = camera.get("rtsp_url") or ""
        enriched["status_url"] = camera.get("status_url") or ""
        enriched["fragment_id"] = f"device-{fragment_safe_id(camera_id)}"
        enriched["status_href"] = f"/devices/status#device-{fragment_safe_id(camera_id)}"
        enriched["detail_href"] = f"/cameras/{urlparse.quote(camera_id, safe='')}"
        camera_health.append(enriched)

    enriched_overview = dict(control_overview)
    enriched_overview["camera_health"] = camera_health
    return enriched_overview


def parse_recording_start(file_name: str) -> str | None:
    stem = Path(file_name).stem
    try:
        started_at = datetime.strptime(stem, "%Y%m%d_%H%M%S").replace(tzinfo=UTC)
    except ValueError:
        return None
    return started_at.isoformat()


@dataclass(slots=True)
class AppConfig:
    app_host: str
    app_port: int
    database_path: Path
    recordings_root: Path
    session_cookie_name: str
    session_ttl_seconds: int
    status_poll_interval_seconds: float
    recorder_poll_interval_seconds: float
    recording_segment_seconds: int
    recording_rtsp_transport: str
    live_preview_fps: int
    live_preview_width: int
    live_preview_jpeg_quality: int
    ffmpeg_binary: str
    admin_username: str
    admin_password: str
    password_salt: str
    default_camera_id: str
    default_camera_name: str
    default_camera_location: str
    default_camera_model: str
    default_camera_vendor: str
    default_camera_status_url: str
    default_camera_rtsp_url: str
    default_camera_hls_url: str
    default_retention_days: int
    control_server_url: str
    camera_heartbeat_ttl_seconds: float

    @classmethod
    def from_env(cls) -> "AppConfig":
        return cls(
            app_host=os.getenv("NVR_APP_HOST", "0.0.0.0"),
            app_port=int(os.getenv("NVR_APP_PORT", "8091")),
            database_path=Path(os.getenv("NVR_DB_PATH", "/data/nvr/nvr.sqlite3")),
            recordings_root=Path(os.getenv("NVR_RECORDINGS_ROOT", "/data/recordings")),
            session_cookie_name=os.getenv("NVR_SESSION_COOKIE_NAME", "nvr_session"),
            session_ttl_seconds=int(os.getenv("NVR_SESSION_TTL_SECONDS", "43200")),
            status_poll_interval_seconds=float(os.getenv("NVR_STATUS_POLL_INTERVAL_SECONDS", "5")),
            recorder_poll_interval_seconds=float(os.getenv("NVR_RECORDER_POLL_INTERVAL_SECONDS", "3")),
            recording_segment_seconds=int(os.getenv("NVR_RECORDING_SEGMENT_SECONDS", "60")),
            recording_rtsp_transport=os.getenv("NVR_RECORDING_RTSP_TRANSPORT", "tcp"),
            live_preview_fps=int(os.getenv("NVR_LIVE_PREVIEW_FPS", "5")),
            live_preview_width=int(os.getenv("NVR_LIVE_PREVIEW_WIDTH", "960")),
            live_preview_jpeg_quality=int(os.getenv("NVR_LIVE_PREVIEW_JPEG_QUALITY", "5")),
            ffmpeg_binary=os.getenv("FFMPEG_BIN", "ffmpeg"),
            admin_username=os.getenv("NVR_ADMIN_USERNAME", "admin"),
            admin_password=os.getenv("NVR_ADMIN_PASSWORD", "lab-admin"),
            password_salt=os.getenv("NVR_PASSWORD_SALT", "ipcam-lab"),
            default_camera_id=os.getenv("NVR_CAMERA_ID", "camera-app-001"),
            default_camera_name=os.getenv("NVR_CAMERA_NAME", "Lobby Entrance"),
            default_camera_location=os.getenv("NVR_CAMERA_LOCATION", "HQ / Entrance"),
            default_camera_model=os.getenv("NVR_CAMERA_MODEL", "LAB-CAM-1080P"),
            default_camera_vendor=os.getenv("NVR_CAMERA_VENDOR", "OpenCam Labs"),
            default_camera_status_url=os.getenv("NVR_CAMERA_STATUS_URL", "http://camera-app:8090/status"),
            default_camera_rtsp_url=os.getenv("NVR_CAMERA_RTSP_URL", "rtsp://mediamtx:8554/cam1"),
            default_camera_hls_url=os.getenv("NVR_CAMERA_HLS_URL", "http://localhost:8888/cam1/index.m3u8"),
            default_retention_days=int(os.getenv("NVR_DEFAULT_RETENTION_DAYS", "7")),
            control_server_url=os.getenv("NVR_CONTROL_SERVER_URL", "http://control-server:8080"),
            camera_heartbeat_ttl_seconds=float(os.getenv("NVR_CAMERA_HEARTBEAT_TTL_SECONDS", "30")),
        )


class NvrRepository:
    def __init__(
        self,
        database_path: Path,
        *,
        scenario_logger: ScenarioLogger | None = None,
    ) -> None:
        self._database_path = database_path
        self._scenario_logger = scenario_logger
        self._lock = threading.RLock()

    def _connect(self) -> sqlite3.Connection:
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self._database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def initialize(self) -> None:
        with self._lock, self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    username TEXT PRIMARY KEY,
                    password_hash TEXT NOT NULL,
                    role TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS cameras (
                    camera_id TEXT PRIMARY KEY,
                    display_name TEXT NOT NULL,
                    location TEXT NOT NULL,
                    model TEXT NOT NULL,
                    vendor TEXT NOT NULL,
                    status_url TEXT NOT NULL,
                    rtsp_url TEXT NOT NULL,
                    hls_url TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    recording_mode TEXT NOT NULL DEFAULT 'continuous',
                    retention_days INTEGER NOT NULL DEFAULT 7,
                    stream_status TEXT NOT NULL DEFAULT 'unknown',
                    last_seen_at TEXT,
                    last_status_json TEXT,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    actor TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    target_type TEXT NOT NULL,
                    target_id TEXT NOT NULL,
                    message TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )
            connection.commit()

    def bootstrap(self, config: AppConfig) -> None:
        admin_hash = password_hash(config.admin_password, config.password_salt)
        now = utc_now()

        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO users (username, password_hash, role, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(username) DO UPDATE SET
                    password_hash = excluded.password_hash,
                    role = excluded.role,
                    updated_at = excluded.updated_at
                """,
                (config.admin_username, admin_hash, "admin", now),
            )
            connection.execute(
                """
                INSERT INTO cameras (
                    camera_id,
                    display_name,
                    location,
                    model,
                    vendor,
                    status_url,
                    rtsp_url,
                    hls_url,
                    enabled,
                    recording_mode,
                    retention_days,
                    stream_status,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, 'continuous', ?, 'unknown', ?)
                ON CONFLICT(camera_id) DO UPDATE SET
                    status_url = excluded.status_url,
                    rtsp_url = excluded.rtsp_url,
                    hls_url = excluded.hls_url,
                    updated_at = excluded.updated_at
                """,
                (
                    config.default_camera_id,
                    config.default_camera_name,
                    config.default_camera_location,
                    config.default_camera_model,
                    config.default_camera_vendor,
                    config.default_camera_status_url,
                    config.default_camera_rtsp_url,
                    config.default_camera_hls_url,
                    config.default_retention_days,
                    now,
                ),
            )
            connection.commit()

    def verify_user(self, username: str, password: str, salt: str) -> bool:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT password_hash FROM users WHERE username = ?",
                (username,),
            ).fetchone()

        if row is None:
            return False
        return secrets.compare_digest(row["password_hash"], password_hash(password, salt))

    def list_cameras(self) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM cameras
                ORDER BY display_name ASC
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def get_camera(self, camera_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM cameras WHERE camera_id = ?",
                (camera_id,),
            ).fetchone()
        return dict(row) if row else None

    def list_recording_targets(self) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM cameras
                WHERE enabled = 1 AND recording_mode != 'disabled'
                ORDER BY camera_id ASC
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def update_camera_settings(
        self,
        *,
        camera_id: str,
        display_name: str,
        location: str,
        recording_mode: str,
        retention_days: int,
    ) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE cameras
                SET
                    display_name = ?,
                    location = ?,
                    recording_mode = ?,
                    retention_days = ?,
                    updated_at = ?
                WHERE camera_id = ?
                """,
                (
                    display_name,
                    location,
                    recording_mode,
                    retention_days,
                    utc_now(),
                    camera_id,
                ),
            )
            connection.commit()

    def update_camera_status(
        self,
        *,
        camera_id: str,
        stream_status: str,
        last_status_json: str,
        last_seen_at: str | None,
    ) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE cameras
                SET
                    stream_status = ?,
                    last_status_json = ?,
                    last_seen_at = ?,
                    updated_at = ?
                WHERE camera_id = ?
                """,
                (
                    stream_status,
                    last_status_json,
                    last_seen_at,
                    utc_now(),
                    camera_id,
                ),
            )
            connection.commit()

    def add_audit_event(
        self,
        *,
        actor: str,
        event_type: str,
        target_type: str,
        target_id: str,
        message: str,
    ) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO audit_events (
                    actor,
                    event_type,
                    target_type,
                    target_id,
                    message,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (actor, event_type, target_type, target_id, message, utc_now()),
            )
            connection.commit()

        if self._scenario_logger:
            self._scenario_logger.event(
                event_type=f"nvr.audit.{event_type}",
                phase="nvr_audit",
                source=actor,
                target=f"{target_type}:{target_id}",
                result="recorded",
                details={
                    "event_type": event_type,
                    "target_type": target_type,
                    "target_id": target_id,
                    "message": message,
                },
            )

    def list_audit_events(self, *, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM audit_events
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]


class SessionStore:
    def __init__(self, ttl_seconds: int) -> None:
        self._ttl_seconds = ttl_seconds
        self._lock = threading.RLock()
        self._sessions: dict[str, dict[str, Any]] = {}

    def create(self, username: str) -> str:
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._sessions[token] = {
                "username": username,
                "updated_at": time.time(),
            }
        return token

    def get_username(self, token: str | None) -> str | None:
        if not token:
            return None

        with self._lock:
            session = self._sessions.get(token)
            if session is None:
                return None

            if time.time() - session["updated_at"] > self._ttl_seconds:
                self._sessions.pop(token, None)
                return None

            session["updated_at"] = time.time()
            return str(session["username"])

    def delete(self, token: str | None) -> None:
        if not token:
            return
        with self._lock:
            self._sessions.pop(token, None)


class ControlServerClient:
    def __init__(self, base_url: str, timeout_seconds: float = 3.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds

    def health(self) -> dict[str, Any]:
        return self._get_json("/health")

    def list_beacons(self, *, camera_id: str | None = None) -> list[dict[str, Any]]:
        payload = self._get_json("/beacons", params={"camera_id": camera_id} if camera_id else None)
        items = payload.get("items", [])
        if isinstance(items, list):
            return items
        return []

    def list_tasks(self, *, camera_id: str | None = None) -> list[dict[str, Any]]:
        payload = self._get_json("/tasks", params={"camera_id": camera_id} if camera_id else None)
        items = payload.get("items", [])
        if isinstance(items, list):
            return items
        return []

    def list_results(self, *, camera_id: str | None = None) -> list[dict[str, Any]]:
        payload = self._get_json("/results", params={"camera_id": camera_id} if camera_id else None)
        items = payload.get("items", [])
        if isinstance(items, list):
            return items
        return []

    def create_task(self, *, camera_id: str, command: str, params: dict[str, Any]) -> dict[str, Any]:
        payload = {
            "camera_id": camera_id,
            "command": command,
            "params": params,
        }
        return self._post_json("/tasks", payload)

    def get_camera_control_state(self, camera_id: str) -> dict[str, Any]:
        beacons = sort_by_timestamp_desc(self.list_beacons(camera_id=camera_id), "received_at")
        tasks = self.list_tasks(camera_id=camera_id)
        results = sort_by_timestamp_desc(self.list_results(camera_id=camera_id), "received_at")
        return {
            "healthy": True,
            "error": None,
            "recent_beacons": beacons[:10],
            "pending_tasks": tasks[:10],
            "recent_results": results[:10],
            "last_beacon": beacons[0] if beacons else None,
            "last_result": results[0] if results else None,
        }

    def get_overview(self, camera_ids: list[str], *, heartbeat_ttl_seconds: float) -> dict[str, Any]:
        beacons = sort_by_timestamp_desc(self.list_beacons(), "received_at")
        tasks = self.list_tasks()
        results = sort_by_timestamp_desc(self.list_results(), "received_at")
        camera_health = summarize_camera_health(
            beacons,
            camera_ids=camera_ids,
            heartbeat_ttl_seconds=heartbeat_ttl_seconds,
        )
        return {
            "healthy": True,
            "error": None,
            "recent_beacons": beacons[:8],
            "recent_results": results[:8],
            "pending_tasks_count": len(tasks),
            "recent_beacons_count": len(beacons),
            "recent_results_count": len(results),
            "active_camera_count": camera_health["alive_count"],
            "camera_health": camera_health["cameras"],
            "channel_status": camera_health["status"],
            "channel_status_label": camera_health["label"],
            "channel_status_hint": camera_health["hint"],
            "heartbeat_ttl_seconds": heartbeat_ttl_seconds,
        }

    def _get_json(self, path: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        url = f"{self._base_url}{path}"
        if params:
            query = {
                key: value
                for key, value in params.items()
                if value is not None and str(value).strip()
            }
            if query:
                url = f"{url}?{urlparse.urlencode(query)}"
        request = urllib_request.Request(url, method="GET")
        with urllib_request.urlopen(request, timeout=self._timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8") or "{}")
        if not isinstance(payload, dict):
            raise ValueError("control server response must be an object")
        return payload

    def _post_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(payload).encode("utf-8")
        request = urllib_request.Request(
            f"{self._base_url}{path}",
            data=body,
            headers={
                "Content-Type": "application/json",
                "Content-Length": str(len(body)),
            },
            method="POST",
        )
        with urllib_request.urlopen(request, timeout=self._timeout_seconds) as response:
            data = json.loads(response.read().decode("utf-8") or "{}")
        if not isinstance(data, dict):
            raise ValueError("control server response must be an object")
        return data


class CameraStatusPoller:
    def __init__(
        self,
        *,
        repository: NvrRepository,
        interval_seconds: float,
        logger: logging.Logger | None = None,
    ) -> None:
        self._repository = repository
        self._interval_seconds = interval_seconds
        self._logger = logger or logging.getLogger(__name__)
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop,
            name="nvr-camera-poller",
            daemon=True,
        )
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=timeout)

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            for camera in self._repository.list_cameras():
                if not camera["enabled"]:
                    continue
                self._refresh_camera_status(camera)

            if self._stop_event.wait(self._interval_seconds):
                break

    def _refresh_camera_status(self, camera: dict[str, Any]) -> None:
        req = urllib_request.Request(str(camera["status_url"]), method="GET")
        try:
            with urllib_request.urlopen(req, timeout=3) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except error.HTTPError as exc:
            message = json.dumps({"error": f"HTTP {exc.code}"})
            self._repository.update_camera_status(
                camera_id=str(camera["camera_id"]),
                stream_status="offline",
                last_status_json=message,
                last_seen_at=None,
            )
            return
        except Exception as exc:
            message = json.dumps({"error": str(exc)})
            self._repository.update_camera_status(
                camera_id=str(camera["camera_id"]),
                stream_status="offline",
                last_status_json=message,
                last_seen_at=None,
            )
            return

        stream_status = str(payload.get("stream", {}).get("status", "unknown"))
        self._repository.update_camera_status(
            camera_id=str(camera["camera_id"]),
            stream_status=stream_status,
            last_status_json=json.dumps(payload, indent=2),
            last_seen_at=utc_now(),
        )


class RecorderSupervisor:
    def __init__(
        self,
        *,
        repository: NvrRepository,
        recordings_root: Path,
        ffmpeg_binary: str,
        rtsp_transport: str,
        segment_seconds: int,
        poll_interval_seconds: float,
        logger: logging.Logger | None = None,
    ) -> None:
        self._repository = repository
        self._recordings_root = recordings_root
        self._ffmpeg_binary = ffmpeg_binary
        self._rtsp_transport = rtsp_transport
        self._segment_seconds = segment_seconds
        self._poll_interval_seconds = poll_interval_seconds
        self._logger = logger or logging.getLogger(__name__)
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.RLock()
        self._processes: dict[str, subprocess.Popen[bytes]] = {}
        self._state: dict[str, dict[str, Any]] = {}

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return

        self._recordings_root.mkdir(parents=True, exist_ok=True)
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop,
            name="nvr-recorder",
            daemon=True,
        )
        self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        self._stop_event.set()
        with self._lock:
            camera_ids = list(self._processes.keys())
        for camera_id in camera_ids:
            self._stop_camera(camera_id, reason="shutdown")
        if self._thread:
            self._thread.join(timeout=timeout)

    def snapshot(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            snapshot = {}
            for camera_id, info in self._state.items():
                snapshot[camera_id] = dict(info)
            return snapshot

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            desired = {
                str(camera["camera_id"]): camera
                for camera in self._repository.list_recording_targets()
            }

            with self._lock:
                active_camera_ids = list(self._processes.keys())

            for camera_id in active_camera_ids:
                if camera_id not in desired:
                    self._stop_camera(camera_id, reason="recording disabled")

            for camera_id, camera in desired.items():
                process = self._current_process(camera_id)
                if process is None:
                    self._start_camera(camera)
                    continue

                if process.poll() is not None:
                    self._logger.warning("recorder exited for %s with code %s", camera_id, process.returncode)
                    self._repository.add_audit_event(
                        actor="system",
                        event_type="recording_restart",
                        target_type="camera",
                        target_id=camera_id,
                        message=f"FFmpeg recorder restarted after exit code {process.returncode}",
                    )
                    self._clear_process(camera_id)
                    self._start_camera(camera)

            if self._stop_event.wait(self._poll_interval_seconds):
                break

    def _start_camera(self, camera: dict[str, Any]) -> None:
        camera_id = str(camera["camera_id"])
        output_dir = self._recordings_root / camera_id
        output_dir.mkdir(parents=True, exist_ok=True)
        output_pattern = output_dir / "%Y%m%d_%H%M%S.mp4"
        command = [
            self._ffmpeg_binary,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "warning",
            "-rtsp_transport",
            self._rtsp_transport,
            "-i",
            str(camera["rtsp_url"]),
            "-map",
            "0:v:0",
            "-c",
            "copy",
            "-f",
            "segment",
            "-segment_time",
            str(self._segment_seconds),
            "-reset_timestamps",
            "1",
            "-strftime",
            "1",
            str(output_pattern),
        ]

        try:
            process = subprocess.Popen(command)
        except OSError as exc:
            self._logger.error("failed to start recorder for %s: %s", camera_id, exc)
            with self._lock:
                self._state[camera_id] = {
                    "status": "error",
                    "pid": None,
                    "last_error": str(exc),
                    "last_started_at": None,
                }
            self._repository.add_audit_event(
                actor="system",
                event_type="recording_error",
                target_type="camera",
                target_id=camera_id,
                message=f"FFmpeg recorder failed to start: {exc}",
            )
            return

        with self._lock:
            self._processes[camera_id] = process
            self._state[camera_id] = {
                "status": "recording",
                "pid": process.pid,
                "last_error": None,
                "last_started_at": utc_now(),
            }

        self._repository.add_audit_event(
            actor="system",
            event_type="recording_start",
            target_type="camera",
            target_id=camera_id,
            message=f"Continuous recording started to {output_dir}",
        )

    def _stop_camera(self, camera_id: str, *, reason: str) -> None:
        process = self._current_process(camera_id)
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)

        with self._lock:
            self._processes.pop(camera_id, None)
            self._state[camera_id] = {
                "status": "stopped",
                "pid": None,
                "last_error": None,
                "last_started_at": self._state.get(camera_id, {}).get("last_started_at"),
            }

        self._repository.add_audit_event(
            actor="system",
            event_type="recording_stop",
            target_type="camera",
            target_id=camera_id,
            message=f"Continuous recording stopped: {reason}",
        )

    def _current_process(self, camera_id: str) -> subprocess.Popen[bytes] | None:
        with self._lock:
            return self._processes.get(camera_id)

    def _clear_process(self, camera_id: str) -> None:
        with self._lock:
            self._processes.pop(camera_id, None)


def live_mjpeg_stream(*, camera: dict[str, Any], config: AppConfig):
    boundary = b"nvrframe"
    scale_filter = f"fps={max(config.live_preview_fps, 1)},scale={max(config.live_preview_width, 160)}:-2"
    command = [
        config.ffmpeg_binary,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-rtsp_transport",
        config.recording_rtsp_transport,
        "-i",
        str(camera["rtsp_url"]),
        "-an",
        "-vf",
        scale_filter,
        "-q:v",
        str(max(2, min(config.live_preview_jpeg_quality, 31))),
        "-f",
        "image2pipe",
        "-vcodec",
        "mjpeg",
        "pipe:1",
    ]

    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
        )
    except OSError as exc:
        logger.warning("failed to start live preview for %s: %s", camera.get("camera_id"), exc)
        return

    if process.stdout is None:
        process.terminate()
        return

    buffer = bytearray()
    try:
        while True:
            chunk = process.stdout.read(8192)
            if not chunk:
                break
            buffer.extend(chunk)

            while True:
                start = buffer.find(b"\xff\xd8")
                if start < 0:
                    if len(buffer) > 1024 * 1024:
                        buffer.clear()
                    break

                end = buffer.find(b"\xff\xd9", start + 2)
                if end < 0:
                    if start > 0:
                        del buffer[:start]
                    break

                frame = bytes(buffer[start : end + 2])
                del buffer[: end + 2]
                yield (
                    b"--"
                    + boundary
                    + b"\r\nContent-Type: image/jpeg\r\nContent-Length: "
                    + str(len(frame)).encode("ascii")
                    + b"\r\n\r\n"
                    + frame
                    + b"\r\n"
                )
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)


def list_recordings(recordings_root: Path, *, limit: int | None = None) -> list[dict[str, Any]]:
    if not recordings_root.exists():
        return []

    items: list[dict[str, Any]] = []
    for camera_dir in recordings_root.iterdir():
        if not camera_dir.is_dir():
            continue

        for file_path in camera_dir.glob("*.mp4"):
            stat = file_path.stat()
            items.append(
                {
                    "camera_id": camera_dir.name,
                    "file_name": file_path.name,
                    "path": str(file_path),
                    "size_bytes": stat.st_size,
                    "size_human": format_bytes(stat.st_size),
                    "created_at": datetime.fromtimestamp(stat.st_mtime, tz=UTC).isoformat(),
                    "started_at": parse_recording_start(file_path.name) or datetime.fromtimestamp(stat.st_mtime, tz=UTC).isoformat(),
                }
            )

    items.sort(key=lambda item: item["created_at"], reverse=True)
    if limit is not None:
        return items[:limit]
    return items


def recordings_summary(recordings_root: Path) -> dict[str, Any]:
    items = list_recordings(recordings_root)
    total_size = sum(int(item["size_bytes"]) for item in items)
    return {
        "count": len(items),
        "total_size_bytes": total_size,
        "total_size_human": format_bytes(total_size),
        "latest": items[:8],
    }


def parse_camera_status_payload(raw_payload: str | None) -> dict[str, Any] | None:
    if not raw_payload:
        return None
    try:
        payload = json.loads(raw_payload)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def format_uptime_label(raw_value: Any) -> str:
    if raw_value is None:
        return "unknown"

    try:
        total_seconds = max(0, int(float(raw_value)))
    except (TypeError, ValueError):
        return "unknown"

    hours, remainder = divmod(total_seconds, 3600)
    minutes = remainder // 60

    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m"
    return "<1m"


def summarize_camera_runtime(status_payload: dict[str, Any] | None) -> dict[str, Any]:
    stream = status_payload.get("stream", {}) if status_payload else {}
    controls = status_payload.get("controls", {}) if status_payload else {}
    source = status_payload.get("source", {}) if status_payload else {}
    control_channels = status_payload.get("control_channels", {}) if status_payload else {}
    primary_channel = control_channels.get("primary", {}) if isinstance(control_channels, dict) else {}
    beacon = primary_channel.get("beacon", {}) if primary_channel else {}
    poller = primary_channel.get("poller", {}) if primary_channel else {}
    if not beacon:
        beacon = status_payload.get("beacon", {}) if status_payload else {}
    if not poller:
        poller = status_payload.get("poller", {}) if status_payload else {}

    quality = controls.get("quality") or stream.get("applied_quality") or "unknown"
    overlay_enabled = controls.get("overlay_enabled")
    overlay_label = "enabled" if overlay_enabled else "disabled"
    if overlay_enabled is None:
        overlay_label = "unknown"

    return {
        "lab_mode": status_payload.get("lab_mode", "unknown") if status_payload else "unknown",
        "stream_status": stream.get("status", "unknown"),
        "quality": str(quality),
        "overlay_enabled": overlay_enabled,
        "overlay_label": overlay_label,
        "source_kind": source.get("kind", "unknown"),
        "source_uri": source.get("uri", "-"),
        "uptime_seconds": status_payload.get("uptime_seconds"),
        "uptime_label": format_uptime_label(status_payload.get("uptime_seconds") if status_payload else None),
        "primary_control_url": primary_channel.get("base_url") or beacon.get("target_url") or poller.get("target_url"),
        "beacon_enabled": beacon.get("enabled"),
        "beacon_status": beacon.get("status", "unknown"),
        "poller_enabled": poller.get("enabled"),
        "poller_status": poller.get("status", "unknown"),
        "last_error": stream.get("last_error"),
        "applied_quality": stream.get("applied_quality"),
        "applied_overlay_enabled": stream.get("applied_overlay_enabled"),
        "config_revision": stream.get("config_revision"),
        "markers_count": len(status_payload.get("markers", [])) if status_payload else 0,
    }


def enrich_camera_record(camera: dict[str, Any]) -> dict[str, Any]:
    enriched = dict(camera)
    status_payload = parse_camera_status_payload(enriched.get("last_status_json"))
    enriched["status_payload"] = status_payload
    enriched["runtime"] = summarize_camera_runtime(status_payload)
    return enriched


def user_from_request(request: Request, session_store: SessionStore, cookie_name: str) -> str | None:
    token = request.cookies.get(cookie_name)
    return session_store.get_username(token)


def redirect_to_login() -> RedirectResponse:
    return RedirectResponse("/login", status_code=303)


def template_context(
    *,
    request: Request,
    current_user: str,
    page_title: str,
    active_nav: str,
    **extra: Any,
) -> dict[str, Any]:
    context = {
        "request": request,
        "current_user": current_user,
        "page_title": page_title,
        "active_nav": active_nav,
    }
    context.update(extra)
    return context


def safe_control_overview(
    control_client: ControlServerClient,
    camera_ids: list[str],
    *,
    heartbeat_ttl_seconds: float,
) -> dict[str, Any]:
    try:
        return control_client.get_overview(camera_ids, heartbeat_ttl_seconds=heartbeat_ttl_seconds)
    except Exception as exc:
        camera_health = summarize_camera_health(
            [],
            camera_ids=camera_ids,
            heartbeat_ttl_seconds=heartbeat_ttl_seconds,
        )
        return {
            "healthy": False,
            "error": str(exc),
            "recent_beacons": [],
            "recent_results": [],
            "pending_tasks_count": 0,
            "recent_beacons_count": 0,
            "recent_results_count": 0,
            "active_camera_count": 0,
            "camera_health": camera_health["cameras"],
            "channel_status": "offline",
            "channel_status_label": "Offline",
            "channel_status_hint": "control server unavailable",
            "heartbeat_ttl_seconds": heartbeat_ttl_seconds,
        }


def safe_camera_control_state(control_client: ControlServerClient, camera_id: str) -> dict[str, Any]:
    try:
        return control_client.get_camera_control_state(camera_id)
    except Exception as exc:
        return {
            "healthy": False,
            "error": str(exc),
            "recent_beacons": [],
            "pending_tasks": [],
            "recent_results": [],
            "last_beacon": None,
            "last_result": None,
        }


def build_task_params(command: str, form_data: dict[str, Any]) -> dict[str, Any]:
    if command == "set_quality":
        quality = str(form_data.get("quality", "")).strip().lower()
        if quality not in {"low", "medium", "high"}:
            raise ValueError("quality must be low, medium, or high")
        return {"quality": quality}

    if command == "toggle_overlay":
        enabled_raw = str(form_data.get("overlay_enabled", "")).strip().lower()
        if enabled_raw not in {"true", "false"}:
            raise ValueError("overlay selection is required")
        return {"enabled": enabled_raw == "true"}

    if command == "record_marker":
        note = str(form_data.get("marker_note", "")).strip()
        if not note:
            raise ValueError("marker note is required")
        return {"note": note}

    if command in {"get_status", "noop"}:
        return {}

    raise ValueError(f"unsupported command: {command}")


def create_app() -> FastAPI:
    config = AppConfig.from_env()
    scenario_logger = ScenarioLogger.from_env(service_name="nvr-console")
    repository = NvrRepository(config.database_path, scenario_logger=scenario_logger)
    repository.initialize()
    repository.bootstrap(config)
    session_store = SessionStore(config.session_ttl_seconds)
    control_client = ControlServerClient(config.control_server_url)
    poller = CameraStatusPoller(
        repository=repository,
        interval_seconds=config.status_poll_interval_seconds,
        logger=logging.getLogger("nvr-console.poller"),
    )
    recorder = RecorderSupervisor(
        repository=repository,
        recordings_root=config.recordings_root,
        ffmpeg_binary=config.ffmpeg_binary,
        rtsp_transport=config.recording_rtsp_transport,
        segment_seconds=config.recording_segment_seconds,
        poll_interval_seconds=config.recorder_poll_interval_seconds,
        logger=logging.getLogger("nvr-console.recorder"),
    )

    templates = Jinja2Templates(directory=str(Path(__file__).with_name("templates")))
    templates.env.filters["filesize"] = format_bytes
    templates.env.filters["prettyjson"] = lambda value: json.dumps(value, indent=2, ensure_ascii=False)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        logger.info("starting nvr-console")
        scenario_logger.event(
            event_type="nvr.lifecycle.start",
            phase="startup",
            source="nvr-console",
            target=config.default_camera_id,
            result="starting",
            details={
                "database_path": str(config.database_path),
                "recordings_root": str(config.recordings_root),
                "control_server_url": config.control_server_url,
            },
        )
        poller.start()
        recorder.start()
        try:
            yield
        finally:
            logger.info("stopping nvr-console")
            scenario_logger.event(
                event_type="nvr.lifecycle.stop",
                phase="shutdown",
                source="nvr-console",
                target=config.default_camera_id,
                result="stopping",
            )
            recorder.stop()
            poller.stop()

    app = FastAPI(
        title="nvr-console",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.mount("/static", StaticFiles(directory=str(Path(__file__).with_name("static"))), name="static")
    app.state.config = config
    app.state.repository = repository
    app.state.session_store = session_store
    app.state.recorder = recorder
    app.state.templates = templates
    app.state.control_client = control_client

    @app.get("/health")
    def health() -> dict[str, Any]:
        summary = recordings_summary(config.recordings_root)
        control_status = safe_control_overview(
            control_client,
            [],
            heartbeat_ttl_seconds=config.camera_heartbeat_ttl_seconds,
        )
        return {
            "status": "ok",
            "cameras": len(repository.list_cameras()),
            "recordings": summary["count"],
            "control_server_healthy": control_status["healthy"],
        }

    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request) -> Response:
        current_user = user_from_request(request, session_store, config.session_cookie_name)
        if current_user:
            return RedirectResponse("/", status_code=303)

        return templates.TemplateResponse(
            "login.html",
            {
                "request": request,
                "page_title": "NVR Sign In",
                "error": request.query_params.get("error"),
            },
        )

    @app.post("/login")
    async def login(request: Request) -> Response:
        form = await request.form()
        username = str(form.get("username", "")).strip()
        password = str(form.get("password", ""))

        if not repository.verify_user(username, password, config.password_salt):
            repository.add_audit_event(
                actor=username or "anonymous",
                event_type="login_failed",
                target_type="session",
                target_id="-",
                message="Failed login attempt",
            )
            return templates.TemplateResponse(
                "login.html",
                {
                    "request": request,
                    "page_title": "NVR Sign In",
                    "error": "Invalid username or password.",
                },
                status_code=401,
            )

        token = session_store.create(username)
        repository.add_audit_event(
            actor=username,
            event_type="login_success",
            target_type="session",
            target_id="-",
            message="Administrator signed in",
        )
        response = RedirectResponse("/", status_code=303)
        response.set_cookie(
            key=config.session_cookie_name,
            value=token,
            httponly=True,
            samesite="lax",
            max_age=config.session_ttl_seconds,
        )
        return response

    @app.post("/logout")
    def logout(request: Request) -> RedirectResponse:
        current_user = user_from_request(request, session_store, config.session_cookie_name) or "anonymous"
        session_store.delete(request.cookies.get(config.session_cookie_name))
        repository.add_audit_event(
            actor=current_user,
            event_type="logout",
            target_type="session",
            target_id="-",
            message="Administrator signed out",
        )
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(config.session_cookie_name)
        return response

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request) -> Response:
        current_user = user_from_request(request, session_store, config.session_cookie_name)
        if not current_user:
            return redirect_to_login()

        cameras = [enrich_camera_record(camera) for camera in repository.list_cameras()]
        recordings = recordings_summary(config.recordings_root)
        audit_events = repository.list_audit_events(limit=8)
        recorder_state = recorder.snapshot()
        control_overview = safe_control_overview(
            control_client,
            [str(camera["camera_id"]) for camera in cameras],
            heartbeat_ttl_seconds=config.camera_heartbeat_ttl_seconds,
        )
        control_overview = attach_camera_health_details(control_overview, cameras)
        online_count = sum(1 for camera in cameras if camera["stream_status"] in {"starting", "publishing"})
        stats = [
            {"label": "연동된 카메라", "value": len(cameras), "hint": "등록된 IP 장비"},
            {"label": "정상 송출 채널", "value": online_count, "hint": "🟢송출중"},
            {"label": "녹화 세그먼트", "value": recordings["count"], "hint": recordings["total_size_human"]},
            {
                "label": "기기 상태",
                "value": control_overview["channel_status_label"],
                "hint": control_overview["channel_status_hint"],
            },
        ]

        return templates.TemplateResponse(
            "dashboard.html",
            template_context(
                request=request,
                current_user=current_user,
                page_title="NVR Overview",
                active_nav="dashboard",
                stats=stats,
                cameras=cameras,
                recordings=recordings["latest"],
                audit_events=audit_events,
                recorder_state=recorder_state,
                control_overview=control_overview,
            ),
        )

    @app.get("/cameras", response_class=HTMLResponse)
    def cameras_page(request: Request) -> Response:
        current_user = user_from_request(request, session_store, config.session_cookie_name)
        if not current_user:
            return redirect_to_login()

        return templates.TemplateResponse(
            "cameras.html",
            template_context(
                request=request,
                current_user=current_user,
                page_title="Camera Inventory",
                active_nav="cameras",
                cameras=[enrich_camera_record(camera) for camera in repository.list_cameras()],
                recorder_state=recorder.snapshot(),
            ),
        )

    @app.get("/devices/status", response_class=HTMLResponse)
    def device_status_page(request: Request) -> Response:
        current_user = user_from_request(request, session_store, config.session_cookie_name)
        if not current_user:
            return redirect_to_login()

        cameras = [enrich_camera_record(camera) for camera in repository.list_cameras()]
        control_overview = safe_control_overview(
            control_client,
            [str(camera["camera_id"]) for camera in cameras],
            heartbeat_ttl_seconds=config.camera_heartbeat_ttl_seconds,
        )
        control_overview = attach_camera_health_details(control_overview, cameras)
        return templates.TemplateResponse(
            "device_status.html",
            template_context(
                request=request,
                current_user=current_user,
                page_title="Device Status",
                active_nav="devices",
                control_overview=control_overview,
                cameras=cameras,
            ),
        )

    @app.get("/cameras/{camera_id}", response_class=HTMLResponse)
    def camera_detail(request: Request, camera_id: str) -> Response:
        current_user = user_from_request(request, session_store, config.session_cookie_name)
        if not current_user:
            return redirect_to_login()

        camera = repository.get_camera(camera_id)
        if camera is None:
            return RedirectResponse("/cameras", status_code=303)

        camera = enrich_camera_record(camera)
        status_payload = camera.get("status_payload")
        if status_payload is None and camera.get("last_status_json"):
            status_payload = {"raw": camera["last_status_json"]}
        control_state = safe_camera_control_state(control_client, camera_id)

        return templates.TemplateResponse(
            "camera_detail.html",
            template_context(
                request=request,
                current_user=current_user,
                page_title=str(camera["display_name"]),
                active_nav="cameras",
                camera=camera,
                recorder_state=recorder.snapshot().get(camera_id, {}),
                status_payload=status_payload,
                control_state=control_state,
                control_feedback=request.query_params.get("control_feedback"),
            ),
        )

    @app.get("/cameras/{camera_id}/live.mjpeg")
    def camera_live_mjpeg(request: Request, camera_id: str) -> Response:
        current_user = user_from_request(request, session_store, config.session_cookie_name)
        if not current_user:
            return redirect_to_login()

        camera = repository.get_camera(camera_id)
        if camera is None:
            return RedirectResponse("/cameras", status_code=303)

        return StreamingResponse(
            live_mjpeg_stream(camera=camera, config=config),
            media_type="multipart/x-mixed-replace; boundary=nvrframe",
            headers={
                "Cache-Control": "no-store, no-cache, must-revalidate",
                "Pragma": "no-cache",
            },
        )

    @app.post("/cameras/{camera_id}/settings")
    async def camera_settings(request: Request, camera_id: str) -> RedirectResponse:
        current_user = user_from_request(request, session_store, config.session_cookie_name)
        if not current_user:
            return redirect_to_login()

        camera = repository.get_camera(camera_id)
        if camera is None:
            return RedirectResponse("/cameras", status_code=303)

        form = await request.form()
        display_name = str(form.get("display_name", camera["display_name"])).strip() or str(camera["display_name"])
        location = str(form.get("location", camera["location"])).strip() or str(camera["location"])
        recording_mode = str(form.get("recording_mode", camera["recording_mode"])).strip().lower()
        if recording_mode not in {"continuous", "disabled"}:
            recording_mode = str(camera["recording_mode"])

        try:
            retention_days = int(str(form.get("retention_days", camera["retention_days"])).strip())
        except ValueError:
            retention_days = int(camera["retention_days"])
        retention_days = max(1, min(retention_days, 90))

        repository.update_camera_settings(
            camera_id=camera_id,
            display_name=display_name,
            location=location,
            recording_mode=recording_mode,
            retention_days=retention_days,
        )
        repository.add_audit_event(
            actor=current_user,
            event_type="camera_update",
            target_type="camera",
            target_id=camera_id,
            message=f"Updated camera settings: mode={recording_mode}, retention={retention_days}d",
        )
        return RedirectResponse(f"/cameras/{camera_id}", status_code=303)

    @app.post("/cameras/{camera_id}/control/task")
    async def camera_control_task(request: Request, camera_id: str) -> RedirectResponse:
        current_user = user_from_request(request, session_store, config.session_cookie_name)
        if not current_user:
            return redirect_to_login()

        camera = repository.get_camera(camera_id)
        if camera is None:
            return RedirectResponse("/cameras", status_code=303)

        form = await request.form()
        command = str(form.get("command", "")).strip().lower()

        try:
            params = build_task_params(command, dict(form))
            queued = control_client.create_task(
                camera_id=camera_id,
                command=command,
                params=params,
            )
        except Exception as exc:
            repository.add_audit_event(
                actor=current_user,
                event_type="control_task_error",
                target_type="camera",
                target_id=camera_id,
                message=f"Failed to queue control task {command}: {exc}",
            )
            error_message = urlparse.quote(str(exc))
            return RedirectResponse(
                f"/cameras/{camera_id}?control_feedback={error_message}",
                status_code=303,
            )

        repository.add_audit_event(
            actor=current_user,
            event_type="control_task_queued",
            target_type="camera",
            target_id=camera_id,
            message=f"Queued control task {command} via normal control-server",
        )
        queued_id = str(queued.get("task", {}).get("id", "queued"))
        return RedirectResponse(
            f"/cameras/{camera_id}?control_feedback={urlparse.quote(queued_id)}",
            status_code=303,
        )

    @app.get("/control", response_class=HTMLResponse)
    def control_page(request: Request) -> Response:
        current_user = user_from_request(request, session_store, config.session_cookie_name)
        if not current_user:
            return redirect_to_login()

        cameras = repository.list_cameras()
        control_overview = safe_control_overview(
            control_client,
            [str(camera["camera_id"]) for camera in cameras],
            heartbeat_ttl_seconds=config.camera_heartbeat_ttl_seconds,
        )
        control_overview = attach_camera_health_details(control_overview, cameras)
        return templates.TemplateResponse(
            "control.html",
            template_context(
                request=request,
                current_user=current_user,
                page_title="Normal Control Plane",
                active_nav="control",
                control_overview=control_overview,
                cameras=cameras,
            ),
        )

    @app.get("/recordings", response_class=HTMLResponse)
    def recordings_page(request: Request) -> Response:
        current_user = user_from_request(request, session_store, config.session_cookie_name)
        if not current_user:
            return redirect_to_login()

        items = list_recordings(config.recordings_root)
        return templates.TemplateResponse(
            "recordings.html",
            template_context(
                request=request,
                current_user=current_user,
                page_title="Recording Archive",
                active_nav="recordings",
                recordings=items,
                summary=recordings_summary(config.recordings_root),
            ),
        )

    @app.get("/recordings/files/{camera_id}/{file_name}")
    def download_recording(request: Request, camera_id: str, file_name: str) -> Response:
        current_user = user_from_request(request, session_store, config.session_cookie_name)
        if not current_user:
            return redirect_to_login()

        file_path = (config.recordings_root / camera_id / file_name).resolve()
        allowed_root = config.recordings_root.resolve()
        if allowed_root not in file_path.parents or not file_path.exists():
            return RedirectResponse("/recordings", status_code=303)
        return FileResponse(file_path)

    @app.get("/audit", response_class=HTMLResponse)
    def audit_page(request: Request) -> Response:
        current_user = user_from_request(request, session_store, config.session_cookie_name)
        if not current_user:
            return redirect_to_login()

        return templates.TemplateResponse(
            "audit.html",
            template_context(
                request=request,
                current_user=current_user,
                page_title="Audit Log",
                active_nav="audit",
                audit_events=repository.list_audit_events(limit=100),
            ),
        )

    @app.get("/api/cameras")
    def cameras_api(request: Request) -> Response:
        current_user = user_from_request(request, session_store, config.session_cookie_name)
        if not current_user:
            return redirect_to_login()
        return JSONResponse({"items": repository.list_cameras()})

    @app.get("/api/recordings")
    def recordings_api(request: Request) -> Response:
        current_user = user_from_request(request, session_store, config.session_cookie_name)
        if not current_user:
            return redirect_to_login()
        items = list_recordings(config.recordings_root)
        return JSONResponse({"count": len(items), "items": items})

    @app.get("/api/audit")
    def audit_api(request: Request) -> Response:
        current_user = user_from_request(request, session_store, config.session_cookie_name)
        if not current_user:
            return redirect_to_login()
        return JSONResponse({"items": repository.list_audit_events(limit=100)})

    @app.get("/api/control/overview")
    def control_overview_api(request: Request) -> Response:
        current_user = user_from_request(request, session_store, config.session_cookie_name)
        if not current_user:
            return redirect_to_login()
        cameras = repository.list_cameras()
        payload = safe_control_overview(
            control_client,
            [str(camera["camera_id"]) for camera in cameras],
            heartbeat_ttl_seconds=config.camera_heartbeat_ttl_seconds,
        )
        payload = attach_camera_health_details(payload, cameras)
        return JSONResponse(payload)

    return app


app = create_app()


if __name__ == "__main__":
    config = AppConfig.from_env()
    uvicorn.run(
        app,
        host=config.app_host,
        port=config.app_port,
        log_level=os.getenv("UVICORN_LOG_LEVEL", "info"),
    )
