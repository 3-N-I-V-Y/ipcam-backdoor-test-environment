from __future__ import annotations

import argparse
from collections import Counter
import csv
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
        description="Train an XGBoost multi-class baseline for scan subtype detection.",
    )
    parser.add_argument("--train", required=True, type=Path, help="Training CSV")
    parser.add_argument("--test", required=True, type=Path, help="Test CSV")
    parser.add_argument("--target-column", default="scan_subtype")
    parser.add_argument(
        "--feature-columns",
        type=Path,
        default=None,
        help="Optional JSON list of feature columns. Inferred from train CSV when omitted.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("data/models/xgboost-scan-subtype"),
    )
    parser.add_argument("--num-rounds", type=int, default=300)
    parser.add_argument("--early-stopping-rounds", type=int, default=30)
    parser.add_argument("--max-depth", type=int, default=4)
    parser.add_argument("--eta", type=float, default=0.05)
    parser.add_argument("--subsample", type=float, default=0.9)
    parser.add_argument("--colsample-bytree", type=float, default=0.9)
    parser.add_argument("--seed", type=int, default=13)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    try:
        import numpy as np
        import xgboost as xgb
    except ImportError as exc:
        raise SystemExit(
            "missing ML dependency. Install the ML requirements first, for example: "
            "pip install -r requirements-ml.txt"
        ) from exc

    train_rows, train_fields = read_csv(args.train)
    test_rows, _ = read_csv(args.test)
    if not train_rows:
        raise SystemExit(f"empty training CSV: {args.train}")
    if not test_rows:
        raise SystemExit(f"empty test CSV: {args.test}")

    feature_columns = (
        read_json(args.feature_columns)
        if args.feature_columns
        else infer_feature_columns(train_rows, train_fields, target_column=args.target_column)
    )
    if not feature_columns:
        raise SystemExit("no numeric feature columns found")

    classes = ordered_labels(row.get(args.target_column, "") for row in train_rows)
    if len(classes) < 2:
        raise SystemExit(f"need at least two target classes, got: {classes}")
    label_to_index = {label: index for index, label in enumerate(classes)}

    y_train = encode_labels(train_rows, target_column=args.target_column, label_to_index=label_to_index)
    y_test = encode_labels(test_rows, target_column=args.target_column, label_to_index=label_to_index)
    x_train = rows_to_matrix(np, train_rows, feature_columns)
    x_test = rows_to_matrix(np, test_rows, feature_columns)

    dtrain = xgb.DMatrix(x_train, label=y_train, feature_names=feature_columns)
    dtest = xgb.DMatrix(x_test, label=y_test, feature_names=feature_columns)
    params = {
        "objective": "multi:softprob",
        "num_class": len(classes),
        "eval_metric": "mlogloss",
        "max_depth": args.max_depth,
        "eta": args.eta,
        "subsample": args.subsample,
        "colsample_bytree": args.colsample_bytree,
        "seed": args.seed,
    }
    model = xgb.train(
        params,
        dtrain,
        num_boost_round=args.num_rounds,
        evals=[(dtrain, "train"), (dtest, "test")],
        early_stopping_rounds=args.early_stopping_rounds,
        verbose_eval=25,
    )

    probabilities = model.predict(dtest)
    predictions = probabilities.argmax(axis=1)
    metrics = compute_metrics(y_test, predictions.tolist(), classes)
    metrics.update(
        {
            "train_rows": len(train_rows),
            "test_rows": len(test_rows),
            "feature_count": len(feature_columns),
            "class_counts_train": dict(sorted(Counter(row[args.target_column] for row in train_rows).items())),
            "class_counts_test": dict(sorted(Counter(row[args.target_column] for row in test_rows).items())),
            "best_iteration": int(getattr(model, "best_iteration", 0) or 0),
        }
    )

    args.output_dir.mkdir(parents=True, exist_ok=True)
    model.save_model(str(args.output_dir / "model.json"))
    write_json(args.output_dir / "label_encoder.json", {"classes": classes, "label_to_index": label_to_index})
    write_json(args.output_dir / "feature_columns.json", feature_columns)
    write_json(args.output_dir / "metrics.json", metrics)
    print(f"wrote model artifacts to {args.output_dir}")
    print(json.dumps(metrics, indent=2, sort_keys=True))


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


def rows_to_matrix(np: Any, rows: list[dict[str, str]], feature_columns: list[str]) -> Any:
    return np.array(
        [[to_float(row.get(column, "")) for column in feature_columns] for row in rows],
        dtype=np.float32,
    )


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


def encode_labels(
    rows: list[dict[str, str]],
    *,
    target_column: str,
    label_to_index: dict[str, int],
) -> list[int]:
    encoded: list[int] = []
    unknown: set[str] = set()
    for row in rows:
        label = str(row.get(target_column) or "").strip()
        if label not in label_to_index:
            unknown.add(label)
            continue
        encoded.append(label_to_index[label])
    if unknown:
        raise SystemExit(f"test data contains labels not seen in train data: {sorted(unknown)}")
    return encoded


def compute_metrics(y_true: list[int], y_pred: list[int], classes: list[str]) -> dict[str, Any]:
    class_count = len(classes)
    confusion = [[0 for _ in range(class_count)] for _ in range(class_count)]
    for actual, predicted in zip(y_true, y_pred):
        confusion[actual][predicted] += 1

    total = len(y_true)
    correct = sum(confusion[index][index] for index in range(class_count))
    per_class: dict[str, dict[str, float]] = {}
    f1_values: list[float] = []
    for index, label in enumerate(classes):
        tp = confusion[index][index]
        fp = sum(confusion[row][index] for row in range(class_count) if row != index)
        fn = sum(confusion[index][column] for column in range(class_count) if column != index)
        precision = tp / max(tp + fp, 1)
        recall = tp / max(tp + fn, 1)
        f1 = (2 * precision * recall) / max(precision + recall, 1e-12)
        f1_values.append(f1)
        per_class[label] = {
            "precision": round(precision, 6),
            "recall": round(recall, 6),
            "f1": round(f1, 6),
            "support": sum(confusion[index]),
        }

    normal_index = classes.index("normal") if "normal" in classes else None
    normal_false_positive_rate = 0.0
    scan_recall = 0.0
    if normal_index is not None:
        normal_total = sum(confusion[normal_index])
        normal_missed = normal_total - confusion[normal_index][normal_index]
        normal_false_positive_rate = normal_missed / max(normal_total, 1)

        scan_total = sum(
            confusion[row][column]
            for row in range(class_count)
            for column in range(class_count)
            if row != normal_index
        )
        scan_correct = sum(
            confusion[row][column]
            for row in range(class_count)
            for column in range(class_count)
            if row != normal_index and column != normal_index
        )
        scan_recall = scan_correct / max(scan_total, 1)

    return {
        "accuracy": round(correct / max(total, 1), 6),
        "macro_f1": round(sum(f1_values) / max(len(f1_values), 1), 6),
        "normal_false_positive_rate": round(normal_false_positive_rate, 6),
        "scan_recall": round(scan_recall, 6),
        "per_class": per_class,
        "confusion_matrix": confusion,
        "classes": classes,
    }


if __name__ == "__main__":
    main()
