from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import UTC, datetime
import json
from pathlib import Path
import subprocess
import sys
from typing import Any


SCHEMA_VERSION = "scenario-log/v1"
DEFAULT_NMAP_IMAGE = "instrumentisto/nmap:latest"
DEFAULT_ALLOWED_TARGETS = {
    "camera-app",
    "control-server",
    "mediamtx",
    "nvr-console",
}


@dataclass(frozen=True, slots=True)
class Scenario:
    scan_type: str
    phase: str
    proto: str
    nmap_args: list[str]


SCENARIOS = {
    "vertical": Scenario(
        scan_type="vertical",
        phase="vertical_scan",
        proto="tcp",
        nmap_args=["-sT", "-Pn", "-n"],
    ),
    "horizontal": Scenario(
        scan_type="horizontal",
        phase="horizontal_scan",
        proto="tcp",
        nmap_args=["-sT", "-Pn", "-n"],
    ),
    "service-probe": Scenario(
        scan_type="service-probe",
        phase="service_probe",
        proto="tcp",
        nmap_args=["-sT", "-sV", "--version-light", "-Pn", "-n"],
    ),
    "udp": Scenario(
        scan_type="udp",
        phase="udp_scan",
        proto="udp",
        nmap_args=["-sU", "-Pn", "-n"],
    ),
    "low-and-slow": Scenario(
        scan_type="low-and-slow",
        phase="low_and_slow_scan",
        proto="tcp",
        nmap_args=["-sT", "-Pn", "-n", "--scan-delay", "60s", "--max-retries", "0"],
    ),
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run an nmap scan inside the Docker lab network and write matching "
            "ground-truth JSONL labels."
        ),
    )
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--scenario-id", required=True)
    parser.add_argument(
        "--scan-type",
        required=True,
        choices=tuple(SCENARIOS),
        help="Dataset subtype traffic pattern to generate.",
    )
    parser.add_argument(
        "--targets",
        required=True,
        help="Comma-separated Docker lab service names.",
    )
    parser.add_argument(
        "--ports",
        required=True,
        help="Comma-separated destination ports.",
    )
    parser.add_argument("--network", default=None, help="Docker network name. Auto-detected when omitted.")
    parser.add_argument("--compose-service", default="camera-app")
    parser.add_argument("--nmap-image", default=DEFAULT_NMAP_IMAGE)
    parser.add_argument("--label", default="scanning")
    parser.add_argument("--technique-id", default="T1046")
    parser.add_argument("--source", default="nmap-scanner")
    parser.add_argument("--scenario-log-root", type=Path, default=Path("data/scenarios"))
    parser.add_argument("--output-root", type=Path, default=Path("data/nmap"))
    parser.add_argument(
        "--allowed-target",
        action="append",
        default=[],
        help="Additional allowed target. Can be repeated.",
    )
    parser.add_argument(
        "--extra-nmap-arg",
        action="append",
        default=[],
        help="Additional nmap argument. Can be repeated; keep this lab-only.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print command and write no labels.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    scenario = SCENARIOS[args.scan_type]
    targets = parse_csv(args.targets)
    ports = parse_ports(args.ports)
    validate_targets(targets, allowed_targets=DEFAULT_ALLOWED_TARGETS | set(args.allowed_target))
    network = args.network or detect_compose_network(args.compose_service)

    output_dir = args.output_root / args.run_id
    output_path = output_dir / "nmap-output.txt"
    command = build_docker_nmap_command(
        image=args.nmap_image,
        network=network,
        scenario=scenario,
        ports=ports,
        targets=targets,
        extra_nmap_args=args.extra_nmap_arg,
    )

    print(" ".join(command))
    if args.dry_run:
        return

    output_dir.mkdir(parents=True, exist_ok=True)
    started_at = utc_now()
    result = subprocess.run(command, text=True, capture_output=True, check=False)
    ended_at = utc_now()
    output_path.write_text(
        result.stdout + ("\n[stderr]\n" + result.stderr if result.stderr else ""),
        encoding="utf-8",
    )

    write_event(
        args.scenario_log_root / "events.jsonl",
        event_type="nmap.scenario.start",
        scenario_id=args.scenario_id,
        run_id=args.run_id,
        phase=scenario.phase,
        label=args.label,
        technique_id=args.technique_id,
        source=args.source,
        result="started",
        timestamp=started_at,
        details={
            "scan_type": args.scan_type,
            "targets": targets,
            "ports": ports,
            "proto": scenario.proto,
            "network": network,
            "nmap_image": args.nmap_image,
            "command": command,
        },
    )
    for target in targets:
        for port in ports:
            write_event(
                args.scenario_log_root / "ground-truth.jsonl",
                event_type="nmap.scenario.attempt",
                scenario_id=args.scenario_id,
                run_id=args.run_id,
                phase=scenario.phase,
                label=args.label,
                technique_id=args.technique_id,
                source=args.source,
                target=target,
                port=port,
                proto=scenario.proto,
                result="attempted",
                timestamp=started_at,
                window_start=started_at,
                window_end=ended_at,
                details={
                    "scan_type": args.scan_type,
                    "nmap_returncode": result.returncode,
                    "nmap_output": str(output_path),
                },
            )
    write_event(
        args.scenario_log_root / "events.jsonl",
        event_type="nmap.scenario.stop",
        scenario_id=args.scenario_id,
        run_id=args.run_id,
        phase=scenario.phase,
        label=args.label,
        technique_id=args.technique_id,
        source=args.source,
        result="completed" if result.returncode == 0 else "error",
        timestamp=ended_at,
        details={
            "scan_type": args.scan_type,
            "returncode": result.returncode,
            "output": str(output_path),
        },
    )

    if result.returncode != 0:
        print(result.stderr, file=sys.stderr)
        raise SystemExit(result.returncode)

    print(f"wrote nmap output to {output_path}")
    print(f"wrote ground truth for {len(targets) * len(ports)} target:port pairs")


def build_docker_nmap_command(
    *,
    image: str,
    network: str,
    scenario: Scenario,
    ports: list[int],
    targets: list[str],
    extra_nmap_args: list[str],
) -> list[str]:
    return [
        "docker",
        "run",
        "--rm",
        "--network",
        network,
        image,
        *scenario.nmap_args,
        *extra_nmap_args,
        "-p",
        ",".join(str(port) for port in ports),
        *targets,
    ]


def detect_compose_network(service: str) -> str:
    container_id = run_text(["docker", "compose", "ps", "-q", service]).strip()
    if not container_id:
        raise SystemExit(f"could not find compose container for service: {service}")

    networks = run_text(
        [
            "docker",
            "inspect",
            "-f",
            "{{range $name, $_ := .NetworkSettings.Networks}}{{println $name}}{{end}}",
            container_id,
        ]
    )
    network_names = [line.strip() for line in networks.splitlines() if line.strip()]
    if not network_names:
        raise SystemExit(f"could not detect Docker network for container: {container_id}")
    return network_names[0]


def run_text(command: list[str]) -> str:
    result = subprocess.run(command, text=True, capture_output=True, check=False)
    if result.returncode != 0:
        raise SystemExit(result.stderr.strip() or f"command failed: {' '.join(command)}")
    return result.stdout


def write_event(
    path: Path,
    *,
    event_type: str,
    scenario_id: str,
    run_id: str,
    phase: str,
    label: str,
    technique_id: str,
    source: str,
    result: str,
    timestamp: str,
    target: str | None = None,
    port: int | None = None,
    proto: str | None = None,
    window_start: str | None = None,
    window_end: str | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    record = compact(
        {
            "schema_version": SCHEMA_VERSION,
            "timestamp": timestamp,
            "service": "nmap-scenario",
            "event_type": event_type,
            "scenario_id": scenario_id,
            "run_id": run_id,
            "technique_id": technique_id,
            "phase": phase,
            "label": label,
            "source": source,
            "target": target,
            "port": port,
            "proto": proto,
            "result": result,
            "window_start": window_start,
            "window_end": window_end,
            "details": details or {},
        }
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as file:
        file.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def parse_csv(raw_value: str) -> list[str]:
    values = [value.strip() for value in raw_value.split(",") if value.strip()]
    if not values:
        raise SystemExit("expected at least one value")
    return values


def parse_ports(raw_value: str) -> list[int]:
    ports: list[int] = []
    for value in parse_csv(raw_value):
        try:
            port = int(value)
        except ValueError as exc:
            raise SystemExit(f"invalid port: {value}") from exc
        if port < 1 or port > 65535:
            raise SystemExit(f"invalid port: {port}")
        ports.append(port)
    return ports


def validate_targets(targets: list[str], *, allowed_targets: set[str]) -> None:
    invalid = [target for target in targets if target not in allowed_targets]
    if invalid:
        allowed = ", ".join(sorted(allowed_targets))
        raise SystemExit(f"target not allowed for lab scan: {invalid}. allowed: {allowed}")


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def compact(record: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in record.items() if value is not None}


if __name__ == "__main__":
    main()
