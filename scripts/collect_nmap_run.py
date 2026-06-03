from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import sys
import time


DEFAULT_TCPDUMP_IMAGE = "nicolaka/netshoot:latest"
DEFAULT_CAPTURE_FILTER = "tcp or (udp and not port 8000 and not port 8001)"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Capture a pcap while running an nmap scenario from the camera-app "
            "network namespace."
        ),
    )
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--scenario-id", required=True)
    parser.add_argument(
        "--scan-type",
        required=True,
        choices=("vertical", "horizontal", "service-probe", "udp", "low-and-slow"),
    )
    parser.add_argument("--targets", required=True)
    parser.add_argument("--ports", required=True)
    parser.add_argument("--compose-service", default="camera-app")
    parser.add_argument("--pcap-root", type=Path, default=Path("data/pcap"))
    parser.add_argument("--tcpdump-image", default=DEFAULT_TCPDUMP_IMAGE)
    parser.add_argument("--nmap-image", default="instrumentisto/nmap:latest")
    parser.add_argument("--scenario-log-root", type=Path, default=Path("data/scenarios"))
    parser.add_argument("--capture-interface", default="any")
    parser.add_argument("--capture-filter", default=DEFAULT_CAPTURE_FILTER)
    parser.add_argument(
        "--capture-network-mode",
        choices=("host", "container"),
        default="host",
        help=(
            "Where to run the tcpdump sidecar. host works reliably on Docker "
            "Desktop/WSL while nmap still runs from the camera-app namespace; "
            "container keeps the older same-namespace capture mode."
        ),
    )
    parser.add_argument("--startup-wait-seconds", type=float, default=2.0)
    parser.add_argument(
        "--post-scan-wait-seconds",
        type=float,
        default=1.0,
        help="Wait after nmap exits before stopping tcpdump so short bursts flush to pcap.",
    )
    parser.add_argument(
        "--repeat-until-seconds",
        type=float,
        default=0.0,
        help=(
            "Keep tcpdump running and repeat the nmap scenario until this many seconds "
            "have elapsed. The default 0 runs a single nmap attempt."
        ),
    )
    parser.add_argument(
        "--repeat-interval-seconds",
        type=float,
        default=10.0,
        help="Sleep between repeated nmap attempts when --repeat-until-seconds is set.",
    )
    parser.add_argument(
        "--max-scan-repeats",
        type=int,
        default=0,
        help="Optional cap for repeated nmap attempts. 0 means no cap.",
    )
    parser.add_argument("--extra-nmap-arg", action="append", default=[])
    parser.add_argument(
        "--skip-pull",
        action="store_true",
        help="Do not pre-pull tcpdump/nmap images before starting capture.",
    )
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    container_id = compose_container_id(args.compose_service)
    if not container_id:
        raise SystemExit(
            f"missing compose service container: {args.compose_service}. "
            "Run docker compose up -d first."
        )

    pcap_root = args.pcap_root.resolve()
    pcap_root.mkdir(parents=True, exist_ok=True)
    pcap_path = pcap_root / f"{args.run_id}.pcap"
    tcpdump_name = safe_container_name(f"ipcam-tcpdump-{args.run_id}")
    scanner_network = f"container:{container_id}"
    capture_network = scanner_network
    capture_filter = args.capture_filter
    if args.capture_network_mode == "host":
        capture_network = "host"
        container_ip = compose_container_ip(args.compose_service)
        if container_ip:
            capture_filter = f"({capture_filter}) and host {container_ip}"

    tcpdump_command = [
        "docker",
        "run",
        "--rm",
        "--name",
        tcpdump_name,
        "--network",
        capture_network,
        "-v",
        f"{pcap_root}:/pcap",
        args.tcpdump_image,
        "tcpdump",
        "-i",
        args.capture_interface,
        "-nn",
        "-s",
        "0",
        "-U",
        "-w",
        f"/pcap/{args.run_id}.pcap",
        capture_filter,
    ]
    nmap_command = [
        sys.executable,
        str(Path(__file__).with_name("run_nmap_scenario.py")),
        "--run-id",
        args.run_id,
        "--scenario-id",
        args.scenario_id,
        "--scan-type",
        args.scan_type,
        "--targets",
        args.targets,
        "--ports",
        args.ports,
        "--network",
        scanner_network,
        "--nmap-image",
        args.nmap_image,
        "--source",
        args.compose_service,
        "--scenario-log-root",
        str(args.scenario_log_root),
    ]
    for extra_arg in args.extra_nmap_arg:
        nmap_command.extend(["--extra-nmap-arg", extra_arg])

    print("tcpdump:", " ".join(tcpdump_command))
    print("nmap:", " ".join(nmap_command))
    if args.dry_run:
        return

    if not args.skip_pull:
        pull_image(args.tcpdump_image)
        pull_image(args.nmap_image)

    cleanup_stale_container(tcpdump_name)
    tcpdump = subprocess.Popen(tcpdump_command)
    try:
        time.sleep(args.startup_wait_seconds)
        result = run_nmap_attempts(args, nmap_command)
        if args.post_scan_wait_seconds > 0:
            time.sleep(args.post_scan_wait_seconds)
    finally:
        subprocess.run(["docker", "stop", tcpdump_name], check=False, capture_output=True, text=True)
        try:
            tcpdump.wait(timeout=30)
        except subprocess.TimeoutExpired:
            tcpdump.kill()

    if result.returncode != 0:
        raise SystemExit(result.returncode)
    print(f"wrote pcap to {pcap_path}")


def run_nmap_attempts(args: argparse.Namespace, base_command: list[str]) -> subprocess.CompletedProcess[bytes]:
    if args.repeat_until_seconds <= 0:
        return subprocess.run(base_command, check=False)

    deadline = time.monotonic() + args.repeat_until_seconds
    attempt_index = 0
    last_result: subprocess.CompletedProcess[bytes] | None = None

    while time.monotonic() < deadline:
        attempt_index += 1
        command = [*base_command, "--attempt-index", str(attempt_index)]
        print(f"nmap attempt {attempt_index}:", " ".join(command))
        last_result = subprocess.run(command, check=False)
        if last_result.returncode != 0:
            return last_result
        if args.max_scan_repeats and attempt_index >= args.max_scan_repeats:
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(args.repeat_interval_seconds, remaining))

    if last_result is None:
        return subprocess.run([*base_command, "--attempt-index", "1"], check=False)
    return last_result


def compose_container_id(service: str) -> str:
    result = subprocess.run(
        ["docker", "compose", "ps", "-q", service],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise SystemExit(result.stderr.strip() or "docker compose ps failed")
    return result.stdout.strip()


def compose_container_ip(service: str) -> str:
    container_id = compose_container_id(service)
    result = subprocess.run(
        [
            "docker",
            "inspect",
            "-f",
            "{{range .NetworkSettings.Networks}}{{println .IPAddress}}{{end}}",
            container_id,
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise SystemExit(result.stderr.strip() or f"docker inspect failed for {service}")
    return next((line.strip() for line in result.stdout.splitlines() if line.strip()), "")


def cleanup_stale_container(name: str) -> None:
    subprocess.run(["docker", "rm", "-f", name], check=False, capture_output=True, text=True)


def pull_image(image: str) -> None:
    print(f"pulling image: {image}")
    subprocess.run(["docker", "pull", image], check=True)


def safe_container_name(value: str) -> str:
    return "".join(char if char.isalnum() or char in {"-", "_"} else "-" for char in value)


if __name__ == "__main__":
    main()
