import asyncio
from array import array
import json
import math
import shutil
import subprocess

import pytest

from novelvideo.freezone.jobs import run_freezone_video_compose
from novelvideo.freezone.composition_crop import CompositionCrop, apply_composition_crop


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg required")
def test_single_clip_5057ms_24fps_stays_within_one_frame(tmp_path):
    source = tmp_path / "source.mp4"
    subprocess.run([
        "ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=180x320:r=24",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100",
        "-t", "5.057", "-c:v", "libx264", "-c:a", "aac", str(source),
    ], check=True)
    output = asyncio.run(run_freezone_video_compose(
        project_dir=tmp_path, job_id="fixed-duration", resolution="480p", fps=24,
        keep_original_audio=True,
        tracks=[{"kind": "video", "items": [{
            "source_path": str(source), "source_start": 0, "source_end": 5.057,
            "timeline_start": 0, "speed": 1, "volume": 1,
        }]}],
    ))
    cropped = apply_composition_crop(output, CompositionCrop(
        top_fraction=0.12, bottom_fraction=0.08, fit_mode="blurred_padding",
    ))
    counts = []
    for path in (output, cropped):
        probe = json.loads(subprocess.check_output([
            "ffprobe", "-v", "error", "-count_frames", "-show_streams",
            "-show_format", "-of", "json", str(path),
        ]))
        assert abs(float(probe["format"]["duration"]) - 5.057) <= 1 / 24
        video = next(s for s in probe["streams"] if s["codec_type"] == "video")
        assert video["r_frame_rate"] == "24/1"
        counts.append(int(video["nb_read_frames"]))
    assert counts[0] == counts[1]


def _decode_audio(path):
    return array("f", subprocess.check_output([
        "ffmpeg", "-v", "error", "-i", str(path), "-vn",
        "-ar", "48000", "-ac", "2", "-f", "f32le", "-",
    ]))


