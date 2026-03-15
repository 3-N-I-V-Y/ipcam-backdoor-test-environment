# Phase 1 Architecture

This repository now supports a more realistic IP camera baseline before any adversary scenarios are added.

## Service Topology

```text
camera-app
  -> publishes RTSP to MediaMTX
  -> exposes /health and /status
  -> keeps a primary control channel to control-server
  -> sends normal beacon / poll traffic on that primary channel

MediaMTX
  -> serves RTSP on port 8554
  -> can expose HLS on port 8888 for browser-oriented clients

nvr-console
  -> administrator sign-in page
  -> camera inventory and detail views
  -> normal control-plane view and safe task UI
  -> continuous recording worker using FFmpeg segment files
  -> recording archive and audit log UI

control-server
  -> normal control-plane for beacon, task, and result traffic
  -> backing API for NVR operator actions
```

## Phase 1 Goals

- Stand up a believable IP camera stack with management, control, and archive views.
- Keep the normal path stable: `camera -> control-server + MediaMTX -> NVR`.
- Treat the normal control path as baseline, not as a lab-only feature flag.
- Persist baseline artifacts:
  - NVR metadata in SQLite
  - recording segments on disk
  - operator and system events in audit logs

## Key Ports

- `8080`: control-server
- `8090`: camera-app API
- `8091`: NVR console
- `8554`: RTSP publish and playback
- `8888`: HLS output from MediaMTX

## Phase 2 Hook Points

Later scenario work should extend the baseline rather than replace it:

- add rogue control or attacker services beside the normal `control-server`
- add abnormal egress destinations beside `MediaMTX`
- attach packet capture and ground-truth tagging around the existing baseline

This keeps NDR validation focused on the delta between normal and abnormal behavior.
