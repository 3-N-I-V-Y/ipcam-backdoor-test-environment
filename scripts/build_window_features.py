from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import statistics
from typing import Any


DEFAULT_TSV_FIELDS = [
    "ts",
    "id.orig_h",
    "id.orig_p",
    "id.resp_h",
    "id.resp_p",
    "proto",
    "duration",
    "orig_bytes",
    "resp_bytes",
    "missed_bytes",
    "orig_pkts",
    "orig_ip_bytes",
    "resp_pkts",
    "resp_ip_bytes",
]


OUTPUT_FIELDS = [
    "window_start",
    "window_end",
    "scenario_id",
    "run_id",
    "label",
    "phases",
    "technique_ids",
    "flow_count",
    "unique_dst_count",
    "unique_dst_port_count",
    "dst_port_entropy",
    "proto_entropy",
    "short_flow_ratio",
    "small_response_ratio",
    "zero_dst_bytes_ratio",
    "avg_duration",
    "median_duration",
    "avg_src_bytes",
    "avg_dst_bytes",
    "avg_total_bytes",
    "bytes_out_in_ratio",
    "pkts_out_in_ratio",
    "inter_flow_time_mean",
    "inter_flow_time_std",
    "tcp_flow_ratio",
    "udp_flow_ratio",
    "dst_port_well_known_ratio",
    "dst_port_registered_ratio",
    "dst_port_ephemeral_ratio",
]


@dataclass(slots=True)
class Flow:
    ts: float
    src_port: int
    dst_port: int
    duration: float
    src_bytes: int
    dst_bytes: int
    missed_bytes: int
    src_pkts: int
    src_ip_bytes: int
    dst_pkts: int
    dst_ip_bytes: int
    proto: str
    src_ip: str
    dst_ip: str


@dataclass(slots=True)
class GroundTruthEvent:
    start_ts: float
    end_ts: float
    label: str
    scenario_id: str | None = None
    run_id: str | None = None
    phase: str | None = None
    technique_id: str | None = None


