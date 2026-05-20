from __future__ import annotations

import argparse
import csv
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
import subprocess
import sys


DEFAULT_KEEP_LABELS = ("normal", "scanning")
DEFAULT_LABEL_MAP = {"attack": "scanning"}


@dataclass(frozen=True, slots=True)
class RunSpec:
    run_id: str
    scenario_id: str
    default_label: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Build 60-second window features for multiple Zeek runs, merge them, "
            "and optionally create run-based train/test CSVs."
        )
    )
    parser.add_argument("--zeek-root", type=Path, default=Path("data/zeek"))
    parser.add_argument("--features-root", type=Path, default=Path("data/features/windowed"))
    parser.add_argument("--ground-truth", type=Path, default=Path("data/scenarios/ground-truth.jsonl"))
    parser.add_argument("--output", type=Path, default=Path("data/features/datasets/ipcam-scan-dataset-60s.csv"))
    parser.add_argument("--window-seconds", type=int, default=60)
    parser.add_argument(
        "--run",
        action="append",
        default=[],
        help=(
            "Run to include. Format: run_id[:scenario_id[:default_label]]. "
            "When omitted, every data/zeek/<run_id>/conn.log is included."
        ),
    )
    parser.add_argument(
        "--test-run",
        action="append",
        default=[],
        help="Run id to place in the optional test split. Can be repeated.",
    )
    parser.add_argument("--train-output", type=Path, default=None)
    parser.add_argument("--test-output", type=Path, default=None)
    parser.add_argument(
        "--keep-label",
        action="append",
        default=[],
        help="Label to keep after label mapping. Defaults to normal and scanning.",
    )
    parser.add_argument(
        "--label-map",
        action="append",
        default=[],
        help="Map labels while merging. Format: old=new. Defaults to attack=scanning.",
    )
    parser.add_argument(
        "--no-default-label-map",
        action="store_true",
        help="Disable the default attack=scanning mapping.",
    )
    parser.add_argument(
        "--skip-feature-build",
        action="store_true",
        help="Merge existing windowed CSVs without regenerating them.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    run_specs = parse_run_specs(args.run, zeek_root=args.zeek_root)
    if not run_specs:
        raise SystemExit(f"no runs found under {args.zeek_root}")

    if not args.skip_feature_build:
        for run_spec in run_specs:
            build_run_features(args, run_spec)

    label_map = parse_label_map(args.label_map, use_default=not args.no_default_label_map)
    keep_labels = set(args.keep_label or DEFAULT_KEEP_LABELS)
    rows, fieldnames = merge_rows(
        run_specs=run_specs,
        features_root=args.features_root,
        label_map=label_map,
        keep_labels=keep_labels,
    )

    write_csv(args.output, rows, fieldnames)
    print_summary("merged", rows)
    print(f"wrote {len(rows)} rows to {args.output}")

    if args.test_run:
        test_runs = set(args.test_run)
        train_rows = [row for row in rows if row.get("run_id") not in test_runs]
        test_rows = [row for row in rows if row.get("run_id") in test_runs]
        train_output = args.train_output or args.output.with_name(f"{args.output.stem}-train.csv")
        test_output = args.test_output or args.output.with_name(f"{args.output.stem}-test.csv")
        write_csv(train_output, train_rows, fieldnames)
        write_csv(test_output, test_rows, fieldnames)
        print_summary("train", train_rows)
        print_summary("test", test_rows)
        print(f"wrote {len(train_rows)} train rows to {train_output}")
        print(f"wrote {len(test_rows)} test rows to {test_output}")


def parse_run_specs(raw_specs: list[str], *, zeek_root: Path) -> list[RunSpec]:
    if not raw_specs:
        raw_specs = [
            path.parent.name
            for path in sorted(zeek_root.glob("*/conn.log"))
            if path.is_file()
        ]

    seen: set[str] = set()
    specs: list[RunSpec] = []
    for raw_spec in raw_specs:
        parts = raw_spec.split(":")
        run_id = parts[0].strip()
        if not run_id or run_id in seen:
            continue
        seen.add(run_id)

        scenario_id = parts[1].strip() if len(parts) > 1 and parts[1].strip() else infer_scenario_id(run_id)
        default_label = parts[2].strip() if len(parts) > 2 and parts[2].strip() else infer_default_label(run_id)
        specs.append(RunSpec(run_id=run_id, scenario_id=scenario_id, default_label=default_label))

    return specs


def build_run_features(args: argparse.Namespace, run_spec: RunSpec) -> None:
    conn_log = args.zeek_root / run_spec.run_id / "conn.log"
    output = args.features_root / f"{run_spec.run_id}-{args.window_seconds}s.csv"
    if not conn_log.exists():
        raise SystemExit(f"missing Zeek conn.log for {run_spec.run_id}: {conn_log}")

    command = [
        sys.executable,
        str(Path(__file__).with_name("build_window_features.py")),
        "--conn-log",
        str(conn_log),
        "--output",
        str(output),
        "--scenario-id",
        run_spec.scenario_id,
        "--run-id",
        run_spec.run_id,
        "--window-seconds",
        str(args.window_seconds),
        "--default-label",
        run_spec.default_label,
    ]
    if args.ground_truth.exists():
        command.extend(["--ground-truth", str(args.ground_truth)])

    subprocess.run(command, check=True)


def merge_rows(
    *,
    run_specs: list[RunSpec],
    features_root: Path,
    label_map: dict[str, str],
    keep_labels: set[str],
) -> tuple[list[dict[str, str]], list[str]]:
    rows: list[dict[str, str]] = []
    fieldnames: list[str] = []

    for feature_path in feature_paths(run_specs, features_root):
        with feature_path.open("r", encoding="utf-8", newline="") as file:
            reader = csv.DictReader(file)
            if reader.fieldnames is None:
                continue
            if not fieldnames:
                fieldnames = list(reader.fieldnames)

            for row in reader:
                label = normalize_label(row.get("label", ""), label_map=label_map)
                if keep_labels and label not in keep_labels:
                    continue
                row["label"] = label
                rows.append(row)

    return rows, fieldnames


def feature_paths(run_specs: list[RunSpec], features_root: Path) -> list[Path]:
    paths: list[Path] = []
    for run_spec in run_specs:
        matches = sorted(features_root.glob(f"{run_spec.run_id}-*s.csv"))
        if not matches:
            matches = [features_root / f"{run_spec.run_id}.csv"]
        for path in matches:
            if path.exists():
                paths.append(path)
                break
        else:
            raise SystemExit(f"missing window feature CSV for {run_spec.run_id} under {features_root}")
    return paths


def parse_label_map(raw_maps: list[str], *, use_default: bool) -> dict[str, str]:
    label_map = dict(DEFAULT_LABEL_MAP if use_default else {})
    for raw_map in raw_maps:
        if "=" not in raw_map:
            raise SystemExit(f"invalid --label-map value: {raw_map}")
        source, target = raw_map.split("=", 1)
        source = source.strip()
        target = target.strip()
        if not source or not target:
            raise SystemExit(f"invalid --label-map value: {raw_map}")
        label_map[source] = target
    return label_map


def normalize_label(label: str, *, label_map: dict[str, str]) -> str:
    value = str(label or "").strip()
    return label_map.get(value, value)


def write_csv(path: Path, rows: list[dict[str, str]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def print_summary(name: str, rows: list[dict[str, str]]) -> None:
    label_counts = Counter(row.get("label", "") for row in rows)
    run_counts = Counter(row.get("run_id", "") for row in rows)
    print(f"{name} labels: {dict(sorted(label_counts.items()))}")
    print(f"{name} runs: {dict(sorted(run_counts.items()))}")


def infer_scenario_id(run_id: str) -> str:
    if run_id.startswith("baseline"):
        return "baseline"
    if run_id.startswith("infected-scan"):
        return "infected-scan"
    if "-" in run_id:
        return run_id.rsplit("-", 1)[0]
    return "unknown"


def infer_default_label(run_id: str) -> str:
    return "normal"


if __name__ == "__main__":
    main()
