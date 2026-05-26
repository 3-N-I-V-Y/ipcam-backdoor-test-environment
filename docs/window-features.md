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

The script groups flows by `src_ip + window_start`, but it does not write raw
`src_ip` or `dst_ip` into the model dataset. Raw addresses are used only during
aggregation. The output contains `src_entity`, a stable hash for grouping one
source's windows into sequences.

Important output columns:

```text
src_entity
scan_subtype
flow_count
unique_dst_count
unique_dst_port_count
top_dst_port_ratio
dst_port_entropy
proto_entropy
service_entropy
unique_service_count
empty_service_ratio
conn_state_entropy
failed_conn_ratio
s0_ratio
rej_ratio
rst_ratio
established_conn_ratio
syn_only_ratio
short_flow_ratio
small_response_ratio
zero_dst_bytes_ratio
bytes_out_in_ratio
pkts_out_in_ratio
inter_flow_time_mean
inter_flow_time_std
```
