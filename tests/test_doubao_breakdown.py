import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from PIL import Image

from novelvideo.doubao_work.client import GatewayError
from novelvideo.doubao_work.jobs import JobStore
from novelvideo.freezone import jobs, vision_gateway


class AnalysisClient:
    def __init__(self, response=None, unknown=False, pending=False):
        self.response = response or json.dumps({
            "title": "眼镜展示", "shots": [{
                "start_time": 0, "end_time": 5, "description": "眼镜缓慢转动",
                "camera_movement": "固定",
            }],
            "music": {"description": "猜测的音乐", "bpm": 120},
        }, ensure_ascii=False)
        self.posts = []
        self.unknown = unknown
        self.pending = pending

    def call(self, method, path, payload=None):
        if method == "POST":
            self.posts.append((path, payload))
            if self.unknown:
                raise GatewayError("REQUEST_OUTCOME_UNKNOWN", True)
            if path == "/v1/sessions":
                return {"id": "session"}
            if path == "/v1/files":
                return {"uri": "image"}
            return {"messages": [{"id": "message"}]}
        return {"messages": [] if self.pending else [{
            "role": "assistant", "session_id": "session", "reply_id": "message",
            "status": "completed", "items": [{"type": "text", "text": self.response}],
        }]}


@pytest.fixture
def native_pipeline(tmp_path, monkeypatch):
    from novelvideo.doubao_work import service, video
    store = JobStore(tmp_path / "state", tmp_path / "native", AnalysisClient())
    monkeypatch.setattr(service, "store", lambda: store)
    monkeypatch.setattr(video, "local_channel_available", lambda: True)
    monkeypatch.setattr(jobs, "_probe_video_duration", AsyncMock(return_value=5.0))
    monkeypatch.setattr(jobs, "_probe_has_audio", AsyncMock(return_value=True))
    monkeypatch.setattr(jobs, "_probe_video_size", AsyncMock(return_value=(1280, 720)))

    async def frame(*, output_path, **kwargs):
        Image.new("RGB", (12, 12)).save(output_path)

    async def render(*, output_path, **kwargs):
        output_path.write_bytes(b"media")

    monkeypatch.setattr(jobs, "_extract_frame_at", frame)
    monkeypatch.setattr(jobs, "_render_audio_clip", render)
    monkeypatch.setattr(jobs, "_render_video_clip", render)
    monkeypatch.setattr(
        jobs, "_extract_breakdown_frames",
        AsyncMock(side_effect=AssertionError("native frames must have known timestamps")),
    )
    monkeypatch.setattr(
        vision_gateway, "call_freezone_vision_model",
        AsyncMock(side_effect=RuntimeError("API key not set")),
    )
    video_path = tmp_path / "source.mp4"
    video_path.write_bytes(b"original-video")
    return store, dict(
        project_dir=tmp_path / "project", video_path=video_path, job_id="queue-run",
        dimensions=["storyboard", "motion", "music"],
    )


@pytest.mark.asyncio
async def test_local_breakdown_uses_native_without_api_key(native_pipeline):
    store, options = native_pipeline
    result = await jobs.run_freezone_video_breakdown(**options)
    assert result["model"] == "doubao_work"
    assert result["storyboard"]["groups"][0]["shots"][0]["image_path"]
    assert Path(result["motion"]["clips"][0]["video_path"]).exists()
    music = result["music"]["clip"]
    assert music["description"] == "原视频音轨参考片段（可能包含人声与环境声）"
    assert music["bpm"] is None
    assert music["instruments"] == []
    assert result["raw"]["music"] is None
    uploads = [body for path, body in store.client.posts if path == "/v1/files"]
    assert 1 <= len(uploads) <= 9
    event = next(body for path, body in store.client.posts if path.endswith("/events"))
    assert "0.312" in event["text"]
    assert "music" in event["text"]
    previous = len(store.client.posts)
    repeated = await jobs.run_freezone_video_breakdown(**options)
    assert repeated["raw"] == result["raw"]
    assert len(store.client.posts) == previous


