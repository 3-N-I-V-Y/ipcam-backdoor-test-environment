from __future__ import annotations

import argparse
import csv
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
from statistics import median
from typing import Any


RAW_IDENTITY_COLUMNS = {
    "src_ip",
    "dst_ip",
    "id.orig_h",
    "id.resp_h",
    "raw_src_ip",
    "raw_dst_ip",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Report sequence volume, label balance, leakage checks, and NPZ shapes.",
    )
    parser.add_argument("--dataset", required=True, type=Path)
    parser.add_argument("--train", required=True, type=Path)
    parser.add_argument("--test", required=True, type=Path)
    parser.add_argument("--sequence-length", action="append", type=int, required=True)
    parser.add_argument("--sequence-metadata", action="append", type=Path, default=[])
    parser.add_argument("--sequence-npz", action="append", type=Path, default=[])
    parser.add_argument("--output-json", required=True, type=Path)
    parser.add_argument("--output-md", required=True, type=Path)
    parser.add_argument("--target-column", default="scan_subtype")
    parser.add_argument("--group-columns", default="run_id,src_entity")
    parser.add_argument("--stride", type=int, default=1)
    parser.add_argument("--pad-start", action="store_true")
    parser.add_argument("--min-total-sequences", type=int, default=10_000)
    parser.add_argument("--min-train-sequences", type=int, default=7_000)
    parser.add_argument("--min-test-sequences", type=int, default=2_000)
    parser.add_argument("--max-run-share", type=float, default=0.25)
    parser.add_argument("--max-scenario-share", type=float, default=0.5)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    group_columns = [column.strip() for column in args.group_columns.split(",") if column.strip()]
    dataset_rows, dataset_fields = read_csv(args.dataset)
    train_rows, train_fields = read_csv(args.train)
    test_rows, test_fields = read_csv(args.test)

    sequence_lengths = sorted(set(args.sequence_length))
    payload = {
        "generated_at": now_utc(),
        "inputs": {
            "dataset": str(args.dataset),
            "train": str(args.train),
            "test": str(args.test),
            "sequence_metadata": [str(path) for path in args.sequence_metadata],
            "sequence_npz": [str(path) for path in args.sequence_npz],
        },
        "parameters": {
            "target_column": args.target_column,
            "group_columns": group_columns,
            "sequence_lengths": sequence_lengths,
            "stride": args.stride,
            "pad_start": args.pad_start,
            "min_total_sequences": args.min_total_sequences,
            "min_train_sequences": args.min_train_sequences,
            "min_test_sequences": args.min_test_sequences,
            "max_run_share": args.max_run_share,
            "max_scenario_share": args.max_scenario_share,
        },
        "dataset": summarize_rows(dataset_rows, dataset_fields, target_column=args.target_column),
        "splits": {
            "train": summarize_rows(train_rows, train_fields, target_column=args.target_column),
            "test": summarize_rows(test_rows, test_fields, target_column=args.target_column),
        },
        "leakage_checks": leakage_checks(train_rows, test_rows),
        "sequence_estimates": {
            "all": estimate_by_length(
                dataset_rows,
                sequence_lengths,
                group_columns=group_columns,
                target_column=args.target_column,
                stride=args.stride,
                pad_start=args.pad_start,
            ),
            "train": estimate_by_length(
                train_rows,
                sequence_lengths,
                group_columns=group_columns,
                target_column=args.target_column,
                stride=args.stride,
                pad_start=args.pad_start,
            ),
            "test": estimate_by_length(
                test_rows,
                sequence_lengths,
                group_columns=group_columns,
                target_column=args.target_column,
                stride=args.stride,
                pad_start=args.pad_start,
            ),
        },
        "sequence_artifacts": {
            "metadata": [read_metadata(path) for path in args.sequence_metadata],
            "npz": [read_npz_summary(path) for path in args.sequence_npz],
        },
    }
    payload["gates"] = gates(payload, args)
    write_json(args.output_json, payload)
    args.output_md.parent.mkdir(parents=True, exist_ok=True)
    args.output_md.write_text(render_markdown(payload), encoding="utf-8")
    print(f"wrote sequence report json to {args.output_json}")
    print(f"wrote sequence report md to {args.output_md}")


def read_csv(path: Path) -> tuple[list[dict[str, str]], list[str]]:
    with path.open("r", encoding="utf-8", newline="") as file:
        reader = csv.DictReader(file)
        return list(reader), list(reader.fieldnames or [])


def summarize_rows(rows: list[dict[str, str]], fields: list[str], *, target_column: str) -> dict[str, Any]:
    labels = Counter(clean(row.get("label")) for row in rows)
    targets = Counter(clean(row.get(target_column)) for row in rows)
    scenarios = Counter(clean(row.get("scenario_id")) for row in rows)
    runs = Counter(clean(row.get("run_id")) for row in rows)
    sources = Counter(clean(row.get("data_source")) for row in rows if clean(row.get("data_source")))
    return {
        "rows": len(rows),
        "field_count": len(fields),
        "raw_identity_columns_present": sorted(set(fields) & RAW_IDENTITY_COLUMNS),
        "label_counts": dict(sorted(labels.items())),
        "label_ratios": ratios(labels),
        "target_counts": dict(sorted(targets.items())),
        "scenario_counts": dict(sorted(scenarios.items())),
        "run_count": len(runs),
        "top_run": top_share(runs),
        "top_scenario": top_share(scenarios),
        "data_source_counts": dict(sorted(sources.items())),
    }