@dataclass(slots=True)
class WindowStats:
    start_ts: float
    window_seconds: int
    short_flow_seconds: float
    small_response_bytes: int
    flow_count: int = 0
    dst_ips: set[str] = field(default_factory=set)
    dst_ports: list[int] = field(default_factory=list)
    protos: list[str] = field(default_factory=list)
    durations: list[float] = field(default_factory=list)
    src_bytes: list[int] = field(default_factory=list)
    dst_bytes: list[int] = field(default_factory=list)
    src_pkts: list[int] = field(default_factory=list)
    dst_pkts: list[int] = field(default_factory=list)
    timestamps: list[float] = field(default_factory=list)
    short_flow_count: int = 0
    small_response_count: int = 0
    zero_dst_bytes_count: int = 0
    tcp_count: int = 0
    udp_count: int = 0
    dst_port_well_known_count: int = 0
    dst_port_registered_count: int = 0
    dst_port_ephemeral_count: int = 0

    @property
    def end_ts(self) -> float:
        return self.start_ts + self.window_seconds

    def add(self, flow: Flow) -> None:
        self.flow_count += 1
        self.dst_ips.add(flow.dst_ip)
        self.dst_ports.append(flow.dst_port)
        self.protos.append(flow.proto)
        self.durations.append(flow.duration)
        self.src_bytes.append(flow.src_bytes)
        self.dst_bytes.append(flow.dst_bytes)
        self.src_pkts.append(flow.src_pkts)
        self.dst_pkts.append(flow.dst_pkts)
        self.timestamps.append(flow.ts)

        if flow.duration <= self.short_flow_seconds:
            self.short_flow_count += 1
        if flow.dst_bytes <= self.small_response_bytes:
            self.small_response_count += 1
        if flow.dst_bytes == 0:
            self.zero_dst_bytes_count += 1

        proto = flow.proto.lower()
        if proto == "tcp":
            self.tcp_count += 1
        elif proto == "udp":
            self.udp_count += 1

        if flow.dst_port < 1024:
            self.dst_port_well_known_count += 1
        elif flow.dst_port < 49152:
            self.dst_port_registered_count += 1
        else:
            self.dst_port_ephemeral_count += 1

    def to_row(
        self,
        *,
        scenario_id: str,
        run_id: str,
        label: str,
        phases: set[str],
        technique_ids: set[str],
    ) -> dict[str, Any]:
        total_src_bytes = sum(self.src_bytes)
        total_dst_bytes = sum(self.dst_bytes)
        total_src_pkts = sum(self.src_pkts)
        total_dst_pkts = sum(self.dst_pkts)
        total_bytes = [src + dst for src, dst in zip(self.src_bytes, self.dst_bytes)]
        inter_flow_times = deltas(sorted(self.timestamps))

        return {
            "window_start": iso_utc(self.start_ts),
            "window_end": iso_utc(self.end_ts),
            "scenario_id": scenario_id,
            "run_id": run_id,
            "label": label,
            "phases": join_values(phases),
            "technique_ids": join_values(technique_ids),
            "flow_count": self.flow_count,
            "unique_dst_count": len(self.dst_ips),
            "unique_dst_port_count": len(set(self.dst_ports)),
            "dst_port_entropy": rounded(entropy(self.dst_ports)),
            "proto_entropy": rounded(entropy(self.protos)),
            "short_flow_ratio": rounded(ratio(self.short_flow_count, self.flow_count)),
            "small_response_ratio": rounded(ratio(self.small_response_count, self.flow_count)),
            "zero_dst_bytes_ratio": rounded(ratio(self.zero_dst_bytes_count, self.flow_count)),
            "avg_duration": rounded(mean(self.durations)),
            "median_duration": rounded(median(self.durations)),
            "avg_src_bytes": rounded(mean(self.src_bytes)),
            "avg_dst_bytes": rounded(mean(self.dst_bytes)),
            "avg_total_bytes": rounded(mean(total_bytes)),
            "bytes_out_in_ratio": rounded(total_src_bytes / max(total_dst_bytes, 1)),
            "pkts_out_in_ratio": rounded(total_src_pkts / max(total_dst_pkts, 1)),
            "inter_flow_time_mean": rounded(mean(inter_flow_times)),
            "inter_flow_time_std": rounded(stddev(inter_flow_times)),
            "tcp_flow_ratio": rounded(ratio(self.tcp_count, self.flow_count)),
            "udp_flow_ratio": rounded(ratio(self.udp_count, self.flow_count)),
            "dst_port_well_known_ratio": rounded(
                ratio(self.dst_port_well_known_count, self.flow_count)
            ),
            "dst_port_registered_ratio": rounded(
                ratio(self.dst_port_registered_count, self.flow_count)
            ),
            "dst_port_ephemeral_ratio": rounded(
                ratio(self.dst_port_ephemeral_count, self.flow_count)
            ),
        }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build rolling/window behavior features from a Zeek conn.log file.",
    )
    parser.add_argument("--conn-log", required=True, type=Path, help="Path to Zeek conn.log")
    parser.add_argument("--output", required=True, type=Path, help="Output feature CSV path")
    parser.add_argument("--window-seconds", type=int, default=60)
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--scenario-id", default=None)
    parser.add_argument("--default-label", default="normal")
    parser.add_argument("--ground-truth", type=Path, default=None)
    parser.add_argument("--short-flow-seconds", type=float, default=1.0)
    parser.add_argument("--small-response-bytes", type=int, default=100)
    parser.add_argument(
        "--include-empty",
        action="store_true",
        help="Include empty windows between the first and last observed flow.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    run_id = args.run_id or os.getenv("SCENARIO_RUN_ID") or infer_run_id(args.conn_log)
    scenario_id = args.scenario_id or os.getenv("SCENARIO_ID") or infer_scenario_id(run_id)

    flows = list(read_flows(args.conn_log))
    windows = build_windows(
        flows,
        window_seconds=args.window_seconds,
        short_flow_seconds=args.short_flow_seconds,
        small_response_bytes=args.small_response_bytes,
        include_empty=args.include_empty,
    )
    truth_events = load_ground_truth(args.ground_truth, run_id=run_id) if args.ground_truth else []

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=OUTPUT_FIELDS)
        writer.writeheader()
        for window in sorted(windows.values(), key=lambda item: item.start_ts):
            label, phases, technique_ids, selected_scenario_id = label_for_window(
                window,
                truth_events=truth_events,
                default_label=args.default_label,
                default_scenario_id=scenario_id,
            )
            writer.writerow(
                window.to_row(
                    scenario_id=selected_scenario_id,
                    run_id=run_id,
                    label=label,
                    phases=phases,
                    technique_ids=technique_ids,
                )
            )

    print(f"wrote {len(windows)} windows to {args.output}")


