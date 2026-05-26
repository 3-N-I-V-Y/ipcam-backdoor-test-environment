from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import UTC, datetime
import ipaddress
import os
from pathlib import Path
import socket
import sys
import time
from typing import Literal

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.scenario_logger import ScenarioLogger


Mode = Literal["vertical_scan", "horizontal_scan", "service_probe", "udp_scan"]
Result = Literal["open", "closed", "timeout", "blocked", "error", "sent", "sent_no_response"]


@dataclass(frozen=True, slots=True)
class Attempt:
    target: str
    port: int
    proto: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate lab-only scan subtype traffic and ground-truth labels.",
    )
    parser.add_argument(
        "--mode",
        required=True,
        choices=("vertical_scan", "horizontal_scan", "service_probe", "udp_scan"),
    )
    parser.add_argument(
        "--targets",
        required=True,
        help="Comma-separated lab targets, e.g. control-server,nvr-console,mediamtx",
    )
    parser.add_argument(
        "--ports",
        required=True,
        help="Comma-separated destination ports, e.g. 22,23,80,443,554,8554",
    )
    parser.add_argument("--interval-seconds", type=float, default=1.0)
    parser.add_argument("--timeout-seconds", type=float, default=1.0)
    parser.add_argument("--max-attempts", type=int, default=0)
    parser.add_argument("--camera-id", default=os.getenv("CAMERA_ID", "camera-app-001"))
    parser.add_argument("--label", default="scanning")
    parser.add_argument("--phase", default=None)
    parser.add_argument("--technique-id", default="T1046")
    parser.add_argument(
        "--allow-external",
        action="store_true",
        help="Allow non-private targets. Keep disabled for dataset collection.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    mode: Mode = args.mode
    targets = parse_csv(args.targets)
    ports = parse_ports(args.ports)
    attempts = build_attempts(mode=mode, targets=targets, ports=ports)
    if args.max_attempts > 0:
        attempts = attempts[: args.max_attempts]
    if not attempts:
        raise SystemExit("no attempts to run")

    phase = args.phase or mode
    logger = ScenarioLogger.from_env(service_name="camera-app")
    logger.event(
        event_type="camera.scenario_traffic.start",
        phase=phase,
        label=args.label,
        technique_id=args.technique_id,
        camera_id=args.camera_id,
        source=args.camera_id,
        result="started",
        details={
            "mode": mode,
            "targets": targets,
            "ports": ports,
            "interval_seconds": args.interval_seconds,
            "timeout_seconds": args.timeout_seconds,
            "max_attempts": args.max_attempts,
            "allow_external": args.allow_external,
        },
    )

    for index, attempt in enumerate(attempts):
        timestamp = utc_now()
        allowed, resolved_ips, safety_error = target_allowed(
            attempt.target,
            allow_external=args.allow_external,
        )
        if not allowed:
            result: Result = "blocked"
            details = {
                "mode": mode,
                "attempt_index": index,
                "target": attempt.target,
                "port": attempt.port,
                "proto": attempt.proto,
                "error": safety_error,
                "resolved_ips": resolved_ips,
            }
        elif mode == "service_probe":
            result, details = service_probe(
                attempt.target,
                attempt.port,
                timeout_seconds=args.timeout_seconds,
            )
            details.update({"mode": mode, "attempt_index": index, "resolved_ips": resolved_ips})
        elif mode == "udp_scan":
            result, details = udp_probe(
                attempt.target,
                attempt.port,
                timeout_seconds=args.timeout_seconds,
            )
            details.update({"mode": mode, "attempt_index": index, "resolved_ips": resolved_ips})
        else:
            result, details = tcp_connect_probe(
                attempt.target,
                attempt.port,
                timeout_seconds=args.timeout_seconds,
            )
            details.update({"mode": mode, "attempt_index": index, "resolved_ips": resolved_ips})

        log_attempt(
            logger,
            event_type="camera.scenario_traffic.attempt",
            phase=phase,
            label=args.label,
            technique_id=args.technique_id,
            camera_id=args.camera_id,
            target=attempt.target,
            port=attempt.port,
            proto=attempt.proto,
            result=result,
            details=details,
            timestamp=timestamp,
        )
        print(
            f"{timestamp} mode={mode} target={attempt.target} "
            f"port={attempt.port}/{attempt.proto} result={result}"
        )

        if index + 1 < len(attempts) and args.interval_seconds > 0:
            time.sleep(args.interval_seconds)

    logger.event(
        event_type="camera.scenario_traffic.stop",
        phase=phase,
        label=args.label,
        technique_id=args.technique_id,
        camera_id=args.camera_id,
        source=args.camera_id,
        result="stopped",
        details={"mode": mode, "attempt_count": len(attempts)},
    )


def build_attempts(*, mode: Mode, targets: list[str], ports: list[int]) -> list[Attempt]:
    proto = "udp" if mode == "udp_scan" else "tcp"
    if mode == "horizontal_scan":
        return [
            Attempt(target=target, port=port, proto=proto)
            for port in ports
            for target in targets
        ]
    return [
        Attempt(target=target, port=port, proto=proto)
        for target in targets
        for port in ports
    ]


def tcp_connect_probe(target: str, port: int, *, timeout_seconds: float) -> tuple[Result, dict]:
    started_at = time.monotonic()
    try:
        with socket.create_connection((target, port), timeout=timeout_seconds):
            return "open", {
                "target": target,
                "port": port,
                "elapsed_ms": elapsed_milliseconds(started_at),
            }
    except ConnectionRefusedError:
        return "closed", {
            "target": target,
            "port": port,
            "elapsed_ms": elapsed_milliseconds(started_at),
        }
    except TimeoutError:
        return "timeout", {
            "target": target,
            "port": port,
            "elapsed_ms": elapsed_milliseconds(started_at),
            "error": "connection timed out",
        }
    except OSError as exc:
        result: Result = "timeout" if "timed out" in str(exc).lower() else "error"
        return result, {
            "target": target,
            "port": port,
            "elapsed_ms": elapsed_milliseconds(started_at),
            "error": str(exc),
        }


def service_probe(target: str, port: int, *, timeout_seconds: float) -> tuple[Result, dict]:
    started_at = time.monotonic()
    payload = service_payload(target, port)
    try:
        with socket.create_connection((target, port), timeout=timeout_seconds) as sock:
            sock.settimeout(timeout_seconds)
            if payload:
                sock.sendall(payload)
            response = read_some(sock)
            return "open", {
                "target": target,
                "port": port,
                "elapsed_ms": elapsed_milliseconds(started_at),
                "probe_bytes": len(payload),
                "response_bytes": len(response),
                "response_preview": response[:80].decode("utf-8", errors="replace"),
            }
    except ConnectionRefusedError:
        return "closed", {
            "target": target,
            "port": port,
            "elapsed_ms": elapsed_milliseconds(started_at),
            "probe_bytes": len(payload),
        }
    except TimeoutError:
        return "timeout", {
            "target": target,
            "port": port,
            "elapsed_ms": elapsed_milliseconds(started_at),
            "probe_bytes": len(payload),
            "error": "service probe timed out",
        }
    except OSError as exc:
        result: Result = "timeout" if "timed out" in str(exc).lower() else "error"
        return result, {
            "target": target,
            "port": port,
            "elapsed_ms": elapsed_milliseconds(started_at),
            "probe_bytes": len(payload),
            "error": str(exc),
        }


def udp_probe(target: str, port: int, *, timeout_seconds: float) -> tuple[Result, dict]:
    started_at = time.monotonic()
    payload = udp_payload(port)
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(timeout_seconds)
            sock.sendto(payload, (target, port))
            try:
                response, _ = sock.recvfrom(512)
            except TimeoutError:
                return "sent_no_response", {
                    "target": target,
                    "port": port,
                    "elapsed_ms": elapsed_milliseconds(started_at),
                    "probe_bytes": len(payload),
                    "response_bytes": 0,
                }
            return "sent", {
                "target": target,
                "port": port,
                "elapsed_ms": elapsed_milliseconds(started_at),
                "probe_bytes": len(payload),
                "response_bytes": len(response),
            }
    except OSError as exc:
        return "error", {
            "target": target,
            "port": port,
            "elapsed_ms": elapsed_milliseconds(started_at),
            "probe_bytes": len(payload),
            "error": str(exc),
        }


def service_payload(target: str, port: int) -> bytes:
    if port in {80, 443, 8080, 8090, 8091, 8888}:
        return (
            f"GET /health HTTP/1.1\r\nHost: {target}\r\n"
            "User-Agent: ipcam-lab-service-probe\r\nConnection: close\r\n\r\n"
        ).encode("ascii")
    if port in {554, 8554}:
        return (
            f"OPTIONS rtsp://{target}:{port}/cam1 RTSP/1.0\r\n"
            "CSeq: 1\r\nUser-Agent: ipcam-lab-service-probe\r\n\r\n"
        ).encode("ascii")
    return b""


def udp_payload(port: int) -> bytes:
    if port == 53:
        return b"\x12\x34\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00\x03lab\x05local\x00\x00\x01\x00\x01"
    if port == 123:
        return b"\x1b" + (b"\x00" * 47)
    return b"ipcam-lab-udp-probe"


def read_some(sock: socket.socket) -> bytes:
    try:
        return sock.recv(512)
    except TimeoutError:
        return b""
    except OSError:
        return b""


def log_attempt(
    logger: ScenarioLogger,
    *,
    event_type: str,
    phase: str,
    label: str,
    technique_id: str,
    camera_id: str,
    target: str,
    port: int,
    proto: str,
    result: Result,
    details: dict,
    timestamp: str,
) -> None:
    logger.event(
        event_type=event_type,
        phase=phase,
        label=label,
        technique_id=technique_id,
        camera_id=camera_id,
        source=camera_id,
        target=target,
        port=port,
        proto=proto,
        result=result,
        details=details,
        timestamp=timestamp,
    )
    logger.ground_truth(
        event_type=event_type,
        phase=phase,
        label=label,
        technique_id=technique_id,
        camera_id=camera_id,
        source=camera_id,
        target=target,
        port=port,
        proto=proto,
        result=result,
        details=details,
        timestamp=timestamp,
    )


def target_allowed(target: str, *, allow_external: bool) -> tuple[bool, list[str], str | None]:
    if allow_external:
        return True, [], None

    if "/" in target or "*" in target:
        return False, [], "CIDR ranges and wildcards are not allowed"

    try:
        parsed_ip = ipaddress.ip_address(target)
    except ValueError:
        parsed_ip = None

    if parsed_ip is not None:
        return ip_allowed(parsed_ip), [str(parsed_ip)], ip_block_reason(parsed_ip)

    try:
        resolved = socket.getaddrinfo(target, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        return False, [], f"target resolution failed: {exc}"

    resolved_ips = sorted({item[4][0] for item in resolved})
    parsed_ips: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for value in resolved_ips:
        try:
            parsed_ips.append(ipaddress.ip_address(value))
        except ValueError:
            return False, resolved_ips, f"resolved address is not an IP: {value}"

    blocked = [value for value in parsed_ips if not ip_allowed(value)]
    if blocked:
        return False, resolved_ips, f"target resolved to blocked address: {blocked[0]}"
    return True, resolved_ips, None


def ip_allowed(value: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return bool(value.is_private or value.is_loopback or value.is_link_local)


def ip_block_reason(value: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str | None:
    if ip_allowed(value):
        return None
    return f"external target blocked: {value}"


def parse_csv(raw_value: str) -> list[str]:
    return [value.strip() for value in raw_value.split(",") if value.strip()]


def parse_ports(raw_value: str) -> list[int]:
    ports = [int(value.strip()) for value in raw_value.split(",") if value.strip()]
    invalid = [port for port in ports if port < 1 or port > 65535]
    if invalid:
        raise SystemExit(f"invalid ports: {invalid}")
    return ports


def elapsed_milliseconds(started_at: float) -> int:
    return round((time.monotonic() - started_at) * 1000)


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


if __name__ == "__main__":
    main()