@pytest.mark.asyncio
async def test_unknown_native_analysis_reuses_receipt_on_new_click(native_pipeline):
    store, options = native_pipeline
    store.client.unknown = True
    for run in ("first", "second"):
        with pytest.raises(RuntimeError, match="原任务"):
            await jobs.run_freezone_video_breakdown(**{**options, "job_id": run})
    assert len(store.client.posts) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("response", ['{"shots":[null]}', '{"shots":[]}', 'not json'])
async def test_invalid_native_output_has_no_automatic_second_call(native_pipeline, response):
    store, options = native_pipeline
    store.client.response = response
    with pytest.raises(RuntimeError, match="输出"):
        await jobs.run_freezone_video_breakdown(**options)
    assert len([path for path, _ in store.client.posts if path.endswith("/events")]) == 1


@pytest.mark.asyncio
async def test_nonlocal_breakdown_preserves_vision_channel(native_pipeline, monkeypatch):
    from novelvideo.doubao_work import video
    _, options = native_pipeline
    monkeypatch.setattr(video, "local_channel_available", lambda: False)
    frame = options["video_path"].with_suffix(".jpg")
    Image.new("RGB", (12, 12)).save(frame)
    monkeypatch.setattr(jobs, "_extract_breakdown_frames", AsyncMock(return_value=[frame]))
    with pytest.raises(RuntimeError, match="API key not set"):
        await jobs.run_freezone_video_breakdown(**options)


@pytest.mark.asyncio
async def test_pending_analysis_resumes_after_restart_without_resubmitting(native_pipeline, monkeypatch):
    from novelvideo.doubao_work import service
    from novelvideo.doubao_work.breakdown import analyze_breakdown

    store, options = native_pipeline
    frame = options["video_path"].with_suffix(".jpg")
    Image.new("RGB", (12, 12)).save(frame)
    store.client.pending = True
    args = dict(project=options["project_dir"], source=options["video_path"],
                prompt="分析", frames=[frame], duration_sec=5, max_polls=1, poll_interval=0)
    with pytest.raises(RuntimeError, match="超时"):
        await analyze_breakdown(**args, run_id="first")
    post_count = len(store.client.posts)
    restored = JobStore(store.state, store.output, store.client)
    monkeypatch.setattr(service, "store", lambda: restored)
    store.client.pending = False
    model, text = await analyze_breakdown(**args, run_id="second")
    assert model == "doubao_work"
    assert json.loads(text)["shots"]
    assert len(store.client.posts) == post_count


@pytest.mark.asyncio
async def test_untrusted_context_cannot_select_native_channel(native_pipeline):
    _, options = native_pipeline
    with pytest.raises(TypeError, match="TrustedEgressContext"):
        await jobs.run_freezone_video_breakdown(**options, egress_context={"kind": "local"})


@pytest.mark.asyncio
@pytest.mark.parametrize("origin,peer", [
    ("https://external.example", "127.0.0.1"), (None, "192.0.2.1"),
])
async def test_implicit_breakdown_route_rejects_nonlocal_request(monkeypatch, origin, peer):
    from fastapi import HTTPException
    from starlette.requests import Request
    from novelvideo.api.routes.doubao_work import guard_native_media_selection
    from novelvideo.doubao_work import video
    from novelvideo.shared import runtime_env

    monkeypatch.setattr(video, "local_channel_available", lambda: True)
    monkeypatch.setattr(runtime_env, "is_ce_effective", lambda: True)
    headers = [(b"host", b"127.0.0.1:8780")]
    if origin:
        headers.append((b"origin", origin.encode()))
    request = Request({
        "type": "http", "method": "POST", "scheme": "http",
        "path": "/api/v1/projects/local/freezone/video-breakdown",
        "query_string": b"", "headers": headers, "client": (peer, 1234),
        "server": ("127.0.0.1", 8780),
    })
    with pytest.raises(HTTPException) as error:
        await guard_native_media_selection(request)
    assert error.value.status_code == 403