def read_flows(path: Path) -> list[Flow]:
    fields: list[str] | None = None
    flows: list[Flow] = []

    with path.open("r", encoding="utf-8", errors="replace") as file:
        for raw_line in file:
            line = raw_line.rstrip("\n")
            if not line:
                continue
            if line.startswith("#"):
                if line.startswith("#fields"):
                    fields = line.split("\t")[1:]
                continue

            parts = line.split("\t")
            if looks_like_header(parts):
                fields = parts
                continue

            row_fields = fields or DEFAULT_TSV_FIELDS
            row = {
                field: parts[index] if index < len(parts) else "-"
                for index, field in enumerate(row_fields)
            }
            flow = flow_from_row(row)
            if flow is not None:
                flows.append(flow)

    return flows


def flow_from_row(row: dict[str, str]) -> Flow | None:
    ts = parse_timestamp(get_value(row, "ts", "timestamp"))
    if ts is None:
        return None

    return Flow(
        ts=ts,
        src_ip=get_value(row, "id.orig_h", "src_ip") or "-",
        dst_ip=get_value(row, "id.resp_h", "dst_ip") or "-",
        src_port=to_int(get_value(row, "id.orig_p", "src_port")),
        dst_port=to_int(get_value(row, "id.resp_p", "dst_port")),
        proto=(get_value(row, "proto") or "unknown").lower(),
        duration=to_float(get_value(row, "duration")),
        src_bytes=to_int(get_value(row, "orig_bytes", "src_bytes")),
        dst_bytes=to_int(get_value(row, "resp_bytes", "dst_bytes")),
        missed_bytes=to_int(get_value(row, "missed_bytes")),
        src_pkts=to_int(get_value(row, "orig_pkts", "src_pkts")),
        src_ip_bytes=to_int(get_value(row, "orig_ip_bytes", "src_ip_bytes")),
        dst_pkts=to_int(get_value(row, "resp_pkts", "dst_pkts")),
        dst_ip_bytes=to_int(get_value(row, "resp_ip_bytes", "dst_ip_bytes")),
    )


def build_windows(
    flows: list[Flow],
    *,
    window_seconds: int,
    short_flow_seconds: float,
    small_response_bytes: int,
    include_empty: bool,
) -> dict[float, WindowStats]:
    windows: dict[float, WindowStats] = {}
    if not flows:
        return windows

    for flow in flows:
        start_ts = math.floor(flow.ts / window_seconds) * window_seconds
        window = windows.setdefault(
            start_ts,
            WindowStats(
                start_ts=start_ts,
                window_seconds=window_seconds,
                short_flow_seconds=short_flow_seconds,
                small_response_bytes=small_response_bytes,
            ),
        )
        window.add(flow)

    if include_empty:
        first_start = min(windows)
        last_start = max(windows)
        current = first_start
        while current <= last_start:
            windows.setdefault(
                current,
                WindowStats(
                    start_ts=current,
                    window_seconds=window_seconds,
                    short_flow_seconds=short_flow_seconds,
                    small_response_bytes=small_response_bytes,
                ),
            )
            current += window_seconds

    return windows


