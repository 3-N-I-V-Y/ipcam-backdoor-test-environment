from __future__ import annotations

import argparse
import csv
from collections import Counter
from dataclasses import dataclass
import json
from pathlib import Path
import subprocess
import sys
import time


DEFAULT_TCPDUMP_IMAGE = "nicolaka/netshoot:latest"
DEFAULT_CAPTURE_FILTER = "tcp or (udp and not port 8000 and not port 8001)"
MIN_TRAIN_WINDOWS_FOR_OPERATIONAL_CLAIM = 500
MIN_TEST_WINDOWS_FOR_OPERATIONAL_CLAIM = 200


@dataclass(frozen=True, slots=True)
class ScenarioPlan:
    scenario_id: str
    scan_type: str | None
    targets: str
    ports: str
    repeats: int


DEFAULT_ATTACK_PLANS = [
    ScenarioPlan(
        scenario_id="vertical-scan",
        scan_type="vertical",
        targets="nvr-console",
        ports="22,23,80,443,554,8554,8080,8090,8091,8888",
        repeats=2,
    ),
    ScenarioPlan(
        scenario_id="horizontal-scan",
        scan_type="horizontal",
        targets="control-server,nvr-console,mediamtx,camera-app",
        ports="80",
        repeats=2,
    ),
    ScenarioPlan(
        scenario_id="service-probe",
        scan_type="service-probe",
        targets="control-server,nvr-console,mediamtx,camera-app",
        ports="80,8080,8090,8091,8554,8888",
        repeats=2,
    ),
    ScenarioPlan(
        scenario_id="udp-scan",
        scan_type="udp",
        targets="mediamtx,control-server,nvr-console",
        ports="53,123,161,1900,5353",
        repeats=2,
    ),
    ScenarioPlan(
        scenario_id="low-and-slow",
        scan_type="low-and-slow",
        targets="control-server,nvr-console,mediamtx",
        ports="22,23,80,443,554,8554,8080,8090,8091,8888",
        repeats=2,
    ),
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Collect multiple Docker-lab scan runs, convert pcaps with Docker Zeek, "
            "and rebuild window feature datasets. This is intended to resolve "
            "real-data volume gaps without using synthetic rows as test evidence."
        )
    )
    parser.add_argument("--compose-service", default="camera-app")
    parser.add_argument("--compose-up", action="store_true")
    parser.add_argument("--skip-pull", action="store_true")
    parser.add_argument("--skip-existing", action="store_true")
    parser.add_argument("--baseline-repeats", type=int, default=3)
    parser.add_argument("--baseline-seconds", type=int, default=600)
    parser.add_argument("--attack-repeats", type=int, default=None)
    parser.add_argument(
        "--attack-duration-seconds",
        type=float,
        default=0.0,
        help=(
            "When greater than 0, keep each attack pcap open for this long and "
            "repeat nmap attempts inside the same run. This creates enough 60s "
            "windows for run-separated real-data evaluation."
        ),
    )
    parser.add_argument(
        "--long-capture-scenario",
        action="append",
        default=[],
        choices=(
            "vertical-scan",
            "horizontal-scan",
            "service-probe",
            "udp-scan",
            "low-and-slow",
        ),
        help=(
            "Attack scenario that should use --attack-duration-seconds. Can be repeated. "
            "When omitted, the duration applies to every attack scenario."
        ),
    )
    parser.add_argument(
        "--attack-interval-seconds",
        type=float,
        default=10.0,
        help="Sleep between repeated nmap attempts during long attack captures.",
    )
    parser.add_argument(
        "--attack-max-scan-repeats",
        type=int,
        default=0,
        help="Optional cap for repeated nmap attempts per attack run. 0 means no cap.",
    )
    parser.add_argument(
        "--scenario",
        action="append",
        default=[],
        choices=(
            "baseline",
            "vertical-scan",
            "horizontal-scan",
            "service-probe",
            "udp-scan",
            "low-and-slow",
        ),
        help="Scenario to collect. Can be repeated. Defaults to all scenarios.",
    )
    parser.add_argument("--run-prefix", default="")
    parser.add_argument("--window-seconds", type=int, default=60)
    parser.add_argument(
        "--test-repeat",
        action="append",
        type=int,
        default=[],
        help=(
            "Repeat index to place in the run-based test split. Can be repeated. "
            "Defaults to repeat 2 when omitted."
        ),
    )
    parser.add_argument("--data-root", type=Path, default=Path("data"))
    parser.add_argument("--capture-interface", default="any")
    parser.add_argument("--capture-filter", default=DEFAULT_CAPTURE_FILTER)
    parser.add_argument(
        "--capture-network-mode",
        choices=("host", "container"),
        default="host",
        help=(
            "Where to run tcpdump. host is the default because Docker Desktop/WSL "
            "can miss nmap TCP flows when tcpdump shares the camera-app namespace."
        ),
    )
    parser.add_argument(
        "--scenario-log-root",
        type=Path,
        default=Path("data/scenarios/generated"),
        help=(
            "Writable root for scenario events and ground-truth generated by this collector. "
            "A subdirectory avoids host permission conflicts with logs written by containers."
        ),
    )
    parser.add_argument(
        "--dataset-output",
        type=Path,
        default=Path("data/features/datasets/ipcam-scan-subtype-60s.csv"),
    )
    parser.add_argument(
        "--summary-output",
        type=Path,
        default=None,
        help=(
            "Collection summary JSON. Defaults to "
            "data/features/datasets/<run-prefix>-collection-summary.json when "
            "--run-prefix is set."
        ),
    )
    parser.add_argument(
        "--estimate-only",
        action="store_true",
        help="Write a collection plan estimate and exit without Docker access.",
    )
    parser.add_argument(
        "--plan-output-json",
        type=Path,
        default=Path("data/features/datasets/bulk-collection-plan.json"),
    )
    parser.add_argument(
        "--plan-output-md",
        type=Path,
        default=Path("data/features/datasets/bulk-collection-plan.md"),
    )
    parser.add_argument("--min-train-windows", type=int, default=MIN_TRAIN_WINDOWS_FOR_OPERATIONAL_CLAIM)
    parser.add_argument("--min-test-windows", type=int, default=MIN_TEST_WINDOWS_FOR_OPERATIONAL_CLAIM)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    plans = build_plans(args)
    test_repeats = set(args.test_repeat or [2])
    collected_runs, test_run_ids = split_runs(plans, test_repeats)

    if args.estimate_only:
        report = estimate_collection_plan(args, plans, test_repeats)
        write_plan_outputs(args, report)
        print(f"wrote {args.plan_output_json}")
        print(f"wrote {args.plan_output_md}")
        print(
            "estimated_train_windows="
            f"{report['split_estimates']['train']['estimated_windows']}"
        )
        print(
            "estimated_test_windows="
            f"{report['split_estimates']['test']['estimated_windows']}"
        )
        print("volume_gate=" + ("pass" if report["volume_gate_pass"] else "fail"))
        return

    if args.compose_up:
        run(["docker", "compose", "up", "-d", "--build"], dry_run=args.dry_run)

    if not args.skip_pull:
        for image in (DEFAULT_TCPDUMP_IMAGE, "instrumentisto/nmap:latest", "zeek/zeek:latest"):
            run(["docker", "pull", image], dry_run=args.dry_run)

    for run_id, plan, repeat_index in plans:
        if args.skip_existing and (args.data_root / "zeek" / run_id / "conn.log").exists():
            print(f"skipping existing Zeek run: {run_id}")
            continue

        if plan.scan_type is None:
            collect_baseline(args, run_id)
        else:
            collect_attack(args, run_id, plan)
        convert_pcap_to_zeek(args, run_id)

    rebuild_dataset(args, collected_runs, test_run_ids)
    summary_path = write_collection_summary(args, collected_runs)
    print("bulk collection plan complete")
    print("runs:", ", ".join(run_id for run_id, _scenario_id in collected_runs))
    print("test runs:", ", ".join(test_run_ids))
    print(f"summary: {summary_path}")


