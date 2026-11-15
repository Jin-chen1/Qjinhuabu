import hashlib
import json
import shutil
import subprocess

import pytest

from novelvideo.freezone.composition_crop import CompositionCrop, crop_geometry, apply_composition_crop


def test_geometry_excludes_bottom_and_preserves_exact_portrait_ratio():
    x, y, w, h = crop_geometry(1152, 2048, CompositionCrop(bottom_fraction=0.04))
    assert (x, y, w, h) == (27, 0, 1098, 1952)
    assert w * 16 == h * 9
    assert y + h <= 2048 * 0.96


def test_top_and_bottom_crop_retains_complete_product_bounds():
    crop = CompositionCrop(top_fraction=0.065, bottom_fraction=0.055)
    x, y, w, h = crop_geometry(720, 1280, crop)
    assert y >= 84 and y + h <= 1208
    assert w * 16 == h * 9
    # The measured product occupies x=55..668 and y=580..875.
    assert x <= 55 and x + w >= 668
    assert y <= 580 and y + h >= 875


def test_combined_crop_cannot_remove_half_the_frame():
    with pytest.raises(ValueError):
        CompositionCrop(top_fraction=0.3, bottom_fraction=0.2)


def test_padding_crop_preserves_full_product_width():
    crop = CompositionCrop(
        top_fraction=0.12, bottom_fraction=0.08, fit_mode="blurred_padding",
    )
    x, y, w, h = crop_geometry(720, 1280, crop)
    assert x == 0 and w == 720
    assert y >= 153 and y + h <= 1178


@pytest.mark.parametrize("fraction", [-0.01, 0.5, float("nan")])
def test_invalid_crop_rejected(fraction):
    with pytest.raises(ValueError):
        CompositionCrop(bottom_fraction=fraction)


def test_api_contract_is_opt_in_and_result_lookup_prefers_cropped(tmp_path, monkeypatch):
    from novelvideo.api.schemas import FreezoneVideoComposeRequest
    from novelvideo.api.routes import freezone

    assert FreezoneVideoComposeRequest().composition_crop is None
    request = FreezoneVideoComposeRequest(composition_crop={"bottom_fraction": 0.04})
    assert request.composition_crop.aspect_ratio == "9:16"
    monkeypatch.setattr(freezone, "outputs_dir", lambda *_: tmp_path)
    assert freezone._video_compose_output_path(tmp_path, "test") == tmp_path / "test.mp4"
    cropped = tmp_path / "test_cropped.mp4"
    cropped.touch()
    assert freezone._video_compose_output_path(tmp_path, "test") == cropped


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg required")
@pytest.mark.parametrize("fit_mode", ["crop", "blurred_padding"])
def test_crop_keeps_source_and_audio_packets_and_duration(tmp_path, fit_mode):
    source = tmp_path / "composed.mp4"
    subprocess.run([
        "ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=180x320:r=24",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
        "-t", "5.057", "-c:v", "libx264", "-c:a", "aac", str(source),
    ], check=True)
    original_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    output = apply_composition_crop(
        source, CompositionCrop(bottom_fraction=0.04, fit_mode=fit_mode),
    )
    assert output != source
    assert hashlib.sha256(source.read_bytes()).hexdigest() == original_hash
    def probe(path):
        return json.loads(subprocess.check_output([
            "ffprobe", "-v", "error", "-show_streams", "-show_packets",
            "-show_data_hash", "sha256", "-of", "json", str(path),
        ]))
    before, after = probe(source), probe(output)
    video = next(s for s in after["streams"] if s["codec_type"] == "video")
    assert (video["width"], video["height"], video["r_frame_rate"]) == (180, 320, "24/1")
    assert video["duration"] == before["streams"][0]["duration"]
    audio_before = [p["data_hash"] for p in before["packets"] if p["stream_index"] == 1]
    audio_after = [p["data_hash"] for p in after["packets"] if p["stream_index"] == 1]
    assert audio_before and audio_after == audio_before
