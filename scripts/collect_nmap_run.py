from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import sys
import time


DEFAULT_TCPDUMP_IMAGE = "nicolaka/netshoot:latest"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Capture a pcap from the camera-app network namespace while running "
            "an nmap scenario in the same namespace."
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
    parser.add_argument("--capture-interface", default="any")
    parser.add_argument("--capture-filter", default="tcp or udp")
    parser.add_argument("--startup-wait-seconds", type=float, default=2.0)
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
    network = f"container:{container_id}"

    tcpdump_command = [
        "docker",
        "run",
        "--rm",
        "--name",
        tcpdump_name,
        "--network",
        network,
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
        args.capture_filter,
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
        network,
        "--nmap-image",
        args.nmap_image,
        "--source",
        args.compose_service,
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
        result = subprocess.run(nmap_command, check=False)
    finally:
        subprocess.run(["docker", "stop", tcpdump_name], check=False, capture_output=True, text=True)
        try:
            tcpdump.wait(timeout=30)
        except subprocess.TimeoutExpired:
            tcpdump.kill()

    if result.returncode != 0:
        raise SystemExit(result.returncode)
    print(f"wrote pcap to {pcap_path}")


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


def cleanup_stale_container(name: str) -> None:
    subprocess.run(["docker", "rm", "-f", name], check=False, capture_output=True, text=True)


def pull_image(image: str) -> None:
    print(f"pulling image: {image}")
    subprocess.run(["docker", "pull", image], check=True)


def safe_container_name(value: str) -> str:
    return "".join(char if char.isalnum() or char in {"-", "_"} else "-" for char in value)


if __name__ == "__main__":
    main()
