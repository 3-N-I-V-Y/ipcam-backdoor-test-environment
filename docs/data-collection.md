# Data Collection

Each dataset run should produce this bundle:

```text
SCENARIO_RUN_ID
data/pcap/<run_id>.pcap
data/zeek/<run_id>/conn.log
data/scenarios/ground-truth.jsonl entries
scan_subtype labels derived from ground-truth phase
```

Keep all generated traffic inside the Docker lab network.

## 1) Start the Lab

Set a run id and scenario id before starting the containers:

```bash
export SCENARIO_RUN_ID=vertical-scan-001
export SCENARIO_ID=vertical-scan
export SCENARIO_LOG_ENABLED=true

docker compose up -d --build
```

## 2) Find the Docker Bridge

On a Linux Docker host:

```bash
CAMERA_CID=$(docker compose ps -q camera-app)
NET=$(docker inspect -f '{{range $name, $_ := .NetworkSettings.Networks}}{{println $name}}{{end}}' "$CAMERA_CID" | head -n 1)
BR=$(docker network inspect -f '{{ index .Options "com.docker.network.bridge.name" }}' "$NET")
if [ -z "$BR" ]; then
  BR="br-$(docker network inspect -f '{{.Id}}' "$NET" | cut -c1-12)"
fi
echo "$BR"
```

If Docker Desktop does not expose the bridge interface on the host, capture from
the `camera-app` network namespace or run packet capture inside the Docker VM.

## 3) Capture Packets

Run this in one terminal and leave it running while the scenario generates
traffic:

```bash
mkdir -p data/pcap

sudo timeout 1800 tcpdump -i "$BR" -nn -s 0 -U \
  -w "data/pcap/${SCENARIO_RUN_ID}.pcap" \
  'tcp or udp'
```

## 4) Generate Subtype Traffic

You can generate subtype traffic either with the built-in Python generator or
with nmap. For model data, prefer one subtype per run.

### Nmap Wrapper

`scripts/run_nmap_scenario.py` runs nmap in the Docker lab network and writes
matching ground-truth labels to `data/scenarios/ground-truth.jsonl`.

The wrapper only allows lab service names by default:

```text
camera-app
control-server
mediamtx
nvr-console
```

It also stores raw nmap output under:

```text
data/nmap/<run_id>/nmap-output.txt
```

For Docker Desktop, prefer `scripts/collect_nmap_run.py`. It starts a tcpdump
sidecar in the `camera-app` network namespace, runs nmap in that same namespace,
then writes:

```text
data/pcap/<run_id>.pcap
data/nmap/<run_id>/nmap-output.txt
data/scenarios/ground-truth.jsonl
```

Example:

```bash
python3 scripts/collect_nmap_run.py \
  --run-id vertical-scan-001 \
  --scenario-id vertical-scan \
  --scan-type vertical \
  --targets nvr-console \
  --ports 22,23,80,443,554,8554,8080,8090,8091,8888
```

### Baseline

No scan traffic. Keep services running long enough to collect normal control,
RTSP/HLS, and NVR traffic:

```bash
export SCENARIO_RUN_ID=baseline-001
export SCENARIO_ID=baseline
export CAMERA_APP_LAB_MODE=none

docker compose up -d --build --force-recreate
sleep 600
```

### Low and Slow Scan

Use the built-in camera scanner:

```bash
export SCENARIO_RUN_ID=low-and-slow-001
export SCENARIO_ID=low-and-slow
export CAMERA_APP_LAB_MODE=infected_scan
export CAMERA_APP_SCAN_PHASE=low_and_slow_scan
export CAMERA_APP_SCAN_TARGETS=control-server,nvr-console,mediamtx
export CAMERA_APP_SCAN_PORTS=22,23,80,443,554,8554,8080,8090,8091,8888
export CAMERA_APP_SCAN_INTERVAL_SECONDS=60
export CAMERA_APP_SCAN_MAX_ATTEMPTS=30

docker compose up -d --build --force-recreate
```

### Vertical Scan

One source scans many ports on one target:

```bash
export SCENARIO_RUN_ID=vertical-scan-001
export SCENARIO_ID=vertical-scan
export CAMERA_APP_LAB_MODE=none

docker compose up -d --build --force-recreate

docker compose exec camera-app python scenario_traffic.py \
  --mode vertical_scan \
  --targets nvr-console \
  --ports 22,23,80,443,554,8554,8080,8090,8091,8888 \
  --interval-seconds 1 \
  --timeout-seconds 1
```

Nmap equivalent:

```bash
python3 scripts/run_nmap_scenario.py \
  --run-id vertical-scan-001 \
  --scenario-id vertical-scan \
  --scan-type vertical \
  --targets nvr-console \
  --ports 22,23,80,443,554,8554,8080,8090,8091,8888
```

### Horizontal Scan

One source scans the same port across many targets. Keep the port set small so
the run is clearly horizontal rather than mixed horizontal/vertical:

