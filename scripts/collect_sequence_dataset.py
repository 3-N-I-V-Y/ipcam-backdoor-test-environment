from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any

import collect_bulk_scan_dataset as bulk


STATE_VERSION = 1
TERMINAL_RUN_STATUSES = {"zeek_done", "skipped_existing"}
DEFAULT_SEQUENCE_ROOT = Path("data/features/sequences")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Orchestrate long-running 10s window collection, checkpoint each run, "
            "build sequence datasets, and optionally train GRU/LSTM models."
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    for name in ("plan", "run", "resume", "finalize", "status"):
        command = subparsers.add_parser(name)
        command.add_argument("--config", type=Path, required=True)
        command.add_argument("--dry-run", action="store_true")
        command.add_argument(
            "--allow-config-change",
            action="store_true",
            help="Continue when an existing state file was created from a different config.",
        )
        if name == "plan":
            command.add_argument("--write-state", action="store_true")
        if name in {"run", "resume"}:
            command.add_argument(
                "--finalize",
                action="store_true",
                help="Rebuild CSVs, build sequence NPZ files, and write reports after collection.",
            )
            command.add_argument(
                "--train",
                action="store_true",
                help="Train the configured GRU/LSTM models during finalize.",
            )
        if name == "resume":
            command.add_argument(
                "--retry-failed",
                action="store_true",
                help="Retry runs currently marked failed.",
            )
        if name == "finalize":
            command.add_argument(
                "--train",
                action="store_true",
                help="Train the configured GRU/LSTM models after building sequence NPZ files.",
            )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_config(args.config)
    records = build_plan(config)
    state_path = path_from_config(config, "state_path", default_state_path(config))
    lock_path = state_path.with_suffix(state_path.suffix + ".lock")

    if args.command == "plan":
        write_plan(config, records)
        if args.write_state:
            state = fresh_state(config, records)
            save_state(state_path, state)
            print(f"wrote state to {state_path}")
        return

    if args.command == "status":
        state = load_or_create_state(state_path, config, records, allow_config_change=True)
        print_status(state)
        return

    if args.command == "finalize":
        state = load_or_create_state(
            state_path,
            config,
            records,
            allow_config_change=args.allow_config_change,
        )
        finalize(config, records, state, state_path, dry_run=args.dry_run, train=args.train)
        return

    with acquire_lock(lock_path):
        state = load_or_create_state(
            state_path,
            config,
            records,
            allow_config_change=args.allow_config_change,
        )
        check_free_disk(config)
        prepare_docker(config, dry_run=args.dry_run)
        retry_failed = args.command == "resume" and args.retry_failed
        run_collection(
            config,
            records,
            state,
            state_path,
            dry_run=args.dry_run,
            retry_failed=retry_failed,
        )
        if args.finalize:
            finalize(config, records, state, state_path, dry_run=args.dry_run, train=args.train)


