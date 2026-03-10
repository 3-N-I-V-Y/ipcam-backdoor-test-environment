#!/bin/sh
set -eu

RTSP_URL="${RTSP_URL:-rtsp://mediamtx:8554/cam1}"
CONTROL_URL="${CONTROL_URL:-http://control-server:8080}"
VIDEO_FILE="${VIDEO_FILE:-/samples/demo.mp4}"

echo "[mock-camera] waiting for services..."
sleep 3

echo "[mock-camera] start beacon loop..."
(
  while true; do
    wget -qO- \
      --header="Content-Type: application/json" \
      --post-data='{"camera_id":"mock-cam-001","stream_state":"publishing"}' \
      "${CONTROL_URL}/beacon" >/dev/null 2>&1 || true
    sleep 10
  done
) &

echo "[mock-camera] start RTSP publish..."
exec ffmpeg -re -stream_loop -1 \
  -i "${VIDEO_FILE}" \
  -c copy \
  -f rtsp "${RTSP_URL}"