# Scan Dataset Build

Use this after collecting multiple baseline and infected-scan runs and converting
each pcap to Zeek `conn.log`.

The recommended experiment-stage window is 60 seconds. This produces enough
samples for quick model checks while preserving low-and-slow behavior.

## Build All Available Runs

```bash
python3 scripts/build_scan_dataset.py \
  --zeek-root data/zeek \
  --features-root data/features/windowed \
  --ground-truth data/scenarios/ground-truth.jsonl \
  --output data/features/datasets/ipcam-scan-dataset-60s.csv \
  --window-seconds 60
```

The script discovers every `data/zeek/<run_id>/conn.log`, rebuilds 60-second
window features, and merges only these labels:

```text
normal
scanning
```

For compatibility with older `infected_scan` runs, `attack` labels are mapped to
`scanning` by default.

## Explicit Runs

```bash
python3 scripts/build_scan_dataset.py \
  --run baseline-002:baseline:normal \
  --run baseline-003:baseline:normal \
  --run infected-scan-001:infected-scan:normal \
  --run infected-scan-002:infected-scan:normal \
  --output data/features/datasets/ipcam-scan-dataset-60s.csv \
  --window-seconds 60
```

For infected-scan runs, `default_label=normal` means windows without scan ground
truth stay normal, while windows overlapping `ground-truth.jsonl` scan attempts
become `scanning`.

## Run-Based Split

Prefer a run-based split over random splitting:

```bash
python3 scripts/build_scan_dataset.py \
  --run baseline-002:baseline:normal \
  --run baseline-003:baseline:normal \
  --run infected-scan-001:infected-scan:normal \
  --run infected-scan-002:infected-scan:normal \
  --test-run baseline-003 \
  --test-run infected-scan-002 \
  --output data/features/datasets/ipcam-scan-dataset-60s.csv \
  --window-seconds 60
```

This also writes:

```text
data/features/datasets/ipcam-scan-dataset-60s-train.csv
data/features/datasets/ipcam-scan-dataset-60s-test.csv
```

## Check Label Counts

```bash
cut -d, -f5 data/features/datasets/ipcam-scan-dataset-60s.csv | sort | uniq -c
```
