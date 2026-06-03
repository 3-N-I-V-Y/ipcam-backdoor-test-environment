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

## 3) Build GRU/LSTM Sequences

Use the exact XGBoost feature columns so the RNN sees the same window vector:

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

## 4) Train GRU Or LSTM

```bash
python3 scripts/train_gru_scan_subtype.py \
  --rnn-type gru \
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

Use `--rnn-type lstm` and a different `--output-dir` to train an LSTM on the
same sequence files.

For long-running 10-second sequence collection on a Linux server, use the
checkpointed collector in `docs/sequence-collection.md`:

```bash
python3 scripts/collect_sequence_dataset.py plan \
  --config configs/sequence_collection_10s_24h.json

python3 scripts/collect_sequence_dataset.py resume \
  --config configs/sequence_collection_10s_24h.json \
  --retry-failed \
  --finalize
```

## Notes

Use run-based splits. Do not random-split windows from the same run into both
train and test, because it leaks scenario-specific timing and topology patterns.

If a run has only one subtype, collect more runs before trusting multi-class
metrics. A subtype classifier needs at least two classes in training data.

## Data Sufficiency, Synthetic Data, And WSL Checks

The companion `../ndr-ml` workspace now includes scripts for:

- diagnosing label, scenario, run, technique, split, dominance, and leakage risk;
- generating schema-compatible synthetic feature rows for training augmentation;
- evaluating real-only, synthetic-only, and real+synthetic training modes separately;
- evaluating real-only generalization with `run_id` group-separated CV;
- checking WSL system tools and Python ML dependencies.

See:

```bash
cd ../ndr-ml
python3 diagnose_data_sufficiency.py --help
python3 generate_synthetic_data.py --help
python3 validate_synthetic_data.py --help
python3 train_eval_xgb_sources.py --help
python3 train_eval_xgb_group_cv.py --help
python3 check_wsl_dependencies.py --help
```

Do not mix synthetic rows into final real-only test metrics. Treat synthetic data
as training augmentation and pipeline validation data unless a separate
synthetic-test result is explicitly labeled.

The binary scan detector can be exported into this project as a lab integration
bundle from the current holdout artifacts:

```bash
cd ../ndr-ml
python3 export_ndr_model_bundle.py \
  --output-dir ../ipcam-backdoor-test-environment/data/models/xgboost-scan-detection-ndr-ml
```

Review these files before wiring the model into a runtime service:

```text
data/models/xgboost-scan-detection-ndr-ml/manifest.json
data/models/xgboost-scan-detection-ndr-ml/model_card.md
data/models/xgboost-scan-detection-ndr-ml/readiness_report.md
```

Validate artifact integrity and runtime prediction compatibility from
`../ndr-ml`:

```bash
cd ../ndr-ml
.venv-wsl/bin/python validate_model_bundle.py \
  --model-dir ../ipcam-backdoor-test-environment/data/models/xgboost-scan-detection-ndr-ml \
  --input ../ipcam-backdoor-test-environment/data/features/datasets/ipcam-scan-subtype-60s.csv \
  --output-json models/model_bundle_validation.json \
  --output-md models/model_bundle_validation.md \
  --prediction-output models/model_bundle_validation_predictions.csv
```

This check validates the manifest, artifact hashes, feature schema, model
loading, and prediction output shape. Smoke-set metrics from this command are
not operational readiness evidence.

To emit solution-facing NDR alert records from a feature CSV, run:

```bash
python3 scripts/run_ndr_scan_detection.py \
  --model-dir data/models/xgboost-scan-detection-ndr-ml \
  --input data/features/datasets/ipcam-scan-subtype-60s.csv \
  --predictions-output data/ndr/predictions/scan_detection_predictions.csv \
  --alerts-output data/ndr/alerts/scan_detection_alerts.jsonl \
  --summary-output data/ndr/alerts/scan_detection_summary.json
```

The adapter emits `ndr-alert/v1` JSONL records for windows predicted as
scanning. It keeps hashed `src_entity` metadata but does not emit raw `src_ip`
or `dst_ip`.

Validate the alert output contract:

```bash
python3 scripts/validate_ndr_alerts.py \
  --alerts data/ndr/alerts/scan_detection_alerts.jsonl \
  --summary data/ndr/alerts/scan_detection_summary.json \
  --output-json data/ndr/alerts/scan_detection_alert_validation.json \
  --output-md data/ndr/alerts/scan_detection_alert_validation.md
