# NDR Scan Detection Model Card

- Bundle: xgboost-scan-detection-groupcv-ndr-ml
- Model type: xgboost_binary_scan_detection
- Created at: 2026-05-30T12:13:09.228293+00:00
- Prediction threshold: 0.8
- Feature count: 22
- Lab integration ready: False
- Operational ready: False

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

- `model.json` sha256 `4e95a2f03f45fa5ac27a5a8822cb724f9c3eab3c0f3e60f38e354fb31a93ac78`
- `feature_names.json` sha256 `8660691d3501156cf921d0a23cbc3ab206d562ed4796f01306b1e6fda7b724f7`
- `metrics.json` sha256 `39e4a4974a9c6b4ce38a27c4413988ca30b40f9b260285b802fba7eb6a3c9377`
- `model.pkl` sha256 `d1718fe056f56df3751074c22787ee007d7780f72bf6d515417b786babf5148b`
- `predict_xgb_scan_detection.py` sha256 `9fbc47d13eaad4c0ae5a29574532d20169e353e128dfc97182916a9e4e0c1a8d`