def build_plans(args: argparse.Namespace) -> list[tuple[str, ScenarioPlan, int]]:
    selected = set(args.scenario)
    baseline = ScenarioPlan(
        scenario_id="baseline",
        scan_type=None,
        targets="",
        ports="",
        repeats=args.baseline_repeats,
    )
    attack_plans = []
    for plan in DEFAULT_ATTACK_PLANS:
        repeats = args.attack_repeats if args.attack_repeats is not None else plan.repeats
        attack_plans.append(
            ScenarioPlan(
                scenario_id=plan.scenario_id,
                scan_type=plan.scan_type,
                targets=plan.targets,
                ports=plan.ports,
                repeats=repeats,
            )
        )

    result = []
    for plan in [baseline, *attack_plans]:
        if selected and plan.scenario_id not in selected:
            continue
        for repeat_index in range(1, plan.repeats + 1):
            run_id = f"{plan.scenario_id}-{repeat_index:03d}"
            if args.run_prefix:
                run_id = f"{args.run_prefix}-{run_id}"
            result.append((run_id, plan, repeat_index))
    return result


def split_runs(
    plans: list[tuple[str, ScenarioPlan, int]],
    test_repeats: set[int],
) -> tuple[list[tuple[str, str]], list[str]]:
    collected_runs = []
    test_run_ids = []
    for run_id, plan, repeat_index in plans:
        collected_runs.append((run_id, plan.scenario_id))
        if repeat_index in test_repeats:
            test_run_ids.append(run_id)
    return collected_runs, test_run_ids