def load_ground_truth(path: Path, *, run_id: str) -> list[GroundTruthEvent]:
    events: list[GroundTruthEvent] = []
    with path.open("r", encoding="utf-8", errors="replace") as file:
        for raw_line in file:
            line = raw_line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue

            payload_run_id = str(payload.get("run_id") or "")
            if payload_run_id and payload_run_id != run_id:
                continue

            start_ts = parse_timestamp(str(payload.get("window_start") or payload.get("timestamp") or ""))
            if start_ts is None:
                continue
            end_ts = parse_timestamp(str(payload.get("window_end") or ""))
            if end_ts is None:
                end_ts = start_ts

            label = str(payload.get("label") or "").strip()
            if not label:
                continue

            events.append(
                GroundTruthEvent(
                    start_ts=start_ts,
                    end_ts=end_ts,
                    label=label,
                    scenario_id=str(payload.get("scenario_id") or "") or None,
                    run_id=payload_run_id or None,
                    phase=str(payload.get("phase") or "") or None,
                    technique_id=str(payload.get("technique_id") or "") or None,
                )
            )
    return events


def label_for_window(
    window: WindowStats,
    *,
    truth_events: list[GroundTruthEvent],
    default_label: str,
    default_scenario_id: str,
) -> tuple[str, set[str], set[str], str]:
    matches = [
        event
        for event in truth_events
        if intervals_overlap(window.start_ts, window.end_ts, event.start_ts, event.end_ts)
    ]
    if not matches:
        return default_label, set(), set(), default_scenario_id

    labels = {event.label for event in matches if event.label}
    phases = {event.phase for event in matches if event.phase}
    technique_ids = {event.technique_id for event in matches if event.technique_id}
    scenario_ids = [event.scenario_id for event in matches if event.scenario_id]

    if "attack" in labels:
        label = "attack"
    elif len(labels) == 1:
        label = next(iter(labels))
    else:
        label = join_values(labels)

    return label, phases, technique_ids, scenario_ids[0] if scenario_ids else default_scenario_id


def intervals_overlap(left_start: float, left_end: float, right_start: float, right_end: float) -> bool:
    if right_start == right_end:
        return left_start <= right_start < left_end
    return left_start < right_end and right_start < left_end


def parse_timestamp(raw_value: str | None) -> float | None:
    if raw_value is None:
        return None

    value = raw_value.strip()
    if not value or value == "-":
        return None

    try:
        return float(value)
    except ValueError:
        pass

    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def get_value(row: dict[str, str], *names: str) -> str:
    for name in names:
        if name in row:
            return row[name]
    return ""


def to_int(raw_value: str | None) -> int:
    if raw_value is None:
        return 0
    value = raw_value.strip()
    if not value or value == "-":
        return 0
    try:
        return int(value)
    except ValueError:
        return int(float(value))


def to_float(raw_value: str | None) -> float:
    if raw_value is None:
        return 0.0
    value = raw_value.strip()
    if not value or value == "-":
        return 0.0
    return float(value)


def looks_like_header(parts: list[str]) -> bool:
    if not parts:
        return False
    return parts[0] in {"ts", "timestamp"}


def entropy(values: list[Any]) -> float:
    if not values:
        return 0.0
    counts: dict[Any, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    total = len(values)
    return -sum((count / total) * math.log2(count / total) for count in counts.values())


def ratio(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return numerator / denominator


def mean(values: list[float] | list[int]) -> float:
    if not values:
        return 0.0
    return float(sum(values) / len(values))


def median(values: list[float]) -> float:
    if not values:
        return 0.0
    return float(statistics.median(values))


def stddev(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    return float(statistics.pstdev(values))


def deltas(values: list[float]) -> list[float]:
    return [right - left for left, right in zip(values, values[1:])]


def rounded(value: float) -> float:
    rounded_value = round(value, 6)
    return 0.0 if rounded_value == 0 else rounded_value


def iso_utc(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()


def join_values(values: set[str]) -> str:
    return ";".join(sorted(value for value in values if value))


def infer_run_id(path: Path) -> str:
    if path.name == "conn.log" and path.parent.name:
        return path.parent.name
    return path.stem


def infer_scenario_id(run_id: str) -> str:
    if "-" in run_id:
        return run_id.rsplit("-", 1)[0]
    return "unknown"


if __name__ == "__main__":
    main()