@pytest.mark.skipif(
    not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
    reason="ffmpeg and ffprobe required",
)
@pytest.mark.parametrize("audio_duration", [5.057, 6.0])
def test_external_audio_fixed_duration_preserves_tail_without_extending_video(
    tmp_path, audio_duration,
):
    target = 5.057
    source = tmp_path / "silent.mp4"
    audio = tmp_path / "original.wav"
    subprocess.run([
        "ffmpeg", "-v", "error", "-f", "lavfi", "-i",
        "testsrc2=s=180x320:r=24", "-t", str(target),
        "-c:v", "libx264", str(source),
    ], check=True)
    subprocess.run([
        "ffmpeg", "-v", "error", "-f", "lavfi", "-i",
        "sine=frequency=440:sample_rate=48000", "-t", str(audio_duration),
        "-c:a", "pcm_s16le", "-ac", "2", str(audio),
    ], check=True)
    output = asyncio.run(run_freezone_video_compose(
        project_dir=tmp_path, job_id="external-tail", resolution="480p", fps=24,
        keep_original_audio=False,
        tracks=[
            {"kind": "video", "items": [{
                "source_path": str(source), "source_end": target,
            }]},
            {"kind": "audio", "items": [{
                "source_path": str(audio), "source_end": audio_duration,
            }]},
        ],
    ))
    probe = json.loads(subprocess.check_output([
        "ffprobe", "-v", "error", "-count_frames", "-show_streams",
        "-show_format", "-of", "json", str(output),
    ]))
    video = next(s for s in probe["streams"] if s["codec_type"] == "video")
    sound = next(s for s in probe["streams"] if s["codec_type"] == "audio")
    assert target <= float(sound["duration"]) <= target + 0.001
    assert int(video["nb_read_frames"]) == math.ceil(target * 24)
    assert float(video["duration"]) == pytest.approx(122 / 24, abs=0.000001)
    assert float(probe["format"]["duration"]) <= 122 / 24 + 0.001
    tail = _decode_audio(output)[int(5.034 * 48000) * 2:int(target * 48000) * 2]
    assert len(tail) == (int(target * 48000) - int(5.034 * 48000)) * 2
    assert sum(x * x for x in tail) / len(tail) > 0.001


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg required")
@pytest.mark.parametrize("volume", [1.0, 0.4])
def test_external_audio_preserves_requested_gain(tmp_path, volume):
    source = tmp_path / "silent.mp4"
    audio = tmp_path / "original.wav"
    subprocess.run([
        "ffmpeg", "-v", "error", "-f", "lavfi", "-i",
        "testsrc2=s=180x320:r=24", "-t", "2",
        "-c:v", "libx264", str(source),
    ], check=True)
    subprocess.run([
        "ffmpeg", "-v", "error", "-f", "lavfi", "-i",
        "sine=frequency=440:sample_rate=48000", "-t", "2",
        "-c:a", "pcm_s16le", "-ac", "2", str(audio),
    ], check=True)
    output = asyncio.run(run_freezone_video_compose(
        project_dir=tmp_path, job_id="external-gain", resolution="480p", fps=24,
        keep_original_audio=False,
        tracks=[
            {"kind": "video", "items": [{
                "source_path": str(source), "source_end": 2,
            }]},
            {"kind": "audio", "items": [{
                "source_path": str(audio), "source_end": 2, "volume": volume,
            }]},
        ],
    ))
    cropped = apply_composition_crop(output, CompositionCrop(
        top_fraction=0.12, bottom_fraction=0.08, fit_mode="blurred_padding",
    ))
    # Ignore AAC boundary transients, but do not align away timing errors.
    reference = _decode_audio(audio)[9600:182400]
    for path in (output, cropped):
        actual = _decode_audio(path)[9600:182400]
        assert len(actual) == len(reference)
        energy = sum(x * x for x in reference)
        gain = sum(x * y for x, y in zip(reference, actual)) / energy
        correlation = gain * math.sqrt(energy / sum(y * y for y in actual))
        assert gain == pytest.approx(volume, rel=0.02)
        assert correlation > 0.995


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg required")
@pytest.mark.parametrize("fit_mode", ["crop", "blurred_padding"])
def test_portrait_blurred_padding_avoids_landscape_letterbox(tmp_path, fit_mode):
    source = tmp_path / "portrait.mp4"
    subprocess.run([
        "ffmpeg", "-v", "error", "-f", "lavfi", "-i",
        "color=c=red:s=180x320:r=24", "-t", "1",
        "-c:v", "libx264", str(source),
    ], check=True)
    crop = CompositionCrop(
        top_fraction=0.12, bottom_fraction=0.08, fit_mode=fit_mode,
    )
    output = asyncio.run(run_freezone_video_compose(
        project_dir=tmp_path, job_id="portrait-fit", resolution="480p", fps=24,
        composition_crop=crop.model_dump(),
        tracks=[{"kind": "video", "items": [{
            "source_path": str(source), "source_end": 1,
        }]}],
    ))
    metadata = json.loads(subprocess.check_output([
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height", "-of", "json", str(output),
    ]))["streams"][0]
    if fit_mode == "crop":
        assert metadata["width"] > metadata["height"]
        return
    assert metadata["width"] < metadata["height"]
    cropped = apply_composition_crop(output, crop)
    pixels = subprocess.check_output([
        "ffmpeg", "-v", "error", "-i", str(cropped),
        "-frames:v", "1", "-vf", "scale=90:160", "-pix_fmt", "rgb24",
        "-f", "rawvideo", "-",
    ])
    # Full-width foreground and blurred fill must not retain large black bars.
    red_fraction = sum(
        pixels[i] > 150 and pixels[i + 1] < 80 and pixels[i + 2] < 80
        for i in range(0, len(pixels), 3)
    ) / (90 * 160)
    assert red_fraction > 0.95