def estimate_collection_plan(
    args: argparse.Namespace,
    plans: list[tuple[str, ScenarioPlan, int]],
    test_repeats: set[int],
) -> dict[str, object]:
    run_estimates = []
    scenario_totals: dict[str, dict[str, int | float]] = {}
    split_totals = {
        "train": {"estimated_windows": 0, "estimated_seconds": 0.0, "run_count": 0},
        "test": {"estimated_windows": 0, "estimated_seconds": 0.0, "run_count": 0},
    }
    for run_id, plan, repeat_index in plans:
        duration = estimated_run_duration_seconds(args, plan)
        windows = max(1, int(duration // args.window_seconds))
        split = "test" if repeat_index in test_repeats else "train"
        run_record = {
            "run_id": run_id,
            "scenario_id": plan.scenario_id,
            "scan_type": plan.scan_type,
            "repeat_index": repeat_index,
            "split": split,
            "estimated_seconds": round(duration, 3),
            "estimated_windows": windows,
            "targets": parse_csv(plan.targets) if plan.targets else [],
            "ports": parse_csv(plan.ports) if plan.ports else [],
            "uses_long_capture": uses_long_capture(args, plan),
        }
        run_estimates.append(run_record)
        scenario = scenario_totals.setdefault(
            plan.scenario_id,
            {"run_count": 0, "estimated_windows": 0, "estimated_seconds": 0.0},
        )
        scenario["run_count"] = int(scenario["run_count"]) + 1
        scenario["estimated_windows"] = int(scenario["estimated_windows"]) + windows
        scenario["estimated_seconds"] = float(scenario["estimated_seconds"]) + duration
        split_totals[split]["estimated_windows"] += windows
        split_totals[split]["estimated_seconds"] += duration
        split_totals[split]["run_count"] += 1

    train_windows = int(split_totals["train"]["estimated_windows"])
    test_windows = int(split_totals["test"]["estimated_windows"])
    total_seconds = sum(float(record["estimated_seconds"]) for record in run_estimates)
    return {
        "parameters": {
            "run_prefix": args.run_prefix,
            "window_seconds": args.window_seconds,
            "baseline_repeats": args.baseline_repeats,
            "baseline_seconds": args.baseline_seconds,
            "attack_repeats": args.attack_repeats,
            "attack_duration_seconds": args.attack_duration_seconds,
            "attack_interval_seconds": args.attack_interval_seconds,
            "attack_max_scan_repeats": args.attack_max_scan_repeats,
            "capture_network_mode": args.capture_network_mode,
            "capture_interface": args.capture_interface,
            "capture_filter": args.capture_filter,
            "long_capture_scenario": args.long_capture_scenario,
            "scenario": args.scenario,
            "test_repeat": sorted(test_repeats),
            "dataset_output": str(args.dataset_output),
        },
        "total_runs": len(run_estimates),
        "estimated_sequential_seconds": round(total_seconds, 3),
        "estimated_sequential_hours": round(total_seconds / 3600.0, 3),
        "split_estimates": split_totals,
        "scenario_estimates": scenario_totals,
        "run_estimates": run_estimates,
        "volume_requirements": {
            "min_train_windows": args.min_train_windows,
            "min_test_windows": args.min_test_windows,
        },
        "volume_gate_pass": train_windows >= args.min_train_windows
        and test_windows >= args.min_test_windows,
        "notes": [
            "This is a planning estimate, not evidence of completed collection.",
            "Actual windows can be lower if a pcap has no Zeek conn rows for a window.",
            "Use the generated train/test CSVs and diagnose_data_sufficiency.py after collection.",
            "Do not treat smoke or pilot plans as operational readiness evidence.",
        ],
    }


def estimated_run_duration_seconds(args: argparse.Namespace, plan: ScenarioPlan) -> float:
    if plan.scan_type is None:
        return float(args.baseline_seconds)

    long_capture_duration = float(args.attack_duration_seconds) if uses_long_capture(args, plan) else 0.0
    if plan.scan_type == "low-and-slow":
        delayed_attempts = max(1, len(parse_csv(plan.targets)) * len(parse_csv(plan.ports)))
        delayed_scan_seconds = float(delayed_attempts * 60)
        return max(long_capture_duration, delayed_scan_seconds)

    if long_capture_duration > 0:
        return long_capture_duration
    return float(args.window_seconds)


def uses_long_capture(args: argparse.Namespace, plan: ScenarioPlan) -> bool:
    if plan.scan_type is None or args.attack_duration_seconds <= 0:
        return False
    long_capture_scenarios = set(args.long_capture_scenario)
    return not long_capture_scenarios or plan.scenario_id in long_capture_scenarios


def write_plan_outputs(args: argparse.Namespace, report: dict[str, object]) -> None:
    args.plan_output_json.parent.mkdir(parents=True, exist_ok=True)
    args.plan_output_md.parent.mkdir(parents=True, exist_ok=True)
    with args.plan_output_json.open("w", encoding="utf-8") as file:
        json.dump(report, file, indent=2, sort_keys=True)
        file.write("\n")
    args.plan_output_md.write_text(render_plan_markdown(report), encoding="utf-8")


def render_plan_markdown(report: dict[str, object]) -> str:
    split = report["split_estimates"]
    requirements = report["volume_requirements"]
    lines = [
        "# Bulk Collection Plan Estimate",
        "",
        f"- Total runs: {report['total_runs']}",
        f"- Estimated sequential hours: {report['estimated_sequential_hours']}",
        f"- Volume gate pass: {report['volume_gate_pass']}",
        f"- Required train windows: {requirements['min_train_windows']}",
        f"- Required test windows: {requirements['min_test_windows']}",
        "",
        "## Split Estimates",
        "",
        "| split | runs | estimated_windows | estimated_hours |",
        "| --- | --- | --- | --- |",
    ]
    for name in ("train", "test"):
        payload = split[name]
        lines.append(
            "| "
            + " | ".join(
                [
                    name,
                    str(payload["run_count"]),
                    str(payload["estimated_windows"]),
                    f"{float(payload['estimated_seconds']) / 3600.0:.3f}",
                ]
            )
            + " |"
        )

    lines.extend(["", "## Scenario Estimates", "", "| scenario | runs | estimated_windows | estimated_hours |", "| --- | --- | --- | --- |"])
    for scenario_id, payload in sorted(report["scenario_estimates"].items()):
        lines.append(
            "| "
            + " | ".join(
                [
                    scenario_id,
                    str(payload["run_count"]),
                    str(payload["estimated_windows"]),
                    f"{float(payload['estimated_seconds']) / 3600.0:.3f}",
                ]
            )
            + " |"
        )

    lines.extend(["", "## Notes", ""])
    for note in report["notes"]:
        lines.append(f"- {note}")
    lines.append("")
    return "\n".join(lines)


def parse_csv(raw_value: str) -> list[str]:
    return [value.strip() for value in str(raw_value or "").split(",") if value.strip()]


def collect_baseline(args: argparse.Namespace, run_id: str) -> None:
    container_id = compose_container_id(args.compose_service, dry_run=args.dry_run)
    container_ip = compose_container_ip(args.compose_service, dry_run=args.dry_run)
    pcap_root = (args.data_root / "pcap").resolve()
    pcap_root.mkdir(parents=True, exist_ok=True)
    capture_name = safe_container_name(f"ipcam-baseline-tcpdump-{run_id}")
    network = f"container:{container_id}" if container_id else "container:DRY_RUN"
    capture_filter = args.capture_filter
    if args.capture_network_mode == "host":
        network = "host"
        if container_ip:
            capture_filter = f"({capture_filter}) and host {container_ip}"

    command = [
        "docker",
        "run",
        "--rm",
        "--name",
        capture_name,
        "--network",
        network,
        "-v",
        f"{pcap_root}:/pcap",
        DEFAULT_TCPDUMP_IMAGE,
        "tcpdump",
        "-i",
        args.capture_interface,
        "-nn",
        "-s",
        "0",
        "-U",
        "-w",
        f"/pcap/{run_id}.pcap",
        capture_filter,
    ]
    print("baseline tcpdump:", " ".join(command))
    if args.dry_run:
        return

    cleanup_container(capture_name)
    process = subprocess.Popen(command)
    try:
        time.sleep(args.baseline_seconds)
    finally:
        subprocess.run(["docker", "stop", capture_name], check=False, capture_output=True, text=True)
        try:
            process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            process.kill()


def collect_attack(args: argparse.Namespace, run_id: str, plan: ScenarioPlan) -> None:
    command = [
        sys.executable,
        "scripts/collect_nmap_run.py",
        "--run-id",
        run_id,
        "--scenario-id",
        plan.scenario_id,
        "--scan-type",
        str(plan.scan_type),
        "--targets",
        plan.targets,
        "--ports",
        plan.ports,
        "--compose-service",
        args.compose_service,
        "--scenario-log-root",
        str(args.scenario_log_root),
        "--capture-network-mode",
        args.capture_network_mode,
        "--capture-interface",
        args.capture_interface,
        "--capture-filter",
        args.capture_filter,
        "--skip-pull",
    ]
    long_capture_scenarios = set(args.long_capture_scenario)
    uses_long_capture = args.attack_duration_seconds > 0 and (
        not long_capture_scenarios or plan.scenario_id in long_capture_scenarios
    )
    if uses_long_capture:
        command.extend(["--repeat-until-seconds", str(args.attack_duration_seconds)])
        command.extend(["--repeat-interval-seconds", str(args.attack_interval_seconds)])
        if args.attack_max_scan_repeats:
            command.extend(["--max-scan-repeats", str(args.attack_max_scan_repeats)])
    run(command, dry_run=args.dry_run)


def convert_pcap_to_zeek(args: argparse.Namespace, run_id: str) -> None:
    run(
        [
            sys.executable,
            "scripts/run_zeek_pcap.py",
            "--run-id",
            run_id,
            "--data-root",
            str(args.data_root),
            "--zeek-image",
            "zeek/zeek:latest",
        ],
        dry_run=args.dry_run,
    )


def rebuild_dataset(
    args: argparse.Namespace,
    runs: list[tuple[str, str]],
    test_run_ids: list[str],
) -> None:
    command = [
        sys.executable,
        "scripts/build_scan_dataset.py",
        "--zeek-root",
        str(args.data_root / "zeek"),
        "--features-root",
        str(args.data_root / "features/windowed"),
        "--ground-truth",
        str(args.scenario_log_root / "ground-truth.jsonl"),
        "--target-column",
        "scan_subtype",
        "--output",
        str(args.dataset_output),
        "--window-seconds",
        str(args.window_seconds),
    ]
    for run_id, scenario_id in runs:
        default_label = "normal"
        command.extend(["--run", f"{run_id}:{scenario_id}:{default_label}"])
    for run_id in test_run_ids:
        command.extend(["--test-run", run_id])
    run(command, dry_run=args.dry_run)


def write_collection_summary(
    args: argparse.Namespace,
    runs: list[tuple[str, str]],
) -> Path:
    summary_path = args.summary_output or default_summary_output(args)
    dataset_rows, label_counts, subtype_counts, run_counts = summarize_dataset(args.dataset_output)
    payload = {
        "dataset": str(args.dataset_output),
        "rows": dataset_rows,
        "label_counts": dict(sorted(label_counts.items())),
        "scan_subtype_counts": dict(sorted(subtype_counts.items())),
        "run_counts": dict(sorted(run_counts.items())),
        "capture_network_mode": args.capture_network_mode,
        "capture_filter": args.capture_filter,
        "window_seconds": args.window_seconds,
        "purpose": (
            "Docker/Zeek/feature-build integration verification or bulk collection "
            "summary. Treat small pilot outputs as non-performance evidence."
        ),
        "outputs": {
            "ground_truth": str(args.scenario_log_root / "ground-truth.jsonl"),
            "pcap": [str(args.data_root / "pcap" / f"{run_id}.pcap") for run_id, _ in runs],
            "zeek_conn": [
                str(args.data_root / "zeek" / run_id / "conn.log") for run_id, _ in runs
            ],
            "windowed": [
                str(args.data_root / "features/windowed" / f"{run_id}-{args.window_seconds}s.csv")
                for run_id, _ in runs
            ],
        },
    }
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return summary_path


def default_summary_output(args: argparse.Namespace) -> Path:
    if args.run_prefix:
        return args.dataset_output.with_name(f"{args.run_prefix}-collection-summary.json")
    return args.dataset_output.with_name(f"{args.dataset_output.stem}-collection-summary.json")


def summarize_dataset(path: Path) -> tuple[int, Counter[str], Counter[str], Counter[str]]:
    label_counts: Counter[str] = Counter()
    subtype_counts: Counter[str] = Counter()
    run_counts: Counter[str] = Counter()
    rows = 0
    if not path.exists():
        return rows, label_counts, subtype_counts, run_counts

    with path.open("r", encoding="utf-8", newline="") as file:
        reader = csv.DictReader(file)
        for row in reader:
            rows += 1
            label_counts[str(row.get("label") or "")] += 1
            subtype_counts[str(row.get("scan_subtype") or "")] += 1
            run_counts[str(row.get("run_id") or "")] += 1
    return rows, label_counts, subtype_counts, run_counts


def compose_container_id(service: str, *, dry_run: bool) -> str:
    if dry_run:
        return ""
    result = subprocess.run(
        ["docker", "compose", "ps", "-q", service],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise SystemExit(result.stderr.strip() or "docker compose ps failed")
    container_id = result.stdout.strip()
    if not container_id:
        raise SystemExit(f"missing compose service container: {service}")
    return container_id


def compose_container_ip(service: str, *, dry_run: bool) -> str:
    if dry_run:
        return ""
    container_id = compose_container_id(service, dry_run=False)
    result = subprocess.run(
        [
            "docker",
            "inspect",
            "-f",
            "{{range .NetworkSettings.Networks}}{{println .IPAddress}}{{end}}",
            container_id,
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise SystemExit(result.stderr.strip() or f"docker inspect failed for {service}")
    return next((line.strip() for line in result.stdout.splitlines() if line.strip()), "")


def cleanup_container(name: str) -> None:
    subprocess.run(["docker", "rm", "-f", name], check=False, capture_output=True, text=True)


def safe_container_name(value: str) -> str:
    return "".join(char if char.isalnum() or char in {"-", "_"} else "-" for char in value)


def run(command: list[str], *, dry_run: bool) -> None:
    print(" ".join(command))
    if dry_run:
        return
    subprocess.run(command, check=True)


if __name__ == "__main__":
    main()
