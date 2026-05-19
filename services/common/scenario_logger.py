from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import json
import logging
import os
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "scenario-log/v1"
logger = logging.getLogger(__name__)


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def parse_bool(raw_value: str | None, *, default: bool) -> bool:
    if raw_value is None or not raw_value.strip():
        return default

    normalized = raw_value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError("boolean value must be one of: true, false, yes, no, on, off, 1, 0")


def compact_record(record: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in record.items() if value is not None}


@dataclass(frozen=True, slots=True)
class ScenarioLogConfig:
    enabled: bool
    service_name: str
    scenario_id: str | None
    run_id: str | None
    events_path: Path
    ground_truth_path: Path

    @classmethod
    def from_env(cls, *, service_name: str) -> "ScenarioLogConfig":
        root = Path(os.getenv("SCENARIO_LOG_ROOT", "/data/scenarios"))
        events_path = Path(os.getenv("SCENARIO_EVENTS_PATH", str(root / "events.jsonl")))
        ground_truth_path = Path(
            os.getenv("SCENARIO_GROUND_TRUTH_PATH", str(root / "ground-truth.jsonl"))
        )
        scenario_id = os.getenv("SCENARIO_ID", "baseline").strip() or None
        run_id = os.getenv("SCENARIO_RUN_ID", "manual-run").strip() or None

        return cls(
            enabled=parse_bool(os.getenv("SCENARIO_LOG_ENABLED"), default=True),
            service_name=service_name,
            scenario_id=scenario_id,
            run_id=run_id,
            events_path=events_path,
            ground_truth_path=ground_truth_path,
        )


class ScenarioLogger:
    def __init__(self, config: ScenarioLogConfig) -> None:
        self._config = config

    @classmethod
    def from_env(cls, *, service_name: str) -> "ScenarioLogger":
        return cls(ScenarioLogConfig.from_env(service_name=service_name))

    @property
    def enabled(self) -> bool:
        return self._config.enabled

    def event(
        self,
        *,
        event_type: str,
        phase: str | None = None,
        label: str | None = None,
        scenario_id: str | None = None,
        run_id: str | None = None,
        technique_id: str | None = None,
        camera_id: str | None = None,
        source: str | None = None,
        target: str | None = None,
        port: int | None = None,
        proto: str | None = None,
        result: str | None = None,
        details: dict[str, Any] | None = None,
        timestamp: str | None = None,
    ) -> None:
        record = self._base_record(
            event_type=event_type,
            phase=phase,
            label=label,
            scenario_id=scenario_id,
            run_id=run_id,
            technique_id=technique_id,
            camera_id=camera_id,
            source=source,
            target=target,
            port=port,
            proto=proto,
            result=result,
            details=details,
            timestamp=timestamp,
        )
        self._append_jsonl(self._config.events_path, record)

    def ground_truth(
        self,
        *,
        event_type: str,
        phase: str,
        label: str,
        scenario_id: str | None = None,
        run_id: str | None = None,
        technique_id: str | None = None,
        camera_id: str | None = None,
        source: str | None = None,
        target: str | None = None,
        port: int | None = None,
        proto: str | None = None,
        result: str | None = None,
        details: dict[str, Any] | None = None,
        timestamp: str | None = None,
        window_start: str | None = None,
        window_end: str | None = None,
    ) -> None:
        record = self._base_record(
            event_type=event_type,
            phase=phase,
            label=label,
            scenario_id=scenario_id,
            run_id=run_id,
            technique_id=technique_id,
            camera_id=camera_id,
            source=source,
            target=target,
            port=port,
            proto=proto,
            result=result,
            details=details,
            timestamp=timestamp,
        )
        record.update(
            compact_record(
                {
                    "window_start": window_start,
                    "window_end": window_end,
                }
            )
        )
        self._append_jsonl(self._config.ground_truth_path, record)

    def _base_record(
        self,
        *,
        event_type: str,
        phase: str | None,
        label: str | None,
        scenario_id: str | None,
        run_id: str | None,
        technique_id: str | None,
        camera_id: str | None,
        source: str | None,
        target: str | None,
        port: int | None,
        proto: str | None,
        result: str | None,
        details: dict[str, Any] | None,
        timestamp: str | None,
    ) -> dict[str, Any]:
        return compact_record(
            {
                "schema_version": SCHEMA_VERSION,
                "timestamp": timestamp or utc_now(),
                "service": self._config.service_name,
                "event_type": event_type,
                "scenario_id": scenario_id or self._config.scenario_id,
                "run_id": run_id or self._config.run_id,
                "technique_id": technique_id,
                "phase": phase,
                "label": label,
                "camera_id": camera_id,
                "source": source,
                "target": target,
                "port": port,
                "proto": proto,
                "result": result,
                "details": details or {},
            }
        )

    def _append_jsonl(self, path: Path, record: dict[str, Any]) -> None:
        if not self._config.enabled:
            return

        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            line = json.dumps(record, ensure_ascii=False, sort_keys=True, default=str)
            with path.open("a", encoding="utf-8") as file:
                file.write(f"{line}\n")
        except OSError as exc:
            logger.warning("failed to write scenario log %s: %s", path, exc)
