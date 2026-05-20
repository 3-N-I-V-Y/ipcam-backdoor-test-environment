# Infected Scan Scenario

`infected_scan` simulates an already-compromised camera performing low-and-slow
TCP service discovery against explicitly configured lab targets.

It is disabled by default. Enable it with:

```bash
export SCENARIO_ID=infected-scan
export SCENARIO_RUN_ID=infected-scan-001
export CAMERA_APP_LAB_MODE=infected_scan

docker compose up -d --build --force-recreate
```

By default, the camera scans only these Docker service names:

```text
control-server,nvr-console,mediamtx
```

and these TCP ports:

```text
22,23,80,443,554,8554,8080,8090,8091,8888
```

The worker tries one `target:port` pair per interval. The default interval is
60 seconds, which creates a low-and-slow pattern.

## Useful Settings

```bash
export CAMERA_APP_SCAN_TARGETS=control-server,nvr-console,mediamtx
export CAMERA_APP_SCAN_PORTS=22,23,80,443,554,8554,8080,8090,8091,8888
export CAMERA_APP_SCAN_INTERVAL_SECONDS=60
export CAMERA_APP_SCAN_TIMEOUT_SECONDS=1
export CAMERA_APP_SCAN_STARTUP_DELAY_SECONDS=10
export CAMERA_APP_SCAN_MAX_ATTEMPTS=0
```

Set `CAMERA_APP_SCAN_MAX_ATTEMPTS` to a small number for short validation runs:

```bash
export CAMERA_APP_SCAN_MAX_ATTEMPTS=10
```

## Safety Controls

External targets are blocked by default:

```bash
export CAMERA_APP_SCAN_BLOCK_EXTERNAL=true
```

With this enabled, targets must resolve to private, loopback, or link-local
addresses. CIDR ranges and wildcards are rejected.

## Logged Events

Each attempt writes to both:

```text
data/scenarios/events.jsonl
data/scenarios/ground-truth.jsonl
```

Event fields include:

```text
event_type = camera.infected_scan.attempt
phase = low_and_slow_scan
label = attack
technique_id = T1046
target
port
proto = tcp
result = open | closed | timeout | blocked | error
```

These labels are used by `scripts/build_window_features.py` to mark matching
Zeek windows as attack windows.