```

A second challenger bundle can be exported from the final model trained on all
real rows used by run-group CV. It is more honest as a generalization candidate,
but currently fails the metric gates:

```bash
cd ../ndr-ml
python3 export_ndr_model_bundle.py \
  --model-json models/group_cv_evaluation/final_model.json \
  --model-pkl models/group_cv_evaluation/final_model.pkl \
  --feature-names models/group_cv_evaluation/final_feature_names.json \
  --metrics models/group_cv_evaluation/final_model_metrics.json \
  --output-dir ../ipcam-backdoor-test-environment/data/models/xgboost-scan-detection-groupcv-ndr-ml \
  --bundle-name xgboost-scan-detection-groupcv-ndr-ml
```

After collecting enough new real runs and rerunning group CV, promote the
group-CV final model into the primary bundle path instead of exporting the old
holdout model:

```bash
cd ../ndr-ml
python3 export_ndr_model_bundle.py \
  --model-json models/group_cv_evaluation/final_model.json \
  --model-pkl models/group_cv_evaluation/final_model.pkl \
  --feature-names models/group_cv_evaluation/final_feature_names.json \
  --metrics models/group_cv_evaluation/final_model_metrics.json \
  --output-dir ../ipcam-backdoor-test-environment/data/models/xgboost-scan-detection-ndr-ml \
  --bundle-name xgboost-scan-detection-ndr-ml
```

Current status: lab integration and operational readiness gates pass for the
`targeted-op20s-v1-scan-subtype-20s.csv` lab dataset. The current binary scan
detector uses 55 behavior features, including rolling source-window context,
and excludes raw IPs, `src_entity`, `scenario_id`, `run_id`, `technique_id`,
`data_source`, and `is_synthetic` from model inputs.

The latest source-separated real-only holdout result is precision 1.0000,
recall 1.0000, F1 1.0000, and ROC-AUC 1.0000 on 366 real test windows. The
latest run-group CV result is precision 1.0000, recall 0.9947, F1 0.9974, and
ROC-AUC 0.99999 on 1,025 real out-of-fold windows across 70 runs. These are
simulation lab metrics, not a guarantee of production network performance.

The dataset labeler now resolves ground-truth `source` and `target` aliases to
Zeek endpoint IPs before assigning scan labels. This prevents normal traffic on
the same port/proto/time window from being mislabeled as scanning.

To collect more run-diverse data, use the long bulk command in
`docs/data-collection.md`. It keeps 60-second windows as the evidence unit,
uses repeated long captures for faster scan profiles, leaves low-and-slow as
its own delayed scan profile, and reserves multiple repeat indices for the
run-based test split. Estimate the plan first:

```bash
python3 scripts/collect_bulk_scan_dataset.py \
  --estimate-only \
  --run-prefix bulk \
  --baseline-repeats 8 \
  --attack-repeats 8 \
  --baseline-seconds 1200 \
  --attack-duration-seconds 1200 \
  --attack-interval-seconds 20 \
  --capture-network-mode host \
  --long-capture-scenario vertical-scan \
  --long-capture-scenario horizontal-scan \
  --long-capture-scenario service-probe \
  --long-capture-scenario udp-scan \
  --test-repeat 7 \
  --test-repeat 8
```

The current plan estimate is 780 train windows and 260 test windows, with about
17.3 hours of sequential collection. The actual collection command is the same
except `--estimate-only` is replaced with `--compose-up --skip-existing` and
`--scenario-log-root data/scenarios/generated` is included.

After collection, run the `../ndr-ml` post-collection pipeline. It regenerates
the data sufficiency report, synthetic augmentation, source-separated
evaluation, run-group CV, primary model bundle, and readiness audit:

```bash
cd ../ndr-ml
.venv-wsl/bin/python run_operational_readiness_pipeline.py \
  --solution-root ../ipcam-backdoor-test-environment
```

Use `--dry-run` first if dataset paths or output locations changed.

Short smoke or pilot runs validate the pipeline only; do not use them as
operational readiness metrics.

The current end-to-end audit is generated from `../ndr-ml`:

```bash
python3 audit_goal_readiness.py \
  --project-root . \
  --solution-root ../ipcam-backdoor-test-environment
```

Review `../ndr-ml/models/goal_readiness_audit.md` before treating the model as
the current lab operational candidate.
