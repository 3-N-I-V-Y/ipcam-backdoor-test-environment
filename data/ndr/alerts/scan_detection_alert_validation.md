# NDR Alert Validation

- Passed: True
- Alert count: 380
- Alerts: `data/ndr/alerts/scan_detection_alerts.jsonl`
- Summary: `data/ndr/alerts/scan_detection_summary.json`

## Checks

| check | passed | details |
| --- | --- | --- |
| alerts_file_exists | True | `{"path": "data/ndr/alerts/scan_detection_alerts.jsonl"}` |
| summary_file_exists | True | `{"path": "data/ndr/alerts/scan_detection_summary.json"}` |
| alerts_non_empty | True | `{"alert_count": 380}` |
| all_alert_records_valid | True | `{"failure_count": 0, "sample_failures": []}` |
| no_raw_identity_keys | True | `{"forbidden_keys": ["dst_ip", "id.orig_h", "id.resp_h", "src_ip"]}` |
| summary_alert_count_matches_jsonl | True | `{"jsonl_alerts": 380, "summary_alerts": 380}` |
| summary_severity_counts_match_jsonl | True | `{"jsonl": {"critical": 369, "high": 10, "medium": 1}, "summary": {"critical": 369, "high": 10, "medium": 1}}` |
| summary_documents_raw_ip_exclusion | True | `{}` |

## Limitations

- This validates alert schema and output contract, not model performance.
- Smoke alert counts are not operational detection metrics.
