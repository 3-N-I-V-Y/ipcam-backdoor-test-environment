# NDR Model Readiness Report

- Lab integration ready: False
- Operational ready: False

| check | passed | details |
| --- | --- | --- |
| feature_schema_present | True | `{"feature_count": 22}` |
| no_leakage_or_identity_features | True | `{"leaked_features": []}` |
| metric_precision_gate | False | `{"actual": 0.7450980392156863, "required": 0.9}` |
| metric_recall_gate | False | `{"actual": 0.5786802030456852, "required": 0.9}` |
| metric_f1_gate | False | `{"actual": 0.6514285714285715, "required": 0.9}` |
| metric_roc_auc_gate | False | `{"actual": 0.8736801449662559, "required": 0.95}` |
| metric_false_positive_rate_gate | True | `{"actual": 0.04462242562929062, "required": 0.05}` |
| confusion_matrix_present | True | `{"fn": 83, "fp": 39, "tn": 835, "tp": 114}` |
| train_row_volume_gate | True | `{"actual": 1071, "required": 500}` |
| test_row_volume_gate | True | `{"actual": 1071, "required": 200}` |
| data_sufficiency_report_available | True | `{"is_insufficient": true, "total_samples": 1071}` |
| source_separated_evaluation_available | True | `{"modes": ["real+synthetic", "real-only", "synthetic-only"]}` |
| group_cv_evaluation_available | True | `{"group_count": 22, "n_splits": 5, "rows": 1071}` |
| group_cv_no_group_overlap | True | `{"group_overlap_between_train_and_test": false}` |
| group_cv_no_raw_identity_features | True | `{"raw_identity_features": []}` |
| group_cv_feature_schema_matches_bundle | True | `{"bundle_feature_count": 22, "extra_in_group_cv": [], "group_cv_feature_count": 22, "missing_from_group_cv": [], "same_order": true}` |
| group_cv_volume_gate | True | `{"min_train_rows_per_fold": 816, "out_of_fold_rows": 1071, "required_oof_rows": 200, "required_train_rows": 500}` |
| group_cv_metric_precision_gate | False | `{"actual": 0.7450980392156863, "required": 0.9}` |
| group_cv_metric_recall_gate | False | `{"actual": 0.5786802030456852, "required": 0.9}` |
| group_cv_metric_f1_gate | False | `{"actual": 0.6514285714285715, "required": 0.9}` |
| group_cv_metric_roc_auc_gate | False | `{"actual": 0.8736801449662559, "required": 0.95}` |
| group_cv_false_positive_rate_gate | True | `{"actual": 0.04462242562929062, "required": 0.05}` |

## Notes

- Lab integration means the model bundle has usable artifacts, stable feature schema, and real-test metrics above gates.
- Operational ready additionally requires stronger train/test volume, source-separated real/synthetic evaluation evidence, and group-separated real-only evaluation when available.
- Synthetic data must not be used to inflate final real-only performance claims.