def leakage_checks(train_rows: list[dict[str, str]], test_rows: list[dict[str, str]]) -> dict[str, Any]:
    train_runs = {clean(row.get("run_id")) for row in train_rows if clean(row.get("run_id"))}
    test_runs = {clean(row.get("run_id")) for row in test_rows if clean(row.get("run_id"))}
    train_scenarios = {clean(row.get("scenario_id")) for row in train_rows if clean(row.get("scenario_id"))}
    test_scenarios = {clean(row.get("scenario_id")) for row in test_rows if clean(row.get("scenario_id"))}
    return {
        "run_id_overlap": sorted(train_runs & test_runs),
        "scenario_id_overlap": sorted(train_scenarios & test_scenarios),
        "has_run_id_leakage": bool(train_runs & test_runs),
    }


def estimate_by_length(
    rows: list[dict[str, str]],
    lengths: list[int],
    *,
    group_columns: list[str],
    target_column: str,
    stride: int,
    pad_start: bool,
) -> dict[str, Any]:
    groups: dict[tuple[str, ...], list[dict[str, str]]] = {}
    for row in rows:
        key = tuple(clean(row.get(column)) for column in group_columns)
        groups.setdefault(key, []).append(row)

    group_lengths = [len(group_rows) for group_rows in groups.values()]
    estimates: dict[str, Any] = {
        "groups": len(groups),
        "group_length": describe_numbers(group_lengths),
        "lengths": {},
    }
    for length in lengths:
        label_counts: Counter[str] = Counter()
        scenario_counts: Counter[str] = Counter()
        run_counts: Counter[str] = Counter()
        total = 0
        eligible_groups = 0
        for group_rows in groups.values():
            ordered = sorted(group_rows, key=lambda row: timestamp_key(row.get("window_start")))
            if len(ordered) >= length or (pad_start and ordered):
                eligible_groups += 1
            start_index = 0 if pad_start else length - 1
            for end_index in range(start_index, len(ordered), max(stride, 1)):
                if not pad_start and end_index < length - 1:
                    continue
                total += 1
                end_row = ordered[end_index]
                label_counts[clean(end_row.get(target_column))] += 1
                scenario_counts[clean(end_row.get("scenario_id"))] += 1
                run_counts[clean(end_row.get("run_id"))] += 1
        estimates["lengths"][str(length)] = {
            "sequences": total,
            "eligible_groups": eligible_groups,
            "target_counts": dict(sorted(label_counts.items())),
            "target_ratios": ratios(label_counts),
            "top_run": top_share(run_counts),
            "top_scenario": top_share(scenario_counts),
        }
    return estimates


def read_metadata(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"path": str(path), "exists": False}
    with path.open("r", encoding="utf-8") as file:
        payload = json.load(file)
    feature_columns = [str(column) for column in payload.get("feature_columns", [])]
    return {
        "path": str(path),
        "exists": True,
        "sequence_length": payload.get("sequence_length"),
        "stride": payload.get("stride"),
        "train_sequences": payload.get("train_sequences"),
        "test_sequences": payload.get("test_sequences"),
        "classes": payload.get("classes"),
        "feature_count": len(feature_columns),
        "raw_identity_features": sorted(set(feature_columns) & RAW_IDENTITY_COLUMNS),
    }


def read_npz_summary(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"path": str(path), "exists": False}
    try:
        import numpy as np
    except ImportError:
        return {"path": str(path), "exists": True, "error": "numpy is not installed"}

    with np.load(path, allow_pickle=False) as data:
        x_shape = list(data["X"].shape) if "X" in data else []
        y = data["y"].tolist() if "y" in data else []
        classes = [str(value) for value in data["classes"].tolist()] if "classes" in data else []
    label_counts = Counter(int(label) for label in y)
    return {
        "path": str(path),
        "exists": True,
        "x_shape": x_shape,
        "sequences": int(x_shape[0]) if x_shape else 0,
        "classes": classes,
        "label_counts": {
            classes[index] if index < len(classes) else str(index): count
            for index, count in sorted(label_counts.items())
        },
    }


