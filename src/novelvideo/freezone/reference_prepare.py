"""Local, model-free reference extraction; never mutates canvas state."""

from __future__ import annotations

import hashlib
import json
import math
import shutil
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, Field

from novelvideo.freezone.paths import outputs_dir, resolve_static_url_to_path


class ReferencePrepareRequest(BaseModel):
    video_url: str = Field(min_length=1)
    canvas_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    node_id: str = Field(min_length=1, max_length=256)


class ReferencePrepareResponse(BaseModel):
    first_frame_url: str
    audio_url: str | None = Field(
        description="Complete original audio track as PCM WAV; null means the source has no audio track."
    )
    duration_ms: int
    width: int
    height: int
    source_sha256: str


@dataclass(frozen=True)
class PreparedReference:
    first_frame_path: Path
    audio_path: Path | None
    duration_ms: int
    width: int
    height: int
    source_sha256: str


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if result.returncode:
        raise ValueError("Unable to probe or extract source video")
    return result


def prepare_reference(project_dir: Path, video_url: str) -> PreparedReference:
    """Resolve through the existing media boundary and extract in a worker thread."""
    source = resolve_static_url_to_path(video_url, project_dir)
    if not source.is_file():
        raise FileNotFoundError("Source video not found")
    probe = _run([
        "ffprobe", "-v", "error", "-show_entries",
        "stream=codec_type,width,height:format=duration", "-of", "json", str(source),
    ])
    try:
        metadata = json.loads(probe.stdout)
        streams = metadata["streams"]
        video = next(s for s in streams if s.get("codec_type") == "video")
        width, height = int(video["width"]), int(video["height"])
        duration = float(metadata["format"]["duration"])
        if width <= 0 or height <= 0 or not math.isfinite(duration) or duration <= 0:
            raise ValueError
        duration_ms = round(duration * 1000)
        if duration_ms <= 0:
            raise ValueError
        has_audio = any(s.get("codec_type") == "audio" for s in streams)
    except (KeyError, StopIteration, TypeError, ValueError) as exc:
        raise ValueError("Source has invalid video metadata") from exc

    digest = hashlib.sha256()
    with source.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)

    directory = outputs_dir(project_dir, "reference_prepare") / str(uuid.uuid4())
    directory.mkdir(parents=True, exist_ok=False)
    first_frame = directory / "first_frame.png"
    audio = directory / "audio.wav" if has_audio else None
    try:
        _run([
            "ffmpeg", "-v", "error", "-nostdin", "-y", "-noautorotate",
            "-i", str(source), "-ss", "0", "-map", "0:v:0",
            "-frames:v", "1", "-an", str(first_frame),
        ])
        if not first_frame.is_file() or first_frame.stat().st_size == 0:
            raise ValueError("Source video has no decodable first frame")
        if audio is not None:
            # No duration cap, separation model, silence synthesis, or video output.
            _run([
                "ffmpeg", "-v", "error", "-nostdin", "-y", "-i", str(source),
                "-map", "0:a:0", "-vn", "-c:a", "pcm_s16le", str(audio),
            ])
            if not audio.is_file() or audio.stat().st_size == 0:
                raise ValueError("Unable to extract source audio track")
        return PreparedReference(
            first_frame, audio, duration_ms, width, height, digest.hexdigest(),
        )
    except BaseException:
        shutil.rmtree(directory)
        raise
