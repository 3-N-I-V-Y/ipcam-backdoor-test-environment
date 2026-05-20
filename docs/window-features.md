# Window Feature Generation

Use this after Zeek has created `conn.log` for one experiment run.

```bash
export SCENARIO_RUN_ID=baseline-001

python3 scripts/build_window_features.py \
  --conn-log "data/zeek/${SCENARIO_RUN_ID}/conn.log" \
  --output "data/features/windowed/${SCENARIO_RUN_ID}.csv" \
  --scenario-id baseline \
  --run-id "${SCENARIO_RUN_ID}" \
  --window-seconds 60
```

For attack runs, pass the scenario ground-truth file as well:

```bash
python3 scripts/build_window_features.py \
  --conn-log "data/zeek/${SCENARIO_RUN_ID}/conn.log" \
  --output "data/features/windowed/${SCENARIO_RUN_ID}.csv" \
  --scenario-id infected-recon \
  --run-id "${SCENARIO_RUN_ID}" \
  --ground-truth data/scenarios/ground-truth.jsonl \
  --window-seconds 60
```

The script does not write raw `src_ip` or `dst_ip` into the model dataset. It uses
them only to create behavior features such as `unique_dst_count`.

Important output columns:

```text
flow_count
unique_dst_count
unique_dst_port_count
dst_port_entropy
proto_entropy
short_flow_ratio
small_response_ratio
zero_dst_bytes_ratio
bytes_out_in_ratio
pkts_out_in_ratio
inter_flow_time_mean
inter_flow_time_std
```
