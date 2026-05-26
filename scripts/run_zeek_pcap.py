from __future__ import annotations

import argparse
from pathlib import Path
import subprocess


DEFAULT_ZEEK_IMAGE = "zeek/zeek:latest"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Convert data/pcap/<run_id>.pcap to Zeek logs with Docker.",
    )
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--data-root", type=Path, default=Path("data"))
    parser.add_argument("--zeek-image", default=DEFAULT_ZEEK_IMAGE)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    data_root = args.data_root.resolve()
    pcap_path = data_root / "pcap" / f"{args.run_id}.pcap"
    zeek_dir = data_root / "zeek" / args.run_id
    if not pcap_path.exists():
        raise SystemExit(f"missing pcap: {pcap_path}")

    zeek_dir.mkdir(parents=True, exist_ok=True)
    command = [
        "docker",
        "run",
        "--rm",
        "-v",
        f"{data_root}:/data",
        args.zeek_image,
        "zeek",
        "-C",
        "-r",
        f"/data/pcap/{args.run_id}.pcap",
        f"Log::default_logdir=/data/zeek/{args.run_id}",
    ]
    print(" ".join(command))
    subprocess.run(command, check=True)
    print(f"wrote Zeek logs to {zeek_dir}")


if __name__ == "__main__":
    main()
