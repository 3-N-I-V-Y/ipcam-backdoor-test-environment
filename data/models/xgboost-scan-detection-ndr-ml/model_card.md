# NDR Scan Detection Model Card

- Bundle: xgboost-scan-detection-ndr-ml
- Model type: xgboost_binary_scan_detection
- Created at: 2026-06-02T17:28:43.261607+00:00
- Prediction threshold: 0.8
- Feature count: 55
- Lab integration ready: True
- Operational ready: True

## Intended Use

Binary scan detection for defensive NDR lab and controlled pilot workflows.
Use real-only metrics as the primary performance evidence.

## Required Inputs

Window-level behavioral features. Raw source or destination IP values are not model features.

## Limitations

- Lab integration means the model bundle has usable artifacts, stable feature schema, and real-test metrics above gates.
- Operational ready additionally requires stronger train/test volume, source-separated real/synthetic evaluation evidence, and group-separated real-only evaluation when available.
- Synthetic data must not be used to inflate final real-only performance claims.

## Artifacts

- `model.json` sha256 `6e7f86744eb5c15282dab6797912e070f6ca7ecdb111d178fa9d262be92af862`
- `feature_names.json` sha256 `1670f9c739bfd37ec57f5fb20166fbeab45afef75c7161caf30e0c10db773efe`
- `metrics.json` sha256 `986ea396e8290754184a089673caf04198f2f677961ada5373e57e238670c205`
- `model.pkl` sha256 `635804143fc57f5ec4e1418eff2379c7238ad52557ae6ac9e1904fb48cad91da`
- `predict_xgb_scan_detection.py` sha256 `9fbc47d13eaad4c0ae5a29574532d20169e353e128dfc97182916a9e4e0c1a8d`
