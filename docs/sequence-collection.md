# 10s Sequence Collection

This pipeline collects long-running Docker-lab traffic for GRU/LSTM sequence
experiments. It keeps Zeek in Docker, writes run-level checkpoints, rebuilds the
10-second feature dataset, creates sequence NPZ files, and writes a quality
report.

Use it only against this defensive lab environment. The collector runs nmap
inside the Docker lab network and should not be pointed at external networks.

## Files

```text
configs/sequence_collection_10s_24h.json
scripts/collect_sequence_dataset.py
scripts/build_sequence_report.py
deploy/systemd/ndr-seq-collector.service
deploy/systemd/ndr-seq-collector.timer
```

The default config writes:

```text
data/features/datasets/seq10s-24h-scan-subtype-10s.csv
data/features/datasets/seq10s-24h-scan-subtype-10s-train.csv
data/features/datasets/seq10s-24h-scan-subtype-10s-test.csv
data/features/sequences/seq10s-24h-n12-train.npz
data/features/sequences/seq10s-24h-n12-test.npz
data/features/sequences/seq10s-24h-n24-train.npz
data/features/sequences/seq10s-24h-n24-test.npz
data/features/sequences/seq10s-24h-sequence-report.json
data/features/sequences/seq10s-24h-sequence-report.md
data/features/sequences/seq10s-24h-state.json
```

## Plan

Estimate the run plan without Docker access:

```bash
python3 scripts/collect_sequence_dataset.py plan \
  --config configs/sequence_collection_10s_24h.json
```

This writes:

```text
data/features/datasets/seq10s-24h-sequence-plan.json
data/features/datasets/seq10s-24h-sequence-plan.md
```

The estimate is duration-based. The final sequence count depends on how many
active `src_entity` groups produce Zeek windows during each run.

## Run

Start or continue the collection:

```bash
python3 scripts/collect_sequence_dataset.py run \
  --config configs/sequence_collection_10s_24h.json \
  --finalize
```

Resume after a reboot or failed run:

```bash
python3 scripts/collect_sequence_dataset.py resume \
  --config configs/sequence_collection_10s_24h.json \
  --retry-failed \
  --finalize
```

Check status:

```bash
python3 scripts/collect_sequence_dataset.py status \
  --config configs/sequence_collection_10s_24h.json
```

The state file records each run as `pending`, `capturing`, `pcap_done`,
`zeek_done`, `skipped_existing`, or `failed`. Existing Zeek `conn.log` files are
reused when `skip_existing=true`.

## Finalize Only

If pcaps and Zeek logs already exist, rebuild the dataset and sequence artifacts
without collecting more traffic:

```bash
python3 scripts/collect_sequence_dataset.py finalize \
  --config configs/sequence_collection_10s_24h.json
```

To train the configured RNN model after sequence creation:

```bash
python3 scripts/collect_sequence_dataset.py finalize \
  --config configs/sequence_collection_10s_24h.json \
  --train
```

Set `training.rnn_type` to `gru` or `lstm` in the config. Training is disabled
by default so long collection can finish without consuming GPU/CPU unexpectedly.

## Quality Gates

`scripts/build_sequence_report.py` checks:

- total/train/test sequence counts for each sequence length;
- train/test `run_id` overlap;
- label and scan subtype distribution;
- scenario and run dominance;
- raw identity columns in CSVs;
- raw identity features in sequence metadata;
- NPZ shape and class counts.

The 10,000 sequence gate is a collection target, not a production guarantee.
Sliding sequences overlap heavily, so final model claims must still use
run-separated real test data. Synthetic data may be useful for pipeline
validation, but it must not be merged into real-test performance claims.

## systemd User Service

Install the user units on a Linux server:

```bash
mkdir -p ~/.config/systemd/user
cp deploy/systemd/ndr-seq-collector.service ~/.config/systemd/user/
cp deploy/systemd/ndr-seq-collector.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now ndr-seq-collector.timer
```

If the server should continue user services after logout:

```bash
loginctl enable-linger "$USER"
```

Monitor logs:

```bash
journalctl --user -u ndr-seq-collector -f
```

Stop the timer:

```bash
systemctl --user disable --now ndr-seq-collector.timer
```

The service assumes the repository is checked out at:

```text
%h/workspace/ipcam-backdoor-test-environment
```

Edit `WorkingDirectory` in
`deploy/systemd/ndr-seq-collector.service` if the server uses another path.
