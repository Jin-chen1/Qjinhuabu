"""Opt-in spatial crop for composed outputs; input assets remain untouched."""

import json
import math
import subprocess
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from novelvideo.task_backend.subprocesses import run_project_subprocess


class CompositionCrop(BaseModel):
    bottom_fraction: float = Field(default=0.04, ge=0, lt=0.5)
    top_fraction: float = Field(default=0, ge=0, lt=0.5)
    aspect_ratio: str = Field(default="9:16", pattern=r"^9:16$")
    fit_mode: Literal["crop", "blurred_padding"] = "crop"

    @model_validator(mode="after")
    def usable_height(self) -> "CompositionCrop":
        if self.top_fraction + self.bottom_fraction >= 0.5:
            raise ValueError("Composition crop must retain more than half the frame")
        return self


def crop_geometry(
    width: int, height: int, crop: CompositionCrop,
) -> tuple[int, int, int, int]:
    # Multiples of 18x32 give an exact 9:16 rectangle with even codec dimensions.
    top = math.ceil(height * crop.top_fraction / 2) * 2
    bottom = math.ceil(height * crop.bottom_fraction / 2) * 2
    if crop.fit_mode == "blurred_padding":
        return 0, top, width // 2 * 2, (height - top - bottom) // 2 * 2
    unit = int(min(width / 18, (height - top - bottom) / 32))
    if unit < 1:
        raise ValueError("Composition crop leaves no usable portrait rectangle")
    w, h = unit * 18, unit * 32
    return (width - w) // 2, top, w, h


def apply_composition_crop(source: Path, crop: CompositionCrop) -> Path:
    metadata = json.loads(subprocess.check_output([
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height", "-of", "json", str(source),
    ], timeout=30))
    stream = metadata["streams"][0]
    width, height = int(stream["width"]), int(stream["height"])
    x, y, w, h = crop_geometry(width, height, crop)
    output_unit = min(width // 18, height // 32)
    output = source.with_name(f"{source.stem}_cropped.mp4")
    output_width, output_height = output_unit * 18, output_unit * 32
    if crop.fit_mode == "blurred_padding":
        filters = [
            "-filter_complex",
            f"[0:v]crop={w}:{h}:{x}:{y},split[fg][bg];"
            f"[bg]scale={output_width}:{output_height}:force_original_aspect_ratio=increase,"
            f"crop={output_width}:{output_height},boxblur=24:2[back];"
            f"[fg]scale={output_width}:{output_height}:force_original_aspect_ratio=decrease[front];"
            "[back][front]overlay=(W-w)/2:(H-h)/2,setsar=1[v]",
            "-map", "[v]",
        ]
    else:
        filters = [
            "-map", "0:v:0", "-vf",
            f"crop={w}:{h}:{x}:{y},scale={output_width}:{output_height},setsar=1",
        ]
    try:
        result = run_project_subprocess([
            "ffmpeg", "-v", "error", "-nostdin", "-y", "-i", str(source),
            *filters, "-map", "0:a?", "-map_metadata", "0",
            "-c:v", "libx264", "-crf", "18", "-preset", "medium",
            "-pix_fmt", "yuv420p", "-vsync", "0",
            "-c:a", "copy", "-movflags", "+faststart", str(output),
        ], capture_output=True, text=True, timeout=30 * 60)
        if result.returncode:
            raise RuntimeError(f"Composition crop failed: {result.stderr}")
    except BaseException:
        output.unlink(missing_ok=True)
        raise
    return output
