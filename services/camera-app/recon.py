from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import ipaddress
import logging
import socket
import threading
import time
from typing import Literal

from common.scenario_logger import ScenarioLogger
from state import CameraState


ScanResult = Literal["open", "closed", "timeout", "blocked", "error"]


@dataclass(frozen=True, slots=True)
class InfectedScanConfig:
    enabled: bool
    camera_id: str
    targets: tuple[str, ...]
    ports: tuple[int, ...]
    interval_seconds: float = 60.0
    connect_timeout_seconds: float = 1.0
    startup_delay_seconds: float = 10.0
    block_external: bool = True
    max_attempts: int = 0
    phase: str = "low_and_slow_scan"
    label: str = "scanning"
    technique_id: str = "T1046"


class InfectedScanWorker:
    def __init__(
        self,
        *,
        config: InfectedScanConfig,
        state: CameraState,
        scenario_logger: ScenarioLogger,
        logger: logging.Logger | None = None,
    ) -> None:
        self._config = config
        self._state = state
        self._scenario_logger = scenario_logger
        self._logger = logger or logging.getLogger(__name__)
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._attempt_index = 0

    def start(self) -> None:
        self._state.configure_infected_scan(
            enabled=self._config.enabled,
            targets=list(self._config.targets),
            ports=list(self._config.ports),
            interval_seconds=self._config.interval_seconds,
            connect_timeout_seconds=self._config.connect_timeout_seconds,
            block_external=self._config.block_external,
        )

        if not self._config.enabled:
            self._logger.info("infected scan worker disabled")
            return

        if not self._config.targets or not self._config.ports:
            self._logger.warning("infected scan worker enabled with no targets or ports")
            self._state.mark_infected_scan_stopped(error="no scan targets or ports configured")
            return

        if self._thread and self._thread.is_alive():
            return

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop,
            name="camera-infected-scan",
            daemon=True,
        )
        self._thread.start()

        self._scenario_logger.event(
            event_type="camera.infected_scan.start",
            phase=self._config.phase,
            label=self._config.label,
            technique_id=self._config.technique_id,
            camera_id=self._config.camera_id,
            source=self._config.camera_id,
            result="started",
            details={
                "targets": list(self._config.targets),
                "ports": list(self._config.ports),
                "interval_seconds": self._config.interval_seconds,
                "connect_timeout_seconds": self._config.connect_timeout_seconds,
                "block_external": self._config.block_external,
                "max_attempts": self._config.max_attempts,
            },
        )

    def stop(self, timeout: float = 5.0) -> None:
        if not self._config.enabled:
            return

        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=timeout)
        self._state.mark_infected_scan_stopped()
        self._scenario_logger.event(
            event_type="camera.infected_scan.stop",
            phase=self._config.phase,
            label=self._config.label,
            technique_id=self._config.technique_id,
            camera_id=self._config.camera_id,
            source=self._config.camera_id,
            result="stopped",
            details={"attempt_count": self._attempt_index},
        )

    def _run_loop(self) -> None:
        if self._config.startup_delay_seconds > 0:
            if self._stop_event.wait(self._config.startup_delay_seconds):
                return

        while not self._stop_event.is_set():
            if self._config.max_attempts > 0 and self._attempt_index >= self._config.max_attempts:
                self._logger.info("infected scan worker reached max_attempts=%s", self._config.max_attempts)
                break

            target, port = self._next_target_port()
            self._scan_once(target=target, port=port)

            if self._stop_event.wait(self._config.interval_seconds):
                break

    def _next_target_port(self) -> tuple[str, int]:
        pairs_count = len(self._config.targets) * len(self._config.ports)
        pair_index = self._attempt_index % pairs_count
        target_index = pair_index // len(self._config.ports)
        port_index = pair_index % len(self._config.ports)
        self._attempt_index += 1
        return self._config.targets[target_index], self._config.ports[port_index]

    def _scan_once(self, *, target: str, port: int) -> None:
        started_at = time.monotonic()
        timestamp = utc_now()
        self._state.mark_infected_scan_attempt(target=target, port=port)

        allowed, resolved_ips, safety_error = self._target_allowed(target)
        if not allowed:
            elapsed_ms = elapsed_milliseconds(started_at)
            details = {
                "target": target,
                "port": port,
                "elapsed_ms": elapsed_ms,
                "error": safety_error,
                "resolved_ips": resolved_ips,
            }
            self._state.mark_infected_scan_result(
                target=target,
                port=port,
                result="blocked",
                elapsed_ms=elapsed_ms,
                error=safety_error,
            )
            self._log_scan_result(
                timestamp=timestamp,
                target=target,
                port=port,
                result="blocked",
                details=details,
            )
            return

        result: ScanResult
        error_message: str | None = None
        try:
            with socket.create_connection(
                (target, port),
                timeout=self._config.connect_timeout_seconds,
            ):
                result = "open"
        except ConnectionRefusedError:
            result = "closed"
        except TimeoutError:
            result = "timeout"
            error_message = "connection timed out"
        except OSError as exc:
            if "timed out" in str(exc).lower():
                result = "timeout"
            else:
                result = "error"
            error_message = str(exc)

        elapsed_ms = elapsed_milliseconds(started_at)
        self._state.mark_infected_scan_result(
            target=target,
            port=port,
            result=result,
            elapsed_ms=elapsed_ms,
            error=error_message,
        )
        self._log_scan_result(
            timestamp=timestamp,
            target=target,
            port=port,
            result=result,
            details={
                "target": target,
                "port": port,
                "elapsed_ms": elapsed_ms,
                "error": error_message,
                "resolved_ips": resolved_ips,
            },
        )

    def _target_allowed(self, target: str) -> tuple[bool, list[str], str | None]:
        if not self._config.block_external:
            return True, [], None

        if "/" in target or "*" in target:
            return False, [], "CIDR ranges and wildcards are not allowed"

        try:
            parsed_ip = ipaddress.ip_address(target)
        except ValueError:
            parsed_ip = None

        if parsed_ip is not None:
            return self._ip_allowed(parsed_ip), [str(parsed_ip)], self._ip_block_reason(parsed_ip)

        try:
            resolved = socket.getaddrinfo(target, None, type=socket.SOCK_STREAM)
        except socket.gaierror as exc:
            return False, [], f"target resolution failed: {exc}"

        resolved_ips = sorted({item[4][0] for item in resolved})
        if not resolved_ips:
            return False, [], "target did not resolve to an address"

        parsed_ips: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
        for value in resolved_ips:
            try:
                parsed_ips.append(ipaddress.ip_address(value))
            except ValueError:
                return False, resolved_ips, f"resolved address is not an IP: {value}"

        blocked = [ip for ip in parsed_ips if not self._ip_allowed(ip)]
        if blocked:
            return False, resolved_ips, f"target resolved to blocked address: {blocked[0]}"

        return True, resolved_ips, None

    def _ip_allowed(self, value: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
        return bool(value.is_private or value.is_loopback or value.is_link_local)

    def _ip_block_reason(self, value: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str | None:
        if self._ip_allowed(value):
            return None
        return f"external target blocked: {value}"

    def _log_scan_result(
        self,
        *,
        timestamp: str,
        target: str,
        port: int,
        result: ScanResult,
        details: dict,
    ) -> None:
        self._scenario_logger.event(
            event_type="camera.infected_scan.attempt",
            phase=self._config.phase,
            label=self._config.label,
            technique_id=self._config.technique_id,
            camera_id=self._config.camera_id,
            source=self._config.camera_id,
            target=target,
            port=port,
            proto="tcp",
            result=result,
            details=details,
            timestamp=timestamp,
        )
        self._scenario_logger.ground_truth(
            event_type="camera.infected_scan.attempt",
            phase=self._config.phase,
            label=self._config.label,
            technique_id=self._config.technique_id,
            camera_id=self._config.camera_id,
            source=self._config.camera_id,
            target=target,
            port=port,
            proto="tcp",
            result=result,
            details=details,
            timestamp=timestamp,
        )


def elapsed_milliseconds(started_at: float) -> int:
    return round((time.monotonic() - started_at) * 1000)


def utc_now() -> str:
    return datetime.now(UTC).isoformat()