```bash
export SCENARIO_RUN_ID=horizontal-scan-001
export SCENARIO_ID=horizontal-scan
export CAMERA_APP_LAB_MODE=none

docker compose up -d --build --force-recreate

docker compose exec camera-app python scenario_traffic.py \
  --mode horizontal_scan \
  --targets control-server,nvr-console,mediamtx,camera-app \
  --ports 80 \
  --interval-seconds 1 \
  --timeout-seconds 1
```

Nmap equivalent:

```bash
python3 scripts/run_nmap_scenario.py \
  --run-id horizontal-scan-001 \
  --scenario-id horizontal-scan \
  --scan-type horizontal \
  --targets control-server,nvr-console,mediamtx,camera-app \
  --ports 80
```

### Service Probe

Connect and send light service-identification requests such as HTTP `GET` or
RTSP `OPTIONS`:

```bash
export SCENARIO_RUN_ID=service-probe-001
export SCENARIO_ID=service-probe
export CAMERA_APP_LAB_MODE=none

docker compose up -d --build --force-recreate

docker compose exec camera-app python scenario_traffic.py \
  --mode service_probe \
  --targets control-server,nvr-console,mediamtx,camera-app \
  --ports 80,8080,8090,8091,8554,8888 \
  --interval-seconds 1 \
  --timeout-seconds 1
```

Nmap equivalent:

```bash
python3 scripts/run_nmap_scenario.py \
  --run-id service-probe-001 \
  --scenario-id service-probe \
  --scan-type service-probe \
  --targets control-server,nvr-console,mediamtx,camera-app \
  --ports 80,8080,8090,8091,8554,8888
```

### UDP Scan

Send small UDP payloads to lab targets:

```bash
export SCENARIO_RUN_ID=udp-scan-001
export SCENARIO_ID=udp-scan
export CAMERA_APP_LAB_MODE=none

docker compose up -d --build --force-recreate

docker compose exec camera-app python scenario_traffic.py \
  --mode udp_scan \
  --targets mediamtx,control-server,nvr-console \
  --ports 53,123,161,8000,8001 \
  --interval-seconds 1 \
  --timeout-seconds 1
```

Nmap equivalent:

```bash
python3 scripts/run_nmap_scenario.py \
  --run-id udp-scan-001 \
  --scenario-id udp-scan \
  --scan-type udp \
  --targets mediamtx,control-server,nvr-console \
  --ports 53,123,161,8000,8001
```

### Low and Slow with Nmap

Use nmap scan delay to spread attempts over time:

```bash
python3 scripts/run_nmap_scenario.py \
  --run-id low-and-slow-001 \
  --scenario-id low-and-slow \
  --scan-type low-and-slow \
  --targets control-server,nvr-console,mediamtx \
  --ports 22,23,80,443,554,8554,8080,8090,8091,8888
```

`scenario_traffic.py` blocks non-private, non-loopback, and non-link-local
targets by default.

## 5) Convert PCAP to Zeek

After `tcpdump` exits:

```bash
mkdir -p "data/zeek/${SCENARIO_RUN_ID}"

zeek -C -r "data/pcap/${SCENARIO_RUN_ID}.pcap" \
  Log::default_logdir="data/zeek/${SCENARIO_RUN_ID}"
```

If Zeek is not installed on the host, use Docker:

```bash
python3 scripts/run_zeek_pcap.py --run-id "${SCENARIO_RUN_ID}"
```

Check:

```bash
test -s "data/zeek/${SCENARIO_RUN_ID}/conn.log"
grep "\"run_id\":\"${SCENARIO_RUN_ID}\"" data/scenarios/ground-truth.jsonl | head
```

## 6) Repeat Runs

Collect at least two runs per subtype:

```text
baseline-001, baseline-002, baseline-003
low-and-slow-001, low-and-slow-002
vertical-scan-001, vertical-scan-002
horizontal-scan-001, horizontal-scan-002
service-probe-001, service-probe-002
udp-scan-001, udp-scan-002
```

## 7) Build the Dataset

```bash
python3 scripts/build_scan_dataset.py \
  --zeek-root data/zeek \
  --features-root data/features/windowed \
  --ground-truth data/scenarios/ground-truth.jsonl \
  --target-column scan_subtype \
  --test-run baseline-003 \
  --test-run low-and-slow-002 \
  --test-run vertical-scan-002 \
  --test-run horizontal-scan-002 \
  --test-run service-probe-002 \
  --test-run udp-scan-002 \
  --output data/features/datasets/ipcam-scan-subtype-60s.csv \
  --window-seconds 60
```

## 8) Check Label Distribution

```bash
python3 - <<'PY'
import csv
from collections import Counter

path = "data/features/datasets/ipcam-scan-subtype-60s.csv"
with open(path, newline="", encoding="utf-8") as f:
    rows = csv.DictReader(f)
    print(Counter(row["scan_subtype"] for row in rows))
PY
```

You should see `normal`, `low_and_slow_scan`, `vertical_scan`,
`horizontal_scan`, `service_probe`, and `udp_scan`.
