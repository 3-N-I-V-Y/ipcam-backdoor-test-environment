from __future__ import annotations

from collections import defaultdict, deque
from datetime import UTC, datetime
import itertools
from pathlib import Path
import sys
import threading
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Query, Request
from pydantic import BaseModel, Field

SERVICES_ROOT = Path(__file__).resolve().parents[1]
if (SERVICES_ROOT / "common").exists():
    sys.path.insert(0, str(SERVICES_ROOT))

from common.scenario_logger import ScenarioLogger


SAFE_SERVER_COMMANDS = {
    "noop",
    "get_status",
    "status",
    "record_marker",
    "marker",
    "set_quality",
    "toggle_overlay",
}


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def normalize_command(command: str) -> str:
    normalized = command.strip().lower()
    alias_map = {
        "status": "get_status",
        "marker": "record_marker",
    }
    return alias_map.get(normalized, normalized)


class TaskCreateRequest(BaseModel):
    camera_id: str
    command: Literal[
        "noop",
        "get_status",
        "status",
        "record_marker",
        "marker",
        "set_quality",
        "toggle_overlay",
    ]
    params: dict[str, Any] = Field(default_factory=dict)
    scenario_id: str | None = None
    run_id: str | None = None
    technique_id: str | None = None
    phase: str | None = None
    label: str | None = None


