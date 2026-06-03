from __future__ import annotations

import argparse
import csv
import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_MODEL_DIR = Path("data/models/xgboost-scan-detection-ndr-ml")
DEFAULT_INPUT = Path("data/features/datasets/ipcam-scan-subtype-60s.csv")
DEFAULT_PREDICTIONS_OUTPUT = Path("data/ndr/predictions/scan_detection_predictions.csv")
DEFAULT_ALERTS_OUTPUT = Path("data/ndr/alerts/scan_detection_alerts.jsonl")
DEFAULT_SUMMARY_OUTPUT = Path("data/ndr/alerts/scan_detection_summary.json")
ALERT_SCHEMA_VERSION = "ndr-alert/v1"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run the exported NDR scan-detection model bundle and emit alert JSONL "
            "for windows predicted as scanning."
        )
    )
    parser.add_argument("--model-dir", type=Path, default=DEFAULT_MODEL_DIR)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--predictions-output", type=Path, default=DEFAULT_PREDICTIONS_OUTPUT)
    parser.add_argument("--alerts-output", type=Path, default=DEFAULT_ALERTS_OUTPUT)
    parser.add_argument("--summary-output", type=Path, default=DEFAULT_SUMMARY_OUTPUT)
    parser.add_argument("--threshold", type=float, default=None)
    parser.add_argument(
        "--alert-threshold",
        type=float,
        default=None,
        help="Probability threshold for alert emission. Defaults to the model threshold.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    manifest = read_json(args.model_dir / "manifest.json")
    model_threshold = args.threshold
    if model_threshold is None:
        model_threshold = float(manifest.get("prediction_threshold", 0.8))
    alert_threshold = args.alert_threshold
    if alert_threshold is None:
        alert_threshold = model_threshold

    predictor = load_predictor(args.model_dir / "predict_xgb_scan_detection.py")
    predictor_args = [
        "--model-dir",
        str(args.model_dir),
        "--input",
        str(args.input),
        "--output",
        str(args.predictions_output),
        "--threshold",
        str(model_threshold),
    ]
    predictor.main_with_args(predictor_args) if hasattr(predictor, "main_with_args") else run_predictor_main(predictor, predictor_args)

    prediction_rows = read_csv(args.predictions_output)
    alerts = build_alerts(
        prediction_rows,
        model_dir=args.model_dir,
        model_threshold=model_threshold,
        alert_threshold=alert_threshold,
        manifest=manifest,
    )
    write_jsonl(args.alerts_output, alerts)
    summary = build_summary(
        prediction_rows=prediction_rows,
        alerts=alerts,
        input_path=args.input,
        predictions_output=args.predictions_output,
        alerts_output=args.alerts_output,
        model_dir=args.model_dir,
        model_threshold=model_threshold,
        alert_threshold=alert_threshold,
        manifest=manifest,
    )
    write_json(args.summary_output, summary)
    print(f"wrote predictions to {args.predictions_output}")
    print(f"wrote alerts to {args.alerts_output}")
    print(f"wrote summary to {args.summary_output}")
    print(f"alerts={len(alerts)} rows={len(prediction_rows)}")


def load_predictor(path: Path) -> Any:
    if not path.exists():
        raise SystemExit(f"missing bundled predictor: {path}")
    spec = importlib.util.spec_from_file_location("bundled_ndr_predictor", path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"could not import bundled predictor: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_predictor_main(module: Any, argv: list[str]) -> None:
    # The bundled predictor is intentionally CLI-shaped. Temporarily patch argv
    # so this adapter reuses the exact same runtime path as direct CLI use.
    import sys

    old_argv = sys.argv
    try:
        sys.argv = [str(module.__file__), *argv]
        module.main()
    finally:
        sys.argv = old_argv


def build_alerts(
    rows: list[dict[str, str]],
    *,
    model_dir: Path,
    model_threshold: float,
    alert_threshold: float,
    manifest: dict[str, Any],
) -> list[dict[str, Any]]:
    alerts = []
    for row in rows:
        probability = to_float(row.get("scanning_probability"))
        if probability < alert_threshold:
            continue
        alert = {
            "schema_version": ALERT_SCHEMA_VERSION,
            "timestamp": row.get("window_end") or row.get("window_start") or now_utc(),
            "event_type": "ndr.scan_detection.alert",
            "model_bundle": manifest.get("bundle_name"),
            "model_dir": str(model_dir),
            "model_type": manifest.get("model_type"),
            "severity": severity_for_probability(probability),
            "confidence": round(probability, 6),
            "threshold": alert_threshold,
            "model_threshold": model_threshold,
            "predicted_label": row.get("predicted_name") or "scanning",
            "window_start": row.get("window_start"),
            "window_end": row.get("window_end"),
            "scenario_id": row.get("scenario_id"),
            "run_id": row.get("run_id"),
            "phase": row.get("phase") or row.get("phases"),
            "technique_id": row.get("technique_id") or row.get("technique_ids"),
            "src_entity": row.get("src_entity"),
            "data_source": row.get("data_source"),
            "is_synthetic": parse_bool(row.get("is_synthetic")),
            "details": {
                "scanning_probability": probability,
                "source_label": row.get("label"),
                "source_scan_subtype": row.get("scan_subtype"),
            },
        }
        alerts.append(compact(alert))
    return alerts


def severity_for_probability(probability: float) -> str:
    if probability >= 0.98:
        return "critical"
    if probability >= 0.90:
        return "high"
    if probability >= 0.80:
        return "medium"
    return "low"


def build_summary(
    *,
    prediction_rows: list[dict[str, str]],
    alerts: list[dict[str, Any]],
    input_path: Path,
    predictions_output: Path,
    alerts_output: Path,
    model_dir: Path,
    model_threshold: float,
    alert_threshold: float,
    manifest: dict[str, Any],
) -> dict[str, Any]:
    severity_counts: dict[str, int] = {}
    for alert in alerts:
        severity = str(alert.get("severity") or "unknown")
        severity_counts[severity] = severity_counts.get(severity, 0) + 1
    return {
        "created_at": now_utc(),
        "input": str(input_path),
        "predictions_output": str(predictions_output),
        "alerts_output": str(alerts_output),
        "model_dir": str(model_dir),
        "model_bundle": manifest.get("bundle_name"),
        "model_created_at": manifest.get("created_at"),
        "model_threshold": model_threshold,
        "alert_threshold": alert_threshold,
        "rows_scored": len(prediction_rows),
        "alerts_emitted": len(alerts),
        "severity_counts": dict(sorted(severity_counts.items())),
        "limitations": [
            "This adapter converts model predictions into NDR alert records.",
            "Operational performance still depends on real run-separated evaluation gates.",
            "Raw src_ip and dst_ip are not emitted by this adapter.",
        ],
    }


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as file:
        return list(csv.DictReader(file))


def write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames: list[str] = []
    for row in rows:
        for key in row:
            if key not in fieldnames:
                fieldnames.append(key)
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as file:
        for row in rows:
            file.write(json.dumps(row, sort_keys=True) + "\n")


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as file:
        json.dump(payload, file, indent=2, sort_keys=True)
        file.write("\n")


def read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as file:
        return json.load(file)


def to_float(value: str | None) -> float:
    try:
        return float(value or 0)
    except ValueError:
        return 0.0


def parse_bool(value: str | None) -> bool | None:
    if value is None or value == "":
        return None
    return str(value).strip().lower() in {"1", "true", "yes", "y"}


def compact(record: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in record.items() if value is not None and value != ""}


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


if __name__ == "__main__":
    main()
