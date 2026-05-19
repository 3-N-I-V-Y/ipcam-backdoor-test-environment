# Scenario Logging

Scenario events and ground-truth labels are written as JSONL files under:

```text
data/scenarios/events.jsonl
data/scenarios/ground-truth.jsonl
```

The files are mounted into participating containers at `/data/scenarios`.

## Environment

```text
SCENARIO_LOG_ENABLED=true
SCENARIO_LOG_ROOT=/data/scenarios
SCENARIO_ID=baseline
SCENARIO_RUN_ID=manual-run
```

Use a shared `SCENARIO_RUN_ID` for every service in one experiment run.

## Event Fields

Common fields:

```text
schema_version
timestamp
service
event_type
scenario_id
run_id
technique_id
phase
label
camera_id
source
target
port
proto
result
details
```

Raw IP addresses can be kept in raw flow logs for aggregation, but they should not
be used directly as model input. Convert them to behavior features such as
`unique_dst_count_5m` and `new_dst_count_1h`.

## Ground Truth

`ground-truth.jsonl` is only written when a scenario record has both `label` and
`phase`. Normal baseline telemetry can still appear in `events.jsonl` without a
label.
