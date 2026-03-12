from __future__ import annotations

from collections import defaultdict, deque
from datetime import UTC, datetime
import itertools
import threading
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Query, Request
from pydantic import BaseModel, Field


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


class ControlStore:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._task_counter = itertools.count(1)
        self._pending_tasks: dict[str, deque[dict[str, Any]]] = defaultdict(deque)
        self._recent_results: deque[dict[str, Any]] = deque(maxlen=100)
        self._recent_beacons: deque[dict[str, Any]] = deque(maxlen=100)

    def enqueue_task(self, *, camera_id: str, command: str, params: dict[str, Any]) -> dict[str, Any]:
        normalized_command = normalize_command(command)
        if normalized_command not in SAFE_SERVER_COMMANDS:
            raise ValueError(f"unsupported safe command: {command}")

        task = {
            "id": f"task-{next(self._task_counter):04d}",
            "camera_id": camera_id,
            "command": normalized_command,
            "params": params,
            "created_at": utc_now(),
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


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/beacon")
async def beacon(request: Request) -> dict[str, Any]:
    payload = await request.json()
    saved = store.save_beacon(payload)
    return {"received": True, "payload": saved}


@app.get("/beacons")
def beacons(camera_id: str | None = Query(default=None)) -> dict[str, Any]:
    return {
        "count": len(store.list_beacons(camera_id=camera_id)),
        "items": store.list_beacons(camera_id=camera_id),
    }


@app.post("/tasks")
def create_task(body: TaskCreateRequest) -> dict[str, Any]:
    try:
        task = store.enqueue_task(
            camera_id=body.camera_id,
            command=body.command,
            params=body.params,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

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
    return {"saved": True, "payload": saved}


@app.get("/results")
def results(camera_id: str | None = Query(default=None)) -> dict[str, Any]:
    items = store.list_results(camera_id=camera_id)
    return {
        "count": len(items),
        "items": items,
    }