class ControlStore:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._task_counter = itertools.count(1)
        self._pending_tasks: dict[str, deque[dict[str, Any]]] = defaultdict(deque)
        self._recent_results: deque[dict[str, Any]] = deque(maxlen=100)
        self._recent_beacons: deque[dict[str, Any]] = deque(maxlen=100)

    def enqueue_task(
        self,
        *,
        camera_id: str,
        command: str,
        params: dict[str, Any],
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        normalized_command = normalize_command(command)
        if normalized_command not in SAFE_SERVER_COMMANDS:
            raise ValueError(f"unsupported safe command: {command}")

        metadata = compact_metadata(metadata or {})
        task = {
            "id": f"task-{next(self._task_counter):04d}",
            "camera_id": camera_id,
            "command": normalized_command,
            "params": params,
            "created_at": utc_now(),
            **metadata,
        }

        with self._lock:
            self._pending_tasks[camera_id].append(task)
            return dict(task)

    def next_task(self, *, camera_id: str) -> dict[str, Any] | None:
        with self._lock:
            queue = self._pending_tasks.get(camera_id)
            if not queue:
                return None
            return dict(queue.popleft())

    def list_tasks(self, *, camera_id: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            if camera_id is not None:
                return [dict(task) for task in self._pending_tasks.get(camera_id, ())]

            tasks: list[dict[str, Any]] = []
            for queued in self._pending_tasks.values():
                tasks.extend(dict(task) for task in queued)
            return tasks

    def save_result(self, payload: dict[str, Any]) -> dict[str, Any]:
        record = {
            **payload,
            "received_at": utc_now(),
        }
        with self._lock:
            self._recent_results.append(record)
        return record

    def list_results(self, *, camera_id: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            records = list(self._recent_results)
        if camera_id is None:
            return records
        return [record for record in records if record.get("camera_id") == camera_id]

    def save_beacon(self, payload: dict[str, Any]) -> dict[str, Any]:
        record = {
            **payload,
            "received_at": utc_now(),
        }
        with self._lock:
            self._recent_beacons.append(record)
        return record

    def list_beacons(self, *, camera_id: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            records = list(self._recent_beacons)
        if camera_id is None:
            return records
        return [record for record in records if record.get("camera_id") == camera_id]


app = FastAPI()
store = ControlStore()
scenario_logger = ScenarioLogger.from_env(service_name="control-server")


def compact_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    allowed_fields = {"scenario_id", "run_id", "technique_id", "phase", "label"}
    return {
        key: value
        for key, value in metadata.items()
        if key in allowed_fields and value is not None and str(value).strip()
    }


def task_metadata_from_request(body: TaskCreateRequest) -> dict[str, Any]:
    return compact_metadata(
        {
            "scenario_id": body.scenario_id,
            "run_id": body.run_id,
            "technique_id": body.technique_id,
            "phase": body.phase,
            "label": body.label,
        }
    )


def event_metadata(payload: dict[str, Any]) -> dict[str, Any]:
    return compact_metadata(
        {
            "scenario_id": payload.get("scenario_id"),
            "run_id": payload.get("run_id"),
            "technique_id": payload.get("technique_id"),
            "phase": payload.get("phase"),
            "label": payload.get("label"),
        }
    )


def log_ground_truth_if_labeled(
    *,
    event_type: str,
    metadata: dict[str, Any],
    camera_id: str | None,
    source: str | None,
    target: str | None,
    result: str | None,
    details: dict[str, Any],
) -> None:
    label = metadata.get("label")
    phase = metadata.get("phase")
    if not label or not phase:
        return

    scenario_logger.ground_truth(
        event_type=event_type,
        phase=str(phase),
        label=str(label),
        scenario_id=metadata.get("scenario_id"),
        run_id=metadata.get("run_id"),
        technique_id=metadata.get("technique_id"),
        camera_id=camera_id,
        source=source,
        target=target,
        result=result,
        details=details,
    )


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/beacon")
async def beacon(request: Request) -> dict[str, Any]:
    payload = await request.json()
    saved = store.save_beacon(payload)
    metadata = event_metadata(payload)
    scenario_logger.event(
        event_type="control.beacon.received",
        phase=metadata.get("phase") or "control_beacon",
        label=metadata.get("label"),
        scenario_id=metadata.get("scenario_id"),
        run_id=metadata.get("run_id"),
        technique_id=metadata.get("technique_id"),
        camera_id=str(payload.get("camera_id") or ""),
        source=request.client.host if request.client else None,
        target="control-server",
        result="received",
        details={
            "control_channel": payload.get("control_channel"),
            "lab_mode": payload.get("lab_mode"),
            "stream_state": payload.get("stream_state"),
            "source_kind": payload.get("source_kind"),
        },
    )
    return {"received": True, "payload": saved}


@app.get("/beacons")
def beacons(camera_id: str | None = Query(default=None)) -> dict[str, Any]:
    return {
        "count": len(store.list_beacons(camera_id=camera_id)),
        "items": store.list_beacons(camera_id=camera_id),
    }


@app.post("/tasks")
def create_task(body: TaskCreateRequest) -> dict[str, Any]:
    metadata = task_metadata_from_request(body)
    try:
        task = store.enqueue_task(
            camera_id=body.camera_id,
            command=body.command,
            params=body.params,
            metadata=metadata,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    scenario_logger.event(
        event_type="control.task.queued",
        phase=metadata.get("phase") or "task_queue",
        label=metadata.get("label"),
        scenario_id=metadata.get("scenario_id"),
        run_id=metadata.get("run_id"),
        technique_id=metadata.get("technique_id"),
        camera_id=body.camera_id,
        source="control-server",
        target=body.camera_id,
        result="queued",
        details={
            "task_id": task["id"],
            "command": task["command"],
            "params": body.params,
        },
    )
    log_ground_truth_if_labeled(
        event_type="control.task.queued",
        metadata=metadata,
        camera_id=body.camera_id,
        source="control-server",
        target=body.camera_id,
        result="queued",
        details={"task_id": task["id"], "command": task["command"]},
    )

    return {
        "queued": True,
        "task": task,
    }


@app.get("/tasks")
def tasks(camera_id: str | None = Query(default=None)) -> dict[str, Any]:
    items = store.list_tasks(camera_id=camera_id)
    return {
        "count": len(items),
        "items": items,
    }


@app.get("/task")
def task(camera_id: str = Query(...)) -> dict[str, Any]:
    next_task = store.next_task(camera_id=camera_id)
    return {
        "camera_id": camera_id,
        "task": next_task,
    }


@app.post("/result")
async def result(request: Request) -> dict[str, Any]:
    payload = await request.json()
    saved = store.save_result(payload)
    metadata = event_metadata(payload)
    scenario_logger.event(
        event_type="control.result.received",
        phase=metadata.get("phase") or "task_result",
        label=metadata.get("label"),
        scenario_id=metadata.get("scenario_id"),
        run_id=metadata.get("run_id"),
        technique_id=metadata.get("technique_id"),
        camera_id=str(payload.get("camera_id") or ""),
        source=str(payload.get("camera_id") or ""),
        target="control-server",
        result="success" if payload.get("success") else "failed",
        details={
            "task_id": payload.get("task_id"),
            "command": payload.get("command"),
            "control_channel": payload.get("control_channel"),
            "error": payload.get("error"),
        },
    )
    log_ground_truth_if_labeled(
        event_type="control.result.received",
        metadata=metadata,
        camera_id=str(payload.get("camera_id") or ""),
        source=str(payload.get("camera_id") or ""),
        target="control-server",
        result="success" if payload.get("success") else "failed",
        details={
            "task_id": payload.get("task_id"),
            "command": payload.get("command"),
            "control_channel": payload.get("control_channel"),
        },
    )
    return {"saved": True, "payload": saved}


@app.get("/results")
def results(camera_id: str | None = Query(default=None)) -> dict[str, Any]:
    items = store.list_results(camera_id=camera_id)
    return {
        "count": len(items),
        "items": items,
    }
