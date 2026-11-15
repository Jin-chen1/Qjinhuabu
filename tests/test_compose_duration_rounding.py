import json
import shutil
import subprocess

import pytest

from novelvideo.freezone.jobs import _render_video_clip


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg required")
async def test_compose_extends_last_frame_without_retiming(tmp_path):
    source = tmp_path / "source.mp4"
    output = tmp_path / "output.mp4"
    subprocess.run([
        "ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
        "testsrc2=size=64x64:rate=24", "-frames:v", "121",
        "-c:v", "libx264", str(source),
    ], check=True)
    await _render_video_clip(
        source_path=str(source), output_path=output, source_start=0,
        duration=5.057, speed=1, width=64, height=64, fps=24,
        background_color="#000000", keep_original_audio=False, volume=1,
        muted=True,
    )
    probe = json.loads(subprocess.check_output([
        "ffprobe", "-v", "error", "-show_entries",
        "stream=codec_type,nb_frames,duration,avg_frame_rate", "-of", "json", str(output),
    ]))
    video = next(s for s in probe["streams"] if s["codec_type"] == "video")
    audio = next(s for s in probe["streams"] if s["codec_type"] == "audio")
    assert int(video["nb_frames"]) == 122
    assert video["avg_frame_rate"] == "24/1"
    assert abs(float(audio["duration"]) - 5.057) <= 0.001
    subprocess.run(["ffmpeg", "-v", "error", "-i", str(output), "-f", "null", "-"],
                   check=True)


async def test_compose_blocks_real_shortage(tmp_path):
    source = tmp_path / "short.mp4"
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg required")
    subprocess.run([
        "ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
        "color=size=64x64:rate=24", "-frames:v", "120",
        "-c:v", "libx264", str(source),
    ], check=True)
    with pytest.raises(RuntimeError, match="shorter"):
        await _render_video_clip(
            source_path=str(source), output_path=tmp_path / "output.mp4",
            source_start=0, duration=5.057, speed=1, width=64, height=64,
            fps=24, background_color="#000000", keep_original_audio=False,
            volume=1, muted=True,
        )
