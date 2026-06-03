from __future__ import annotations

import argparse
import json
from pathlib import Path


LEAKAGE_COLUMNS = {
    "label",
    "target",
    "type",
    "scan_subtype",
    "timestamp",
    "ts",
    "window_start",
    "window_end",
    "scenario_id",
    "run_id",
    "phase",
    "phases",
    "technique_id",
    "technique_ids",
    "src_entity",
    "src_ip",
    "dst_ip",
    "id.orig_h",
    "id.resp_h",
    "is_synthetic",
    "data_source",
}
METADATA_COLUMNS = [
    "window_start",
    "window_end",
    "scenario_id",
    "run_id",
    "label",
    "scan_subtype",
    "phase",
    "phases",
    "technique_id",
    "technique_ids",
    "src_entity",
    "data_source",
    "is_synthetic",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run binary XGBoost NDR scan detection on window feature CSV data.",
    )
    parser.add_argument("--model-dir", required=True, type=Path)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--threshold", type=float, default=None)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    try:
        import pandas as pd
        import xgboost as xgb
    except ImportError as exc:
        raise SystemExit(
            "missing runtime dependency. Activate the project venv and install requirements.txt."
        ) from exc

    manifest = read_json(args.model_dir / "manifest.json")
    feature_columns = [str(value) for value in manifest["feature_columns"]]
    threshold = args.threshold
    if threshold is None:
        threshold = float(manifest.get("prediction_threshold", 0.8))

    frame = pd.read_csv(args.input)
    metadata = frame[[column for column in METADATA_COLUMNS if column in frame.columns]].copy()
    features = frame.drop(columns=[column for column in LEAKAGE_COLUMNS if column in frame.columns], errors="ignore")
    features = features.apply(pd.to_numeric, errors="coerce").replace([float("inf"), float("-inf")], 0).fillna(0)
    features = features.reindex(columns=feature_columns, fill_value=0)

    model = xgb.Booster()
    model.load_model(str(args.model_dir / "model.json"))
    dmatrix = xgb.DMatrix(features, feature_names=feature_columns)
    probabilities = model.predict(dmatrix)
    if probabilities.ndim > 1:
        probabilities = probabilities[:, 1]
    predictions = (probabilities >= threshold).astype(int)

    output = metadata.copy()
    output["scanning_probability"] = probabilities
    output["predicted_label"] = predictions
    output["predicted_name"] = output["predicted_label"].map({0: "normal", 1: "scanning"})
    output["threshold"] = threshold
    args.output.parent.mkdir(parents=True, exist_ok=True)
    output.to_csv(args.output, index=False)
    print(f"wrote predictions to {args.output}")


def read_json(path: Path) -> dict[str, object]:
    with path.open("r", encoding="utf-8") as file:
        return json.load(file)


if __name__ == "__main__":
    main()
