from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any


EXPECTED_SCHEMA_VERSION = "ndr-alert/v1"
EXPECTED_EVENT_TYPE = "ndr.scan_detection.alert"
ALLOWED_SEVERITIES = {"low", "medium", "high", "critical"}
RAW_IDENTITY_KEYS = {"src_ip", "dst_ip", "id.orig_h", "id.resp_h"}
REQUIRED_KEYS = {
    "schema_version",
    "timestamp",
    "event_type",
    "model_bundle",
    "model_type",
    "severity",
    "confidence",
    "threshold",
    "predicted_label",
    "window_start",
    "window_end",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate solution-facing NDR alert JSONL output.")
    parser.add_argument("--alerts", type=Path, default=Path("data/ndr/alerts/scan_detection_alerts.jsonl"))
    parser.add_argument("--summary", type=Path, default=Path("data/ndr/alerts/scan_detection_summary.json"))
    parser.add_argument("--output-json", type=Path, default=Path("data/ndr/alerts/scan_detection_alert_validation.json"))
    parser.add_argument("--output-md", type=Path, default=Path("data/ndr/alerts/scan_detection_alert_validation.md"))
    parser.add_argument("--allow-empty", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    report = validate_alerts(args)
    write_json(args.output_json, report)
    args.output_md.parent.mkdir(parents=True, exist_ok=True)
    args.output_md.write_text(render_markdown(report), encoding="utf-8")
    print(f"wrote {args.output_json}")
    print(f"wrote {args.output_md}")
    print("alert_validation=" + ("pass" if report["passed"] else "fail"))


def validate_alerts(args: argparse.Namespace) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    alerts = read_jsonl(args.alerts)
    summary = read_optional_json(args.summary)
    checks.append(check("alerts_file_exists", args.alerts.exists(), {"path": str(args.alerts)}))
    checks.append(check("summary_file_exists", summary is not None, {"path": str(args.summary)}))
    checks.append(check("alerts_non_empty", args.allow_empty or bool(alerts), {"alert_count": len(alerts)}))

    severity_counts: Counter[str] = Counter()
    failures: list[dict[str, Any]] = []
    for index, alert in enumerate(alerts, start=1):
        alert_failures = validate_alert_record(alert)
        if alert_failures:
            failures.append({"line": index, "failures": alert_failures, "alert": alert})
        severity = str(alert.get("severity") or "unknown")
        severity_counts[severity] += 1

    checks.append(check("all_alert_records_valid", not failures, {"failure_count": len(failures), "sample_failures": failures[:5]}))
    checks.append(check("no_raw_identity_keys", no_raw_identity_keys(alerts), {"forbidden_keys": sorted(RAW_IDENTITY_KEYS)}))
    if summary is not None:
        checks.append(
            check(
                "summary_alert_count_matches_jsonl",
                int(summary.get("alerts_emitted") or 0) == len(alerts),
                {"summary_alerts": summary.get("alerts_emitted"), "jsonl_alerts": len(alerts)},
            )
        )
        checks.append(
            check(
                "summary_severity_counts_match_jsonl",
                normalize_counter(summary.get("severity_counts") or {}) == dict(sorted(severity_counts.items())),
                {
                    "summary": normalize_counter(summary.get("severity_counts") or {}),
                    "jsonl": dict(sorted(severity_counts.items())),
                },
            )
        )
        checks.append(
            check(
                "summary_documents_raw_ip_exclusion",
                any(
                    "Raw src_ip and dst_ip are not emitted" in str(note)
                    for note in summary.get("limitations", [])
                ),
                {},
            )
        )

    return {
        "passed": all(item["passed"] for item in checks),
        "alerts": str(args.alerts),
        "summary": str(args.summary),
        "alert_count": len(alerts),
        "severity_counts": dict(sorted(severity_counts.items())),
        "checks": checks,
        "limitations": [
            "This validates alert schema and output contract, not model performance.",
            "Smoke alert counts are not operational detection metrics.",
        ],
    }


def validate_alert_record(alert: dict[str, Any]) -> list[str]:
    failures = []
    missing = sorted(key for key in REQUIRED_KEYS if key not in alert)
    if missing:
        failures.append("missing_required_keys=" + ",".join(missing))
    if alert.get("schema_version") != EXPECTED_SCHEMA_VERSION:
        failures.append("invalid_schema_version")
    if alert.get("event_type") != EXPECTED_EVENT_TYPE:
        failures.append("invalid_event_type")
    if alert.get("severity") not in ALLOWED_SEVERITIES:
        failures.append("invalid_severity")
    confidence = to_float(alert.get("confidence"))
    threshold = to_float(alert.get("threshold"))
    if confidence is None or confidence < 0 or confidence > 1:
        failures.append("confidence_out_of_range")
    if threshold is None or threshold < 0 or threshold > 1:
        failures.append("threshold_out_of_range")
    if confidence is not None and threshold is not None and confidence < threshold:
        failures.append("confidence_below_threshold")
    if alert.get("predicted_label") != "scanning":
        failures.append("predicted_label_not_scanning")
    for key in ("timestamp", "window_start", "window_end"):
        if key in alert and not valid_iso_timestamp(str(alert[key])):
            failures.append(f"invalid_{key}")
    if RAW_IDENTITY_KEYS.intersection(alert.keys()):
        failures.append("raw_identity_key_present")
    details = alert.get("details")
    if isinstance(details, dict) and RAW_IDENTITY_KEYS.intersection(details.keys()):
        failures.append("raw_identity_key_present_in_details")
    return failures


def no_raw_identity_keys(alerts: list[dict[str, Any]]) -> bool:
    for alert in alerts:
        if RAW_IDENTITY_KEYS.intersection(alert.keys()):
            return False
        details = alert.get("details")
        if isinstance(details, dict) and RAW_IDENTITY_KEYS.intersection(details.keys()):
            return False
    return True


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows = []
    with path.open("r", encoding="utf-8") as file:
        for line in file:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def read_optional_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    with path.open("r", encoding="utf-8") as file:
        return json.load(file)


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as file:
        json.dump(payload, file, indent=2, sort_keys=True)
        file.write("\n")


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# NDR Alert Validation",
        "",
        f"- Passed: {report['passed']}",
        f"- Alert count: {report['alert_count']}",
        f"- Alerts: `{report['alerts']}`",
        f"- Summary: `{report['summary']}`",
        "",
        "## Checks",
        "",
        "| check | passed | details |",
        "| --- | --- | --- |",
    ]
    for item in report["checks"]:
        lines.append(f"| {item['name']} | {item['passed']} | `{json.dumps(item['details'], sort_keys=True)}` |")
    lines.extend(["", "## Limitations", ""])
    for limitation in report["limitations"]:
        lines.append(f"- {limitation}")
    lines.append("")
    return "\n".join(lines)


def check(name: str, passed: bool, details: dict[str, Any]) -> dict[str, Any]:
    return {"name": name, "passed": bool(passed), "details": details}


def to_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def valid_iso_timestamp(value: str) -> bool:
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def normalize_counter(value: dict[str, Any]) -> dict[str, int]:
    return {str(key): int(count) for key, count in sorted(value.items())}


if __name__ == "__main__":
    main()
