import hashlib
import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize("has_audio", [True, False])
def test_prepare_extracts_first_frame_and_complete_audio(tmp_path, monkeypatch, has_audio):
    from novelvideo.freezone import reference_prepare as service

    source = tmp_path / "source.mp4"
    source.write_bytes(b"source-video")
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        assert kwargs["timeout"] > 0
        if cmd[0] == "ffprobe":
            streams = [{"codec_type": "video", "width": 320, "height": 240}]
            if has_audio:
                streams.append({"codec_type": "audio"})
            return SimpleNamespace(returncode=0, stdout=json.dumps({
                "streams": streams, "format": {"duration": "2.125"},
            }))
        Path(cmd[-1]).write_bytes(b"extracted")
        return SimpleNamespace(returncode=0, stdout="")

    monkeypatch.setattr(service.subprocess, "run", run)
    result = service.prepare_reference(tmp_path, "source.mp4")
    assert result.duration_ms == 2125
    assert (result.width, result.height) == (320, 240)
    assert result.source_sha256 == hashlib.sha256(b"source-video").hexdigest()
    assert result.first_frame_path.is_file()
    assert result.first_frame_path.parent.parent == tmp_path / "freezone/_outputs/reference_prepare"
    frame = calls[1]
    assert frame[frame.index("-ss") + 1] == "0"
    assert frame[frame.index("-frames:v") + 1] == "1"
    assert (result.audio_path is not None) == has_audio
    if has_audio:
        audio = calls[2]
        assert "-t" not in audio and "-shortest" not in audio
        assert audio[audio.index("-c:a") + 1] == "pcm_s16le"
        assert result.audio_path.is_file()
    assert len(calls) == (3 if has_audio else 2)
    assert source.read_bytes() == b"source-video"


@pytest.mark.parametrize("url", [
    "../outside.mp4", "https://example.com/video.mp4", "missing.mp4",
])
def test_prepare_rejects_invalid_paths_without_outputs(tmp_path, url):
    from novelvideo.freezone.reference_prepare import prepare_reference

    with pytest.raises((ValueError, FileNotFoundError)):
        prepare_reference(tmp_path, url)
    assert not (tmp_path / "freezone/_outputs").exists()


def test_prepare_rejects_symlink_escape(tmp_path):
    from novelvideo.freezone.reference_prepare import prepare_reference

    (tmp_path / "escape.mp4").symlink_to(tmp_path.parent / "outside.mp4")
    with pytest.raises(ValueError):
        prepare_reference(tmp_path, "escape.mp4")


def test_prepare_corrupt_video_does_not_leave_outputs(tmp_path, monkeypatch):
    from novelvideo.freezone import reference_prepare as service

    (tmp_path / "bad.mp4").write_bytes(b"bad")
    monkeypatch.setattr(service.subprocess, "run", lambda *a, **k:
                        SimpleNamespace(returncode=1, stdout=""))
    with pytest.raises(ValueError):
        service.prepare_reference(tmp_path, "bad.mp4")
    assert not (tmp_path / "freezone/_outputs").exists()


def test_extract_failure_removes_partial_outputs(tmp_path, monkeypatch):
    from novelvideo.freezone import reference_prepare as service

    (tmp_path / "source.mp4").write_bytes(b"video")

    def run(cmd, **kwargs):
        if cmd[0] == "ffprobe":
            return SimpleNamespace(returncode=0, stdout=json.dumps({
                "streams": [{"codec_type": "video", "width": 320, "height": 240}],
                "format": {"duration": "1"},
            }))
        Path(cmd[-1]).write_bytes(b"partial")
        return SimpleNamespace(returncode=1, stdout="")

    monkeypatch.setattr(service.subprocess, "run", run)
    with pytest.raises(ValueError):
        service.prepare_reference(tmp_path, "source.mp4")
    assert list((tmp_path / "freezone/_outputs/reference_prepare").iterdir()) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("has_audio", [True, False])