def gates(payload: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    train_lengths = payload["sequence_estimates"]["train"]["lengths"]
    test_lengths = payload["sequence_estimates"]["test"]["lengths"]
    all_lengths = payload["sequence_estimates"]["all"]["lengths"]
    result: dict[str, Any] = {
        "no_run_id_overlap": not payload["leakage_checks"]["has_run_id_leakage"],
        "no_raw_identity_columns": not payload["dataset"]["raw_identity_columns_present"],
        "no_raw_identity_features": True,
        "lengths": {},
    }
    for metadata in payload["sequence_artifacts"]["metadata"]:
        if metadata.get("raw_identity_features"):
            result["no_raw_identity_features"] = False
    for length in args.sequence_length:
        key = str(length)
        train_record = train_lengths.get(key, {})
        test_record = test_lengths.get(key, {})
        all_record = all_lengths.get(key, {})
        train_sequences = int(train_record.get("sequences", 0))
        test_sequences = int(test_record.get("sequences", 0))
        total_sequences = int(all_record.get("sequences", 0))
        train_run_share = float(train_record.get("top_run", {}).get("share", 0.0))
        train_scenario_share = float(train_record.get("top_scenario", {}).get("share", 0.0))
        result["lengths"][key] = {
            "total_sequence_gate": total_sequences >= args.min_total_sequences,
            "train_sequence_gate": train_sequences >= args.min_train_sequences,
            "test_sequence_gate": test_sequences >= args.min_test_sequences,
            "train_run_dominance_gate": train_run_share <= args.max_run_share,
            "train_scenario_dominance_gate": train_scenario_share <= args.max_scenario_share,
            "total_sequences": total_sequences,
            "train_sequences": train_sequences,
            "test_sequences": test_sequences,
            "train_top_run_share": train_run_share,
            "train_top_scenario_share": train_scenario_share,
        }
    result["overall_pass"] = bool(
        result["no_run_id_overlap"]
        and result["no_raw_identity_columns"]
        and result["no_raw_identity_features"]
        and all(
            length_result["total_sequence_gate"]
            and length_result["train_sequence_gate"]
            and length_result["test_sequence_gate"]
            and length_result["train_run_dominance_gate"]
            and length_result["train_scenario_dominance_gate"]
            for length_result in result["lengths"].values()
        )
    )
    return result


def render_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Sequence Collection Report",
        "",
        f"- Generated at: {payload['generated_at']}",
        f"- Dataset rows: {payload['dataset']['rows']}",
        f"- Train rows: {payload['splits']['train']['rows']}",
        f"- Test rows: {payload['splits']['test']['rows']}",
        f"- Overall pass: {payload['gates']['overall_pass']}",
        "",
        "## Leakage And Identity Checks",
        "",
        f"- Run ID overlap: {payload['leakage_checks']['run_id_overlap']}",
        f"- Scenario ID overlap: {payload['leakage_checks']['scenario_id_overlap']}",
        f"- Raw identity columns: {payload['dataset']['raw_identity_columns_present']}",
        f"- Raw identity feature gate: {payload['gates']['no_raw_identity_features']}",
        "",
        "## Split Label Counts",
        "",
        "| split | rows | label_counts | target_counts |",
        "| --- | ---: | --- | --- |",
    ]
    for split_name in ("train", "test"):
        split = payload["splits"][split_name]
        lines.append(
            f"| {split_name} | {split['rows']} | `{json.dumps(split['label_counts'], sort_keys=True)}` | "
            f"`{json.dumps(split['target_counts'], sort_keys=True)}` |"
        )

    lines.extend(["", "## Sequence Estimates", "", "| split | length | sequences | eligible_groups | target_counts |", "| --- | ---: | ---: | ---: | --- |"])
    for split_name in ("all", "train", "test"):
        lengths = payload["sequence_estimates"][split_name]["lengths"]
        for length, record in sorted(lengths.items(), key=lambda item: int(item[0])):
            lines.append(
                f"| {split_name} | {length} | {record['sequences']} | {record['eligible_groups']} | "
                f"`{json.dumps(record['target_counts'], sort_keys=True)}` |"
            )

    lines.extend(["", "## Artifact Shapes", "", "| path | exists | sequences | shape |", "| --- | --- | ---: | --- |"])
    for artifact in payload["sequence_artifacts"]["npz"]:
        lines.append(
            f"| `{artifact['path']}` | {artifact.get('exists')} | {artifact.get('sequences', '')} | "
            f"`{artifact.get('x_shape', '')}` |"
        )

    lines.extend(["", "## Gates", "", "```json", json.dumps(payload["gates"], indent=2, sort_keys=True), "```", ""])
    return "\n".join(lines)


def describe_numbers(values: list[int]) -> dict[str, Any]:
    if not values:
        return {"min": 0, "median": 0, "max": 0}
    return {"min": min(values), "median": median(values), "max": max(values)}


def top_share(counter: Counter[str]) -> dict[str, Any]:
    total = sum(counter.values())
    if total == 0:
        return {"value": "", "count": 0, "share": 0.0}
    value, count = counter.most_common(1)[0]
    return {"value": value, "count": count, "share": round(count / total, 6)}


def ratios(counter: Counter[str]) -> dict[str, float]:
    total = sum(counter.values())
    return {key: round(value / total, 6) for key, value in sorted(counter.items())} if total else {}


def clean(value: Any) -> str:
    return str(value or "").strip()


def timestamp_key(value: Any) -> float:
    raw = clean(value)
    if not raw:
        return 0.0
    try:
        return float(raw)
    except ValueError:
        pass
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
