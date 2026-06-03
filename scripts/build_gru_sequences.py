from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any


EXCLUDE_COLUMNS = {
    "window_start",
    "window_end",
    "src_entity",
    "scenario_id",
    "run_id",
    "label",
    "scan_subtype",
    "phases",
    "technique_ids",
    "phase",
    "technique_id",
    "is_synthetic",
    "data_source",
    "src_ip",
    "dst_ip",
    "id.orig_h",
    "id.resp_h",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build GRU-ready sliding sequences from src_ip window feature CSVs.",
    )
    parser.add_argument("--train-input", required=True, type=Path)
    parser.add_argument("--test-input", required=True, type=Path)
    parser.add_argument(
        "--train-output",
        type=Path,
        default=Path("data/features/sequences/scan-subtype-60s-n5-train.npz"),
    )
    parser.add_argument(
        "--test-output",
        type=Path,
        default=Path("data/features/sequences/scan-subtype-60s-n5-test.npz"),
    )
    parser.add_argument(
        "--metadata-output",
        type=Path,
        default=Path("data/features/sequences/scan-subtype-60s-n5-metadata.json"),
    )
    parser.add_argument("--sequence-length", type=int, default=5)
    parser.add_argument("--stride", type=int, default=1)
    parser.add_argument("--target-column", default="scan_subtype")
    parser.add_argument(
        "--feature-columns",
        type=Path,
        default=None,
        help="Optional JSON list of feature columns. Inferred from train CSV when omitted.",
    )
    parser.add_argument(
        "--pad-start",
        action="store_true",
        help="Left-pad early windows with zeros so every window can produce one sequence.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    try:
        import numpy as np
    except ImportError as exc:
        raise SystemExit(
            "missing numpy. Install the ML requirements first, for example: "
            "pip install -r requirements-ml.txt"
        ) from exc

    train_rows, train_fields = read_csv(args.train_input)
    test_rows, _ = read_csv(args.test_input)
    if not train_rows:
        raise SystemExit(f"empty training CSV: {args.train_input}")
    if not test_rows:
        raise SystemExit(f"empty test CSV: {args.test_input}")
    if args.sequence_length < 1:
        raise SystemExit("--sequence-length must be >= 1")
    if args.stride < 1:
        raise SystemExit("--stride must be >= 1")

    feature_columns = (
        read_json(args.feature_columns)
        if args.feature_columns
        else infer_feature_columns(train_rows, train_fields, target_column=args.target_column)
    )
    classes = ordered_labels(row.get(args.target_column, "") for row in train_rows)
    if len(classes) < 2:
        raise SystemExit(f"need at least two target classes, got: {classes}")
    label_to_index = {label: index for index, label in enumerate(classes)}

    train_payload = build_sequences(
        np,
        train_rows,
        feature_columns=feature_columns,
        target_column=args.target_column,
        label_to_index=label_to_index,
        sequence_length=args.sequence_length,
        stride=args.stride,
        pad_start=args.pad_start,
    )
    test_payload = build_sequences(
        np,
        test_rows,
        feature_columns=feature_columns,
        target_column=args.target_column,
        label_to_index=label_to_index,
        sequence_length=args.sequence_length,
        stride=args.stride,
        pad_start=args.pad_start,
    )
    write_npz(np, args.train_output, train_payload)
    write_npz(np, args.test_output, test_payload)
    write_json(
        args.metadata_output,
        {
            "feature_columns": feature_columns,
            "classes": classes,
            "label_to_index": label_to_index,
            "sequence_length": args.sequence_length,
            "stride": args.stride,
            "pad_start": args.pad_start,
            "train_sequences": int(train_payload["X"].shape[0]),
            "test_sequences": int(test_payload["X"].shape[0]),
        },
    )
    print(f"wrote train sequences to {args.train_output}: {train_payload['X'].shape}")
    print(f"wrote test sequences to {args.test_output}: {test_payload['X'].shape}")
    print(f"wrote metadata to {args.metadata_output}")


def read_csv(path: Path) -> tuple[list[dict[str, str]], list[str]]:
    with path.open("r", encoding="utf-8", newline="") as file:
        reader = csv.DictReader(file)
        if reader.fieldnames is None:
            return [], []
        return list(reader), list(reader.fieldnames)


def read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as file:
        return json.load(file)


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as file:
        json.dump(payload, file, indent=2, sort_keys=True)
        file.write("\n")


def infer_feature_columns(
    rows: list[dict[str, str]],
    fieldnames: list[str],
    *,
    target_column: str,
) -> list[str]:
    excluded = set(EXCLUDE_COLUMNS)
    excluded.add(target_column)
    columns: list[str] = []
    for fieldname in fieldnames:
        if fieldname in excluded:
            continue
        if all(is_float(row.get(fieldname, "")) for row in rows):
            columns.append(fieldname)
    return columns


def build_sequences(
    np: Any,
    rows: list[dict[str, str]],
    *,
    feature_columns: list[str],
    target_column: str,
    label_to_index: dict[str, int],
    sequence_length: int,
    stride: int,
    pad_start: bool,
) -> dict[str, Any]:
    groups: dict[tuple[str, str], list[dict[str, str]]] = {}
    for row in rows:
        group_key = (row.get("run_id", ""), row.get("src_entity", ""))
        groups.setdefault(group_key, []).append(row)

    sequences: list[list[list[float]]] = []
    labels: list[int] = []
    run_ids: list[str] = []
    src_entities: list[str] = []
    window_starts: list[str] = []
    window_ends: list[str] = []
    unknown_labels: set[str] = set()

    for (run_id, src_entity), group_rows in groups.items():
        ordered_rows = sorted(group_rows, key=lambda row: parse_timestamp(row.get("window_start", "")))
        if not ordered_rows:
            continue

        start_index = 0 if pad_start else sequence_length - 1
        for end_index in range(start_index, len(ordered_rows), stride):
            target_label = str(ordered_rows[end_index].get(target_column) or "").strip()
            if target_label not in label_to_index:
                unknown_labels.add(target_label)
                continue

            sequence_rows = ordered_rows[max(0, end_index - sequence_length + 1) : end_index + 1]
            sequence = [
                [to_float(row.get(column, "")) for column in feature_columns]
                for row in sequence_rows
            ]
            if len(sequence) < sequence_length:
                if not pad_start:
                    continue
                padding = [[0.0 for _ in feature_columns] for _ in range(sequence_length - len(sequence))]
                sequence = [*padding, *sequence]

            sequences.append(sequence)
            labels.append(label_to_index[target_label])
            run_ids.append(run_id)
            src_entities.append(src_entity)
            window_starts.append(sequence_rows[0].get("window_start", ""))
            window_ends.append(ordered_rows[end_index].get("window_end", ""))

    if unknown_labels:
        raise SystemExit(f"data contains labels not seen in train data: {sorted(unknown_labels)}")

    if not sequences:
        feature_count = len(feature_columns)
        x = np.empty((0, sequence_length, feature_count), dtype=np.float32)
    else:
        x = np.array(sequences, dtype=np.float32)

    return {
        "X": x,
        "y": np.array(labels, dtype=np.int64),
        "run_id": np.array(run_ids),
        "src_entity": np.array(src_entities),
        "window_start": np.array(window_starts),
        "window_end": np.array(window_ends),
        "feature_columns": np.array(feature_columns),
        "classes": np.array(indexed_labels(label_to_index)),
    }


def write_npz(np: Any, path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(path, **payload)


def parse_timestamp(raw_value: str | None) -> float:
    if raw_value is None:
        return 0.0
    value = raw_value.strip()
    if not value:
        return 0.0
    try:
        return float(value)
    except ValueError:
        pass
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def is_float(value: str | None) -> bool:
    if value is None:
        return True
    value = str(value).strip()
    if not value or value == "-":
        return True
    try:
        float(value)
    except ValueError:
        return False
    return True


def to_float(value: str | None) -> float:
    if value is None:
        return 0.0
    value = str(value).strip()
    if not value or value == "-":
        return 0.0
    return float(value)


def ordered_labels(labels: Any) -> list[str]:
    unique = sorted({str(label).strip() for label in labels if str(label).strip()})
    if "normal" not in unique:
        return unique
    return ["normal", *[label for label in unique if label != "normal"]]


def indexed_labels(label_to_index: dict[str, int]) -> list[str]:
    return [label for label, _ in sorted(label_to_index.items(), key=lambda item: item[1])]


if __name__ == "__main__":
    main()