async def test_route_contract_offloads_and_preserves_project_permissions(
    tmp_path, monkeypatch, has_audio,
):
    import threading
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from novelvideo.api.routes import freezone as routes
    from novelvideo.doubao_work import video
    from novelvideo.freezone.reference_prepare import PreparedReference

    main_thread = threading.get_ident()
    scope_calls = []

    async def resolve(project, user):
        scope_calls.append((project, user))
        return object(), "local", "demo", tmp_path, str(tmp_path)

    def prepare(project_dir, url):
        assert threading.get_ident() != main_thread
        assert project_dir == tmp_path and url == "source.mp4"
        return PreparedReference(
            tmp_path / "first_frame.png",
            tmp_path / "audio.wav" if has_audio else None,
            2125, 320, 240, "a" * 64,
        )

    monkeypatch.setattr(video, "local_channel_available", lambda: True)
    monkeypatch.setattr(routes, "_resolve_freezone_project", resolve)
    monkeypatch.setattr(routes, "prepare_reference", prepare)
    monkeypatch.setattr(routes, "make_static_url_for_context",
                        lambda ctx, rel, **kw: f"/api/v1/projects/demo/media/{rel}")
    app = FastAPI()
    app.include_router(routes.router, prefix="/api/v1")
    app.dependency_overrides[routes.get_api_user] = lambda: {"username": "local"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/v1/projects/demo/freezone/reference/prepare", json={
            "video_url": "source.mp4", "canvas_id": "new_canvas", "node_id": "reference",
        })
    assert response.status_code == 200
    assert response.json() == {
        "first_frame_url": "/api/v1/projects/demo/media/first_frame.png",
        "audio_url": "/api/v1/projects/demo/media/audio.wav" if has_audio else None,
        "duration_ms": 2125, "width": 320, "height": 240, "source_sha256": "a" * 64,
    }
    assert scope_calls == [("demo", {"username": "local"})]


@pytest.mark.asyncio
async def test_route_refuses_nonlocal_environment(monkeypatch):
    from fastapi import HTTPException
    from novelvideo.api.routes import freezone as routes
    from novelvideo.doubao_work import video
    from novelvideo.freezone.reference_prepare import ReferencePrepareRequest

    monkeypatch.setattr(video, "local_channel_available", lambda: False)
    with pytest.raises(HTTPException) as error:
        await routes.freezone_reference_prepare(
            "demo", ReferencePrepareRequest(
                video_url="source.mp4", canvas_id="new_canvas", node_id="reference",
            ), {"username": "local"},
        )
    assert error.value.status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize("failure,status", [
    (ValueError("unsafe path"), 400),
    (FileNotFoundError("missing"), 404),
])
async def test_route_maps_invalid_source_errors(tmp_path, monkeypatch, failure, status):
    from fastapi import HTTPException
    from novelvideo.api.routes import freezone as routes
    from novelvideo.doubao_work import video
    from novelvideo.freezone.reference_prepare import ReferencePrepareRequest

    async def resolve(project, user):
        return object(), "local", "demo", tmp_path, str(tmp_path)

    def prepare(*args):
        raise failure

    monkeypatch.setattr(video, "local_channel_available", lambda: True)
    monkeypatch.setattr(routes, "_resolve_freezone_project", resolve)
    monkeypatch.setattr(routes, "prepare_reference", prepare)
    with pytest.raises(HTTPException) as error:
        await routes.freezone_reference_prepare(
            "demo", ReferencePrepareRequest(
                video_url="bad.mp4", canvas_id="new_canvas", node_id="reference",
            ), {"username": "local"},
        )
    assert error.value.status_code == status


def test_real_ffmpeg_extracts_existing_silent_video(tmp_path):
    from PIL import Image
    from novelvideo.freezone.reference_prepare import prepare_reference

    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("FFmpeg tools are not installed")
    bundled = Path(__file__).parents[1] / "src/novelvideo/assets/login_bg_v2.mp4"
    if not bundled.is_file():
        pytest.skip("Bundled video is not available in this checkout")
    source = tmp_path / "source.mp4"
    shutil.copyfile(bundled, source)
    result = prepare_reference(tmp_path, "source.mp4")
    assert result.audio_path is None
    assert result.duration_ms == 8000
    assert (result.width, result.height) == (1236, 652)
    with Image.open(result.first_frame_path) as image:
        image.load()
        assert image.size == (1236, 652)
    with source.open("rb") as handle:
        assert result.source_sha256 == hashlib.file_digest(handle, "sha256").hexdigest()
