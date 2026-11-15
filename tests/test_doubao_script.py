import json
import pytest
from PIL import Image

from novelvideo.doubao_work.jobs import JobStore
from novelvideo.freezone import text_node
from test_doubao_breakdown import AnalysisClient


@pytest.fixture
def native_script(tmp_path, monkeypatch):
    from novelvideo.doubao_work import service, video

    client = AnalysisClient(response=json.dumps({
        "title": "眼镜展示", "rows": [{
            "shot_no": 1, "duration": 2, "visual_description": "黑色圆框眼镜置于浅灰台面",
            "shot_prompt": "[近景：黑色圆框眼镜]", "video_motion_prompt": "[缓慢推进]",
            "keyframe_index": 1,
        }],
    }, ensure_ascii=False))
    store = JobStore(tmp_path / "state", tmp_path / "native", client)
    monkeypatch.setattr(service, "store", lambda: store)
    monkeypatch.setattr(video, "local_channel_available", lambda: True)
    monkeypatch.setattr(
        text_node, "get_freezone_video_story_script_agent",
        lambda: (_ for _ in ()).throw(RuntimeError("API key not set")),
    )
    frame = tmp_path / "frame.jpg"
    Image.new("RGB", (12, 12)).save(frame)
    return store, frame


@pytest.mark.asyncio
async def test_native_script_accepts_video_frames_without_gateway_key(native_script):
    store, frame = native_script
    result = await text_node.generate_freezone_story_script_with_vision(frame_paths=[frame])
    assert result.rows[0].visual_description.startswith("黑色圆框")
    text_node.bind_story_script_assets(result, frame_urls=["/static/real.jpg"])
    assert result.rows[0].reference == "/static/real.jpg"
    assert len([p for p, _ in store.client.posts if p.endswith("/events")]) == 1
    repeat = await text_node.generate_freezone_story_script_with_vision(frame_paths=[frame])
    assert repeat.title == result.title
    assert len([p for p, _ in store.client.posts if p.endswith("/events")]) == 1


@pytest.mark.asyncio
async def test_native_text_script_without_gateway_key(native_script, monkeypatch):
    store, _ = native_script
    monkeypatch.setattr(
        text_node, "get_freezone_story_script_agent",
        lambda model: (_ for _ in ()).throw(RuntimeError("API key not set")),
    )
    result = await text_node.generate_freezone_story_script(source_text="拍摄一副黑色圆框眼镜")
    assert result.rows
    assert not [p for p, _ in store.client.posts if p == "/v1/files"]


@pytest.mark.asyncio
@pytest.mark.parametrize("response", ['{"rows":[]}', '{"rows":[{}]}', "not json"])
async def test_native_script_invalid_output_is_not_retried(native_script, response):
    store, frame = native_script
    store.client.response = response
    with pytest.raises(RuntimeError, match="原任务"):
        await text_node.generate_freezone_story_script_with_vision(frame_paths=[frame])
    assert len([p for p, _ in store.client.posts if p.endswith("/events")]) == 1


@pytest.mark.asyncio
async def test_nonlocal_script_preserves_gateway(native_script, monkeypatch):
    from novelvideo.doubao_work import video

    store, frame = native_script
    monkeypatch.setattr(video, "local_channel_available", lambda: False)
    with pytest.raises(RuntimeError, match="API key not set"):
        await text_node.generate_freezone_story_script_with_vision(frame_paths=[frame])
    assert not store.client.posts


@pytest.mark.asyncio
async def test_script_rejects_untrusted_egress_context(native_script):
    store, frame = native_script
    with pytest.raises(TypeError, match="TrustedEgressContext"):
        await text_node.generate_freezone_story_script_with_vision(
            frame_paths=[frame], egress_context={"kind": "local"},
        )
    assert not store.client.posts


@pytest.mark.asyncio
async def test_organization_script_does_not_use_native(native_script, monkeypatch):
    from novelvideo.egress_context import TrustedEgressContext

    store, frame = native_script
    context = object.__new__(TrustedEgressContext)
    monkeypatch.setattr(TrustedEgressContext, "is_organization", property(lambda self: True))
    with pytest.raises(RuntimeError, match="API key not set"):
        await text_node.generate_freezone_story_script_with_vision(
            frame_paths=[frame], egress_context=context,
        )
    assert not store.client.posts


@pytest.mark.asyncio
async def test_script_receipt_survives_timeout_and_new_click(native_script, monkeypatch):
    from novelvideo.doubao_work import service
    from novelvideo.doubao_work.script import generate_script

    store, _ = native_script
    store.client.pending = True
    options = dict(scope="project/canvas/script-node", prompt="眼镜脚本", images=[],
                   poll_interval=0, max_polls=1)
    with pytest.raises(RuntimeError, match="超时"):
        await generate_script(**options, run_id="first")
    count = len(store.client.posts)
    monkeypatch.setattr(service, "store", lambda: JobStore(store.state, store.output, store.client))
    store.client.pending = False
    assert (await generate_script(**options, run_id="second")).rows
    assert len(store.client.posts) == count


@pytest.mark.asyncio
async def test_script_unknown_submission_does_not_repeat(native_script):
    from novelvideo.doubao_work.script import generate_script

    store, _ = native_script
    store.client.unknown = True
    for run in ("first", "second"):
        with pytest.raises(RuntimeError, match="原任务"):
            await generate_script(scope="same/node", run_id=run, prompt="眼镜脚本", images=[])
    assert len(store.client.posts) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("origin,peer", [
    ("https://external.example", "127.0.0.1"), (None, "192.0.2.1"),
])
async def test_script_route_guards_implicit_native_selection(native_script, origin, peer):
    from fastapi import HTTPException
    from starlette.requests import Request
    from novelvideo.api.routes.doubao_work import guard_native_media_selection

    headers = [(b"host", b"127.0.0.1:8780")]
    if origin:
        headers.append((b"origin", origin.encode()))
    request = Request({
        "type": "http", "method": "POST", "scheme": "http",
        "path": "/api/v1/projects/local/freezone/text/story-script",
        "query_string": b"", "headers": headers, "client": (peer, 1234),
        "server": ("127.0.0.1", 8780),
    })
    with pytest.raises(HTTPException) as error:
        await guard_native_media_selection(request)
    assert error.value.status_code == 403
