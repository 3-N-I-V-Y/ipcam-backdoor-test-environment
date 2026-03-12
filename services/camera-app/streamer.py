from __future__ import annotations

from dataclasses import dataclass
import logging
import subprocess
import threading
import time

from source import CameraSource
from state import CameraState


@dataclass(slots=True)
class StreamerConfig:
    source: CameraSource
    rtsp_url: str
    rtsp_transport: str = "tcp"
    ffmpeg_binary: str = "ffmpeg"
    restart_delay_seconds: float = 3.0
    publish_probe_seconds: float = 1.5


class StreamerSupervisor:
    def __init__(
        self,
        *,
        config: StreamerConfig,
        state: CameraState,
        logger: logging.Logger | None = None,
    ) -> None:
        self._config = config
        self._state = state
        self._logger = logger or logging.getLogger(__name__)
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._process_lock = threading.Lock()
        self._process: subprocess.Popen[bytes] | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop,
            name="camera-streamer",
            daemon=True,
        )
        self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        self._stop_event.set()
        self._terminate_process()

        if self._thread:
            self._thread.join(timeout=timeout)

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._config.source.validate()
            except (OSError, ValueError) as exc:
                error = str(exc)
                self._logger.error(error)
                self._state.mark_stream_retrying(exit_code=None, error=error)
                self._wait_before_retry()
                continue

            command = self._build_command()
            self._logger.info("starting ffmpeg publisher")
            self._logger.debug("ffmpeg command: %s", " ".join(command))

            try:
                process = subprocess.Popen(command)
            except OSError as exc:
                error = f"failed to start ffmpeg: {exc}"
                self._logger.exception(error)
                self._state.mark_stream_retrying(exit_code=None, error=error)
                self._wait_before_retry()
                continue

            with self._process_lock:
                self._process = process

            self._state.mark_stream_starting(process.pid)
            time.sleep(self._config.publish_probe_seconds)

            if self._stop_event.is_set():
                self._terminate_process()
                break

            if process.poll() is None:
                self._state.mark_stream_publishing(process.pid)

            exit_code = process.wait()
            with self._process_lock:
                self._process = None

            if self._stop_event.is_set():
                self._state.mark_stream_stopped(
                    exit_code=exit_code,
                    error="shutdown requested",
                )
                break

            error = f"ffmpeg exited with code {exit_code}"
            self._logger.warning(error)
            self._state.mark_stream_retrying(exit_code=exit_code, error=error)
            self._wait_before_retry()

        if not self._stop_event.is_set():
            self._state.mark_stream_stopped(exit_code=None)

    def _build_command(self) -> list[str]:
        return [
            self._config.ffmpeg_binary,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "warning",
            *self._config.source.ffmpeg_input_args(),
            *self._config.source.ffmpeg_codec_args(),
            "-rtsp_transport",
            self._config.rtsp_transport,
            "-f",
            "rtsp",
            self._config.rtsp_url,
        ]

    def _terminate_process(self) -> None:
        with self._process_lock:
            process = self._process

        if process is None or process.poll() is not None:
            return

        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._logger.warning("ffmpeg did not stop in time, killing process")
            process.kill()
            process.wait(timeout=5)

    def _wait_before_retry(self) -> None:
        deadline = time.monotonic() + self._config.restart_delay_seconds
        while not self._stop_event.is_set() and time.monotonic() < deadline:
            time.sleep(0.2)
