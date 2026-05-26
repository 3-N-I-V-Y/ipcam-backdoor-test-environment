# Model Training

This pipeline trains a tabular scan subtype baseline first, then builds sequence
data from the same window vectors for GRU experiments.

## 1) Build src_ip + 60s Window Features

```bash
python3 scripts/build_scan_dataset.py \
  --zeek-root data/zeek \
  --features-root data/features/windowed \
  --ground-truth data/scenarios/ground-truth.jsonl \
  --target-column scan_subtype \
  --test-run baseline-003 \
  --test-run infected-scan-002 \
  --output data/features/datasets/ipcam-scan-subtype-60s.csv \
  --window-seconds 60
```

This writes:

```text
data/features/datasets/ipcam-scan-subtype-60s.csv
data/features/datasets/ipcam-scan-subtype-60s-train.csv
data/features/datasets/ipcam-scan-subtype-60s-test.csv
```

## 2) Train XGBoost Multi-Class Baseline

Install ML dependencies if needed:

```bash
pip install -r requirements-ml.txt
```

Train:

```bash
python3 scripts/train_xgboost_scan_subtype.py \
  --train data/features/datasets/ipcam-scan-subtype-60s-train.csv \
  --test data/features/datasets/ipcam-scan-subtype-60s-test.csv \
  --target-column scan_subtype \
  --output-dir data/models/xgboost-scan-subtype
```

Artifacts:

```text
data/models/xgboost-scan-subtype/model.json
data/models/xgboost-scan-subtype/label_encoder.json
data/models/xgboost-scan-subtype/feature_columns.json
data/models/xgboost-scan-subtype/metrics.json
```

## 3) Build GRU Sequences

Use the exact XGBoost feature columns so the GRU sees the same window vector:

```bash
python3 scripts/build_gru_sequences.py \
  --train-input data/features/datasets/ipcam-scan-subtype-60s-train.csv \
  --test-input data/features/datasets/ipcam-scan-subtype-60s-test.csv \
  --feature-columns data/models/xgboost-scan-subtype/feature_columns.json \
  --sequence-length 5 \
  --train-output data/features/sequences/scan-subtype-60s-n5-train.npz \
  --test-output data/features/sequences/scan-subtype-60s-n5-test.npz \
  --metadata-output data/features/sequences/scan-subtype-60s-n5-metadata.json
```

`sequence_length=5` means each sample represents the latest five 60-second
windows for the same `run_id + src_entity`.

## 4) Train GRU

```bash
python3 scripts/train_gru_scan_subtype.py \
  --train data/features/sequences/scan-subtype-60s-n5-train.npz \
  --test data/features/sequences/scan-subtype-60s-n5-test.npz \
  --output-dir data/models/gru-scan-subtype \
  --epochs 30 \
  --hidden-size 64
```

Artifacts:

```text
data/models/gru-scan-subtype/model.pt
data/models/gru-scan-subtype/metadata.json
data/models/gru-scan-subtype/normalization.json
data/models/gru-scan-subtype/metrics.json
```

## Notes

Use run-based splits. Do not random-split windows from the same run into both
train and test, because it leaks scenario-specific timing and topology patterns.

If a run has only one subtype, collect more runs before trusting multi-class
metrics. A subtype classifier needs at least two classes in training data.
