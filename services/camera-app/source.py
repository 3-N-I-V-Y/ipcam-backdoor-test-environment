from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol


class CameraSource(Protocol):
    kind: str
    uri: str

    def validate(self) -> None:
        ...

    def ffmpeg_input_args(self) -> list[str]:
        ...

    def ffmpeg_codec_args(self) -> list[str]:
        ...

    def state_fields(self) -> dict[str, Any]:
        ...


@dataclass(slots=True)
class FileSource:
    path: str
    kind: str = "file"

    @property
    def uri(self) -> str:
        return self.path

    def validate(self) -> None:
        input_path = Path(self.path)
        if not input_path.exists():
            raise FileNotFoundError(f"file source not found: {self.path}")
        if not input_path.is_file():
            raise ValueError(f"file source is not a regular file: {self.path}")

    def ffmpeg_input_args(self) -> list[str]:
        return [
            "-re",
            "-stream_loop",
            "-1",
            "-i",
            self.path,
        ]

    def ffmpeg_codec_args(self) -> list[str]:
        return ["-c", "copy"]

    def state_fields(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "uri": self.uri,
        }


@dataclass(slots=True)
class WebcamSource:
    device: str
    backend: str = "v4l2"
    framerate: int = 30
    resolution: str = "1280x720"
    input_format: str | None = None
    kind: str = "webcam"

    @property
    def uri(self) -> str:
        return self.device

    def validate(self) -> None:
        if self.framerate <= 0:
            raise ValueError("webcam framerate must be greater than 0")

        if "x" not in self.resolution:
            raise ValueError("webcam resolution must look like WIDTHxHEIGHT")

        if self.backend == "v4l2":
            device_path = Path(self.device)
            if not device_path.exists():
                raise FileNotFoundError(f"webcam device not found: {self.device}")
            return

        if self.backend == "dshow":
            if not self.device.strip():
                raise ValueError("webcam device name is required for dshow")
            return

        raise ValueError(f"unsupported webcam backend: {self.backend}")

    def ffmpeg_input_args(self) -> list[str]:
        args = ["-f", self.backend]

        if self.framerate > 0:
            args.extend(["-framerate", str(self.framerate)])

        if self.resolution:
            args.extend(["-video_size", self.resolution])

        if self.input_format:
            if self.backend == "v4l2":
                args.extend(["-input_format", self.input_format])
            elif self.backend == "dshow":
                args.extend(["-pixel_format", self.input_format])

        if self.backend == "dshow":
            args.extend(["-i", f"video={self.device}"])
        else:
            args.extend(["-i", self.device])
        return args

    def ffmpeg_codec_args(self) -> list[str]:
        return [
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-tune",
            "zerolatency",
            "-pix_fmt",
            "yuv420p",
            "-g",
            str(max(self.framerate, 1)),
        ]

    def state_fields(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "uri": self.uri,
            "backend": self.backend,
            "framerate": self.framerate,
            "resolution": self.resolution,
            "input_format": self.input_format,
        }


def create_camera_source(
    *,
    source_type: str,
    file_path: str,
    webcam_device: str,
    webcam_backend: str,
    webcam_framerate: int,
    webcam_resolution: str,
    webcam_input_format: str | None,
) -> CameraSource:
    normalized = source_type.strip().lower()

    if normalized == "file":
        return FileSource(path=file_path)

    if normalized == "webcam":
        return WebcamSource(
            device=webcam_device,
            backend=webcam_backend,
            framerate=webcam_framerate,
            resolution=webcam_resolution,
            input_format=webcam_input_format,
        )

    raise ValueError("SOURCE_TYPE must be one of: file, webcam")