def load_config(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as file:
        config = json.load(file)
    if "run_prefix" not in config:
        raise SystemExit("config must define run_prefix")
    if "dataset_output" not in config:
        raise SystemExit("config must define dataset_output")
    if not config.get("sequence_lengths"):
        raise SystemExit("config must define at least one sequence length")
    return config


def config_hash(config: dict[str, Any]) -> str:
    normalized = json.dumps(config, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def path_from_config(config: dict[str, Any], key: str, fallback: Path | None = None) -> Path:
    value = config.get(key)
    if value:
        return Path(str(value))
    if fallback is None:
        raise KeyError(key)
    return fallback


def default_state_path(config: dict[str, Any]) -> Path:
    root = Path(str(config.get("sequence_root") or DEFAULT_SEQUENCE_ROOT))
    return root / f"{config['run_prefix']}-state.json"


def default_plan_json_path(config: dict[str, Any]) -> Path:
    root = Path(str(config.get("plan_root") or "data/features/datasets"))
    return root / f"{config['run_prefix']}-sequence-plan.json"


def default_plan_md_path(config: dict[str, Any]) -> Path:
    root = Path(str(config.get("plan_root") or "data/features/datasets"))
    return root / f"{config['run_prefix']}-sequence-plan.md"


def default_report_json_path(config: dict[str, Any]) -> Path:
    root = Path(str(config.get("sequence_root") or DEFAULT_SEQUENCE_ROOT))
    return root / f"{config['run_prefix']}-sequence-report.json"


def default_report_md_path(config: dict[str, Any]) -> Path:
    root = Path(str(config.get("sequence_root") or DEFAULT_SEQUENCE_ROOT))
    return root / f"{config['run_prefix']}-sequence-report.md"


def build_plan(config: dict[str, Any]) -> list[dict[str, Any]]:
    run_prefix = str(config["run_prefix"])
    selected = set(config.get("scenarios") or [])
    test_repeats = {int(value) for value in config.get("test_repeats", [2])}
    records: list[dict[str, Any]] = []

    baseline = config.get("baseline", {})
    baseline_repeats = int(baseline.get("repeats", 0))
    baseline_seconds = float(baseline.get("seconds", 0))
    if (not selected or "baseline" in selected) and baseline_repeats > 0:
        for repeat_index in range(1, baseline_repeats + 1):
            records.append(
                {
                    "run_id": f"{run_prefix}-baseline-{repeat_index:03d}",
                    "scenario_id": "baseline",
                    "scan_type": None,
                    "repeat_index": repeat_index,
                    "split": split_for_repeat(repeat_index, test_repeats),
                    "seconds": baseline_seconds,
                    "targets": [],
                    "ports": [],
                    "long_capture": False,
                }
            )

    for attack in config.get("attacks", []):
        scenario_id = str(attack["scenario_id"])
        if selected and scenario_id not in selected:
            continue
        repeats = int(attack.get("repeats", 0))
        for repeat_index in range(1, repeats + 1):
            duration = float(attack.get("duration_seconds") or config.get("attack_duration_seconds") or 0)
            if not attack.get("long_capture", True):
                duration = 0.0
            if duration <= 0 and attack.get("scan_type") == "low-and-slow":
                duration = estimate_low_and_slow_seconds(attack)
            if duration <= 0:
                duration = float(config.get("window_seconds", 10))
            records.append(
                {
                    "run_id": f"{run_prefix}-{scenario_id}-{repeat_index:03d}",
                    "scenario_id": scenario_id,
                    "scan_type": str(attack["scan_type"]),
                    "repeat_index": repeat_index,
                    "split": split_for_repeat(repeat_index, test_repeats),
                    "seconds": duration,
                    "targets": parse_csv(attack.get("targets", "")),
                    "ports": parse_csv(attack.get("ports", "")),
                    "long_capture": bool(attack.get("long_capture", True)),
                    "interval_seconds": float(
                        attack.get("interval_seconds", config.get("attack_interval_seconds", 10))
                    ),
                    "max_scan_repeats": int(
                        attack.get("max_scan_repeats", config.get("attack_max_scan_repeats", 0))
                    ),
                }
            )

    return records


def split_for_repeat(repeat_index: int, test_repeats: set[int]) -> str:
    return "test" if repeat_index in test_repeats else "train"


def estimate_low_and_slow_seconds(attack: dict[str, Any]) -> float:
    return float(max(1, len(parse_csv(attack.get("targets", ""))) * len(parse_csv(attack.get("ports", "")))) * 60)


def parse_csv(value: Any) -> list[str]:
    return [part.strip() for part in str(value or "").split(",") if part.strip()]


def write_plan(config: dict[str, Any], records: list[dict[str, Any]]) -> None:
    window_seconds = int(config.get("window_seconds", 10))
    sequence_lengths = [int(value) for value in config["sequence_lengths"]]
    expected_active_src_entities = int(config.get("expected_active_src_entities", 1))
    split_estimates: dict[str, dict[str, float | int]] = {
        "train": {"runs": 0, "windows": 0, "seconds": 0.0},
        "test": {"runs": 0, "windows": 0, "seconds": 0.0},
    }
    split_sequence_estimates: dict[str, dict[str, int]] = {
        str(length): {"train": 0, "test": 0, "total": 0}
        for length in sequence_lengths
    }
    scenario_estimates: dict[str, dict[str, float | int]] = {}

    for record in records:
        windows = estimated_windows(record, window_seconds)
        split = record["split"]
        split_estimates[split]["runs"] = int(split_estimates[split]["runs"]) + 1
        split_estimates[split]["windows"] = int(split_estimates[split]["windows"]) + windows
        split_estimates[split]["seconds"] = float(split_estimates[split]["seconds"]) + float(record["seconds"])
        scenario = scenario_estimates.setdefault(
            str(record["scenario_id"]),
            {"runs": 0, "windows": 0, "seconds": 0.0},
        )
        scenario["runs"] = int(scenario["runs"]) + 1
        scenario["windows"] = int(scenario["windows"]) + windows
        scenario["seconds"] = float(scenario["seconds"]) + float(record["seconds"])
        for length in sequence_lengths:
            sequence_count = estimated_sequences(record, window_seconds, length, int(config.get("stride", 1)))
            length_key = str(length)
            split_sequence_estimates[length_key][split] += sequence_count
            split_sequence_estimates[length_key]["total"] += sequence_count

    payload = {
        "run_prefix": config["run_prefix"],
        "dataset_output": config["dataset_output"],
        "window_seconds": window_seconds,
        "sequence_lengths": sequence_lengths,
        "expected_active_src_entities": expected_active_src_entities,
        "stride": int(config.get("stride", 1)),
        "total_runs": len(records),
        "estimated_hours": round(sum(float(record["seconds"]) for record in records) / 3600.0, 3),
        "split_estimates": split_estimates,
        "sequence_estimates_per_active_group": split_sequence_estimates,
        "sequence_estimates_expected": multiply_sequence_estimates(
            split_sequence_estimates,
            expected_active_src_entities,
        ),
        "scenario_estimates": scenario_estimates,
        "run_estimates": [
            {
                **record,
                "estimated_windows": estimated_windows(record, window_seconds),
                "estimated_sequences": {
                    str(length): estimated_sequences(record, window_seconds, length, int(config.get("stride", 1)))
                    for length in sequence_lengths
                },
            }
            for record in records
        ],
        "notes": [
            "This estimate is based on requested run durations.",
            "Actual sequence counts depend on active src_entity counts and Zeek conn rows.",
            "Final metrics must use run_id-separated real test data.",
        ],
    }
    json_path = path_from_config(config, "plan_output_json", default_plan_json_path(config))
    md_path = path_from_config(config, "plan_output_md", default_plan_md_path(config))
    write_json(json_path, payload)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(render_plan_md(payload), encoding="utf-8")
    print(f"wrote plan json to {json_path}")
    print(f"wrote plan md to {md_path}")
    print(f"estimated_hours={payload['estimated_hours']}")


def estimated_windows(record: dict[str, Any], window_seconds: int) -> int:
    return max(1, int(float(record["seconds"]) // max(window_seconds, 1)))


def estimated_sequences(record: dict[str, Any], window_seconds: int, sequence_length: int, stride: int) -> int:
    windows = estimated_windows(record, window_seconds)
    if windows < sequence_length:
        return 0
    return ((windows - sequence_length) // max(stride, 1)) + 1


def render_plan_md(payload: dict[str, Any]) -> str:
    lines = [
        "# Sequence Collection Plan",
        "",
        f"- Run prefix: {payload['run_prefix']}",
        f"- Dataset output: {payload['dataset_output']}",
        f"- Window seconds: {payload['window_seconds']}",
        f"- Sequence lengths: {', '.join(str(value) for value in payload['sequence_lengths'])}",
        f"- Expected active src_entity groups: {payload['expected_active_src_entities']}",
        f"- Total runs: {payload['total_runs']}",
        f"- Estimated sequential hours: {payload['estimated_hours']}",
        "",
        "## Split Estimates",
        "",
        "| split | runs | windows | hours |",
        "| --- | ---: | ---: | ---: |",
    ]
    for split, record in payload["split_estimates"].items():
        lines.append(
            f"| {split} | {record['runs']} | {record['windows']} | {float(record['seconds']) / 3600.0:.3f} |"
        )
    lines.extend(
        [
            "",
            "## Sequence Estimates Per Active Group",
            "",
            "| length | train | test | total |",
            "| ---: | ---: | ---: | ---: |",
        ]
    )
    for length, record in sorted(payload["sequence_estimates_per_active_group"].items(), key=lambda item: int(item[0])):
        lines.append(f"| {length} | {record['train']} | {record['test']} | {record['total']} |")
    lines.extend(
        [
            "",
            "## Expected Sequence Estimates",
            "",
            "| length | train | test | total |",
            "| ---: | ---: | ---: | ---: |",
        ]
    )
    for length, record in sorted(payload["sequence_estimates_expected"].items(), key=lambda item: int(item[0])):
        lines.append(f"| {length} | {record['train']} | {record['test']} | {record['total']} |")
    lines.extend(["", "## Scenario Estimates", "", "| scenario | runs | windows | hours |", "| --- | ---: | ---: | ---: |"])
    for scenario_id, record in sorted(payload["scenario_estimates"].items()):
        lines.append(
            f"| {scenario_id} | {record['runs']} | {record['windows']} | {float(record['seconds']) / 3600.0:.3f} |"
        )
    lines.extend(["", "## Notes", ""])
    for note in payload["notes"]:
        lines.append(f"- {note}")
    lines.append("")
    return "\n".join(lines)


def multiply_sequence_estimates(
    estimates: dict[str, dict[str, int]],
    multiplier: int,
) -> dict[str, dict[str, int]]:
    multiplier = max(1, multiplier)
    return {
        length: {
            split: count * multiplier
            for split, count in record.items()
        }
        for length, record in estimates.items()
    }


def fresh_state(config: dict[str, Any], records: list[dict[str, Any]]) -> dict[str, Any]:
    now = bulk.time.strftime("%Y-%m-%dT%H:%M:%SZ", bulk.time.gmtime())
    return {
        "version": STATE_VERSION,
        "config_hash": config_hash(config),
        "run_prefix": config["run_prefix"],
        "created_at": now,
        "updated_at": now,
        "runs": {
            record["run_id"]: {
                "run_id": record["run_id"],
                "scenario_id": record["scenario_id"],
                "split": record["split"],
                "repeat_index": record["repeat_index"],
                "status": "pending",
                "attempts": 0,
                "error": "",
            }
            for record in records
        },
        "finalize": {
            "status": "pending",
            "dataset": "",
            "reports": {},
            "sequences": {},
            "models": {},
            "error": "",
        },
    }


def load_or_create_state(
    state_path: Path,
    config: dict[str, Any],
    records: list[dict[str, Any]],
    *,
    allow_config_change: bool,
) -> dict[str, Any]:
    if not state_path.exists():
        state = fresh_state(config, records)
        save_state(state_path, state)
        return state

    with state_path.open("r", encoding="utf-8") as file:
        state = json.load(file)
    expected = config_hash(config)
    if state.get("config_hash") != expected and not allow_config_change:
        raise SystemExit(
            f"state config hash does not match {state_path}. "
            "Pass --allow-config-change if this is intentional."
        )
    ensure_state_runs(state, records)
    return state


def ensure_state_runs(state: dict[str, Any], records: list[dict[str, Any]]) -> None:
    runs = state.setdefault("runs", {})
    for record in records:
        runs.setdefault(
            record["run_id"],
            {
                "run_id": record["run_id"],
                "scenario_id": record["scenario_id"],
                "split": record["split"],
                "repeat_index": record["repeat_index"],
                "status": "pending",
                "attempts": 0,
                "error": "",
            },
        )
    state.setdefault(
        "finalize",
        {"status": "pending", "dataset": "", "reports": {}, "sequences": {}, "models": {}, "error": ""},
    )


def save_state(path: Path, state: dict[str, Any]) -> None:
    state["updated_at"] = bulk.time.strftime("%Y-%m-%dT%H:%M:%SZ", bulk.time.gmtime())
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp_path.replace(path)


def print_status(state: dict[str, Any]) -> None:
    counts: dict[str, int] = {}
    for run in state.get("runs", {}).values():
        status = str(run.get("status", "unknown"))
        counts[status] = counts.get(status, 0) + 1
    print(json.dumps({"run_prefix": state.get("run_prefix"), "run_status_counts": counts, "finalize": state.get("finalize")}, indent=2, sort_keys=True))


def check_free_disk(config: dict[str, Any]) -> None:
    min_free_gb = float(config.get("min_free_disk_gb") or 0)
    if min_free_gb <= 0:
        return
    data_root = path_from_config(config, "data_root", Path("data"))
    data_root.mkdir(parents=True, exist_ok=True)
    free_gb = shutil.disk_usage(data_root).free / (1024**3)
    if free_gb < min_free_gb:
        raise SystemExit(f"free disk is {free_gb:.1f} GiB, below configured minimum {min_free_gb:.1f} GiB")


def prepare_docker(config: dict[str, Any], *, dry_run: bool) -> None:
    if config.get("compose_up", False):
        run_command(["docker", "compose", "up", "-d", "--build"], dry_run=dry_run)
    if config.get("skip_pull", True):
        return
    for image in (
        bulk.DEFAULT_TCPDUMP_IMAGE,
        str(config.get("nmap_image", "instrumentisto/nmap:latest")),
        str(config.get("zeek_image", "zeek/zeek:latest")),
    ):
        run_command(["docker", "pull", image], dry_run=dry_run)


def run_collection(
    config: dict[str, Any],
    records: list[dict[str, Any]],
    state: dict[str, Any],
    state_path: Path,
    *,
    dry_run: bool,
    retry_failed: bool,
) -> None:
    if dry_run:
        for record in records:
            run_state = state["runs"].get(record["run_id"], {})
            status = str(run_state.get("status", "pending"))
            if status in TERMINAL_RUN_STATUSES:
                continue
            if status == "failed" and not retry_failed:
                continue
            if should_skip_existing(config, record):
                print(f"would skip existing run: {record['run_id']}")
                continue
            collect_one(config, record, dry_run=True)
            bulk.convert_pcap_to_zeek(bulk_args(config, record, dry_run=True), record["run_id"])
        return

    for record in records:
        run_state = state["runs"][record["run_id"]]
        status = str(run_state.get("status", "pending"))
        if status in TERMINAL_RUN_STATUSES:
            continue
        if status == "failed" and not retry_failed:
            continue

        run_state["status"] = "capturing"
        run_state["attempts"] = int(run_state.get("attempts", 0)) + 1
        run_state["error"] = ""
        save_state(state_path, state)

        try:
            if should_skip_existing(config, record):
                run_state["status"] = "skipped_existing"
                save_state(state_path, state)
                print(f"skipping existing run: {record['run_id']}")
                continue

            collect_one(config, record, dry_run=dry_run)
            run_state["status"] = "pcap_done"
            save_state(state_path, state)

            bulk.convert_pcap_to_zeek(bulk_args(config, record, dry_run=dry_run), record["run_id"])
            run_state["status"] = "zeek_done"
            save_state(state_path, state)
        except Exception as exc:
            run_state["status"] = "failed"
            run_state["error"] = str(exc)
            save_state(state_path, state)
            raise


def should_skip_existing(config: dict[str, Any], record: dict[str, Any]) -> bool:
    if not config.get("skip_existing", True):
        return False
    conn_log = path_from_config(config, "data_root", Path("data")) / "zeek" / record["run_id"] / "conn.log"
    return conn_log.exists()


def collect_one(config: dict[str, Any], record: dict[str, Any], *, dry_run: bool) -> None:
    args = bulk_args(config, record, dry_run=dry_run)
    if record["scan_type"] is None:
        bulk.collect_baseline(args, record["run_id"])
        return

    plan = bulk.ScenarioPlan(
        scenario_id=str(record["scenario_id"]),
        scan_type=str(record["scan_type"]),
        targets=",".join(record["targets"]),
        ports=",".join(record["ports"]),
        repeats=1,
    )
    bulk.collect_attack(args, record["run_id"], plan)


def bulk_args(config: dict[str, Any], record: dict[str, Any] | None = None, *, dry_run: bool) -> argparse.Namespace:
    record = record or {}
    duration = float(record.get("seconds", config.get("attack_duration_seconds", 0)) or 0)
    use_repeat_capture = bool(record.get("scan_type") is not None and record.get("long_capture", False))
    return argparse.Namespace(
        compose_service=str(config.get("compose_service", "camera-app")),
        compose_up=bool(config.get("compose_up", False)),
        skip_pull=bool(config.get("skip_pull", True)),
        skip_existing=bool(config.get("skip_existing", True)),
        baseline_repeats=0,
        baseline_seconds=float(record.get("seconds", config.get("baseline", {}).get("seconds", 0)) or 0),
        attack_repeats=0,
        attack_duration_seconds=duration if use_repeat_capture else 0.0,
        long_capture_scenario=[str(record.get("scenario_id"))] if use_repeat_capture else [],
        attack_interval_seconds=float(record.get("interval_seconds", config.get("attack_interval_seconds", 10)) or 10),
        attack_max_scan_repeats=int(record.get("max_scan_repeats", config.get("attack_max_scan_repeats", 0)) or 0),
        scenario=[],
        run_prefix=str(config["run_prefix"]),
        window_seconds=int(config.get("window_seconds", 10)),
        test_repeat=[int(value) for value in config.get("test_repeats", [2])],
        data_root=path_from_config(config, "data_root", Path("data")),
        capture_interface=str(config.get("capture_interface", "any")),
        capture_filter=str(config.get("capture_filter", bulk.DEFAULT_CAPTURE_FILTER)),
        capture_network_mode=str(config.get("capture_network_mode", "host")),
        scenario_log_root=path_from_config(config, "scenario_log_root", Path("data/scenarios/generated")),
        dataset_output=path_from_config(config, "dataset_output"),
        summary_output=None,
        estimate_only=False,
        plan_output_json=path_from_config(config, "plan_output_json", default_plan_json_path(config)),
        plan_output_md=path_from_config(config, "plan_output_md", default_plan_md_path(config)),
        min_train_windows=int(config.get("min_train_windows", 0)),
        min_test_windows=int(config.get("min_test_windows", 0)),
        dry_run=dry_run,
    )


def finalize(
    config: dict[str, Any],
    records: list[dict[str, Any]],
    state: dict[str, Any],
    state_path: Path,
    *,
    dry_run: bool,
    train: bool,
) -> None:
    if dry_run:
        successful = records
        if not successful:
            raise SystemExit("no planned runs are available for finalize dry-run")
        test_run_ids = [record["run_id"] for record in successful if record["split"] == "test"]
        common_args = bulk_args(config, dry_run=True)
        bulk.rebuild_dataset(
            common_args,
            [(record["run_id"], record["scenario_id"]) for record in successful],
            test_run_ids,
        )
        dataset_output = path_from_config(config, "dataset_output")
        train_csv = dataset_output.with_name(f"{dataset_output.stem}-train.csv")
        test_csv = dataset_output.with_name(f"{dataset_output.stem}-test.csv")
        sequence_outputs = build_sequences(config, train_csv, test_csv, dry_run=True)
        write_sequence_report(config, dataset_output, train_csv, test_csv, sequence_outputs, dry_run=True)
        train_models(config, sequence_outputs, dry_run=True, force_train=train)
        return

    final_state = state["finalize"]
    final_state["status"] = "running"
    final_state["error"] = ""
    save_state(state_path, state)

    try:
        successful = successful_records(config, records, state)
        if not successful:
            raise SystemExit("no successful Zeek runs are available for finalize")
        test_run_ids = [record["run_id"] for record in successful if record["split"] == "test"]
        if not test_run_ids:
            raise SystemExit("finalize requires at least one successful test run")

        common_args = bulk_args(config, dry_run=dry_run)
        bulk.rebuild_dataset(
            common_args,
            [(record["run_id"], record["scenario_id"]) for record in successful],
            test_run_ids,
        )

        dataset_output = path_from_config(config, "dataset_output")
        train_csv = dataset_output.with_name(f"{dataset_output.stem}-train.csv")
        test_csv = dataset_output.with_name(f"{dataset_output.stem}-test.csv")
        sequence_outputs = build_sequences(config, train_csv, test_csv, dry_run=dry_run)
        report_paths = write_sequence_report(config, dataset_output, train_csv, test_csv, sequence_outputs, dry_run=dry_run)
        model_outputs = train_models(config, sequence_outputs, dry_run=dry_run, force_train=train)

        final_state["status"] = "complete"
        final_state["dataset"] = str(dataset_output)
        final_state["reports"] = {key: str(value) for key, value in report_paths.items()}
        final_state["sequences"] = {str(key): {name: str(path) for name, path in value.items()} for key, value in sequence_outputs.items()}
        final_state["models"] = {str(key): str(value) for key, value in model_outputs.items()}
        save_state(state_path, state)
    except Exception as exc:
        final_state["status"] = "failed"
        final_state["error"] = str(exc)
        save_state(state_path, state)
        raise


def successful_records(
    config: dict[str, Any],
    records: list[dict[str, Any]],
    state: dict[str, Any],
) -> list[dict[str, Any]]:
    data_root = path_from_config(config, "data_root", Path("data"))
    result = []
    for record in records:
        run_state = state.get("runs", {}).get(record["run_id"], {})
        if run_state.get("status") not in TERMINAL_RUN_STATUSES:
            continue
        if (data_root / "zeek" / record["run_id"] / "conn.log").exists():
            result.append(record)
    return result


def build_sequences(
    config: dict[str, Any],
    train_csv: Path,
    test_csv: Path,
    *,
    dry_run: bool,
) -> dict[int, dict[str, Path]]:
    sequence_root = Path(str(config.get("sequence_root") or DEFAULT_SEQUENCE_ROOT))
    run_prefix = str(config["run_prefix"])
    outputs: dict[int, dict[str, Path]] = {}
    for length in [int(value) for value in config["sequence_lengths"]]:
        base = sequence_root / f"{run_prefix}-n{length}"
        train_output = base.with_name(f"{base.name}-train.npz")
        test_output = base.with_name(f"{base.name}-test.npz")
        metadata_output = base.with_name(f"{base.name}-metadata.json")
        command = [
            sys.executable,
            "scripts/build_gru_sequences.py",
            "--train-input",
            str(train_csv),
            "--test-input",
            str(test_csv),
            "--sequence-length",
            str(length),
            "--stride",
            str(int(config.get("stride", 1))),
            "--target-column",
            str(config.get("target_column", "scan_subtype")),
            "--train-output",
            str(train_output),
            "--test-output",
            str(test_output),
            "--metadata-output",
            str(metadata_output),
        ]
        feature_columns = config.get("feature_columns")
        if feature_columns:
            command.extend(["--feature-columns", str(feature_columns)])
        if config.get("pad_start", False):
            command.append("--pad-start")
        run_command(command, dry_run=dry_run)
        outputs[length] = {"train": train_output, "test": test_output, "metadata": metadata_output}
    return outputs


def write_sequence_report(
    config: dict[str, Any],
    dataset_output: Path,
    train_csv: Path,
    test_csv: Path,
    sequence_outputs: dict[int, dict[str, Path]],
    *,
    dry_run: bool,
) -> dict[str, Path]:
    output_json = path_from_config(config, "report_output_json", default_report_json_path(config))
    output_md = path_from_config(config, "report_output_md", default_report_md_path(config))
    command = [
        sys.executable,
        "scripts/build_sequence_report.py",
        "--dataset",
        str(dataset_output),
        "--train",
        str(train_csv),
        "--test",
        str(test_csv),
        "--group-columns",
        "run_id,src_entity",
        "--stride",
        str(int(config.get("stride", 1))),
        "--output-json",
        str(output_json),
        "--output-md",
        str(output_md),
    ]
    for length in [int(value) for value in config["sequence_lengths"]]:
        command.extend(["--sequence-length", str(length)])
    for outputs in sequence_outputs.values():
        command.extend(["--sequence-metadata", str(outputs["metadata"])])
        command.extend(["--sequence-npz", str(outputs["train"])])
        command.extend(["--sequence-npz", str(outputs["test"])])
    run_command(command, dry_run=dry_run)
    return {"json": output_json, "markdown": output_md}


def train_models(
    config: dict[str, Any],
    sequence_outputs: dict[int, dict[str, Path]],
    *,
    dry_run: bool,
    force_train: bool,
) -> dict[int, Path]:
    training = config.get("training", {})
    if not force_train and not training.get("enabled", False):
        return {}
    rnn_type = str(training.get("rnn_type", "gru"))
    output_root = Path(str(training.get("output_root") or "data/models"))
    outputs: dict[int, Path] = {}
    for length, paths in sequence_outputs.items():
        output_dir = output_root / f"{rnn_type}-{config['run_prefix']}-n{length}"
        command = [
            sys.executable,
            "scripts/train_gru_scan_subtype.py",
            "--rnn-type",
            rnn_type,
            "--train",
            str(paths["train"]),
            "--test",
            str(paths["test"]),
            "--output-dir",
            str(output_dir),
            "--epochs",
            str(int(training.get("epochs", 30))),
            "--batch-size",
            str(int(training.get("batch_size", 32))),
            "--hidden-size",
            str(int(training.get("hidden_size", 64))),
            "--num-layers",
            str(int(training.get("num_layers", 1))),
            "--dropout",
            str(float(training.get("dropout", 0.1))),
            "--lr",
            str(float(training.get("lr", 0.001))),
            "--weight-decay",
            str(float(training.get("weight_decay", 0.0001))),
            "--class-weight",
            str(training.get("class_weight", "balanced")),
            "--device",
            str(training.get("device", "auto")),
        ]
        run_command(command, dry_run=dry_run)
        outputs[length] = output_dir
    return outputs


def run_command(command: list[str], *, dry_run: bool) -> None:
    print(" ".join(command))
    if dry_run:
        return
    subprocess.run(command, check=True)


class acquire_lock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.fd: int | None = None

    def __enter__(self) -> "acquire_lock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as exc:
            raise SystemExit(f"collector lock already exists: {self.path}") from exc
        os.write(self.fd, str(os.getpid()).encode("utf-8"))
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        if self.fd is not None:
            os.close(self.fd)
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
