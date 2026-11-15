import base64

import pytest
from PIL import Image

from novelvideo.doubao_work.client import GatewayError
from novelvideo.doubao_work.jobs import JobStore
from novelvideo.generators.video_generator import VideoGenStatus, create_video_generator


class VideoClient:
    def __init__(self, unknown=False):
        self.posts = []
        self.unknown = unknown

    def call(self, method, path, payload=None):
        if method == "POST":
            self.posts.append((path, payload))
            if self.unknown:
                raise GatewayError("REQUEST_OUTCOME_UNKNOWN", True)
            if path == "/v1/sessions":
                return {"id": "session"}
            if path == "/v1/files":
                return {"uri": "file-uri"}
            return {"messages": [{"id": "message"}]}
        return {"messages": [{
            "role": "assistant", "session_id": "session", "reply_id": "message",
            "status": "completed", "items": [{
                "type": "artifact", "artifact_type": "video", "state": "active",
                "video": {"url": "https://example.com/video"},
            }],
        }]}


def media_store(tmp_path, monkeypatch, client=None):
    from novelvideo.doubao_work import jobs
    monkeypatch.setattr(jobs, "download", lambda url, path: path.write_bytes(b"verified-video"))
    monkeypatch.setattr(jobs, "verified_media", lambda path, kind: {
        "extension": ".mp4", "width": 720, "height": 720, "duration": 5.057, "codec": "hevc",
    })
    return JobStore(tmp_path / "state", tmp_path / "out", client or VideoClient())


def test_factory_supports_local_doubao(monkeypatch):
    from novelvideo.shared import runtime_env
    monkeypatch.setattr(runtime_env, "is_ce_effective", lambda: True)
    assert type(create_video_generator("doubao_work")).__name__ == "DoubaoVideoGenerator"


def test_factory_denies_ee(monkeypatch):
    from novelvideo.shared import runtime_env
    monkeypatch.setattr(runtime_env, "is_ce_effective", lambda: False)
    with pytest.raises(ValueError, match="本机"):
        create_video_generator("doubao_work")


def test_factory_allows_trusted_local_task_and_denies_organization(monkeypatch):
    from novelvideo.egress_context import TrustedEgressContext
    from novelvideo.ports.authz import BillingPrincipal
    from novelvideo.ports.model_credentials import CredentialReference
    from novelvideo.shared import runtime_env
    monkeypatch.setattr(runtime_env, "is_ce_effective", lambda: True)
    def context(kind):
        return TrustedEgressContext(
            envelope_id="env", project_id="project", task_type="single_video",
            requester_user_id="user", root_task_id="run", admission_id="admission",
            admitted_at="2026-11-05", membership_id=None, authz_version=1,
            billing_principal=BillingPrincipal(kind=kind, id="local"),
            credential=CredentialReference(source="local", credential_id="local", key_version=1),
        )
    assert type(create_video_generator("doubao_work", egress_context=context("local"))).__name__ == "DoubaoVideoGenerator"
    with pytest.raises(RuntimeError, match="egress"):
        create_video_generator("doubao_work", egress_context=context("organization"))


@pytest.mark.asyncio
async def test_generation_delivers_actual_duration_and_same_run_reuses(tmp_path, monkeypatch):
    from novelvideo.doubao_work.video import DoubaoVideoGenerator
    store = media_store(tmp_path, monkeypatch)
    frame = tmp_path / "frame.png"
    Image.new("RGB", (12, 12)).save(frame)
    output = tmp_path / "beats" / "beat.mp4"
    generator = DoubaoVideoGenerator(store=store)
    options = dict(image_path=str(frame), prompt="眼镜缓慢转动", output_path=str(output),
                   run_id="run-1", max_polls=2, poll_interval=0)
    result = await generator.generate(**options)
    assert result.status == VideoGenStatus.DONE
    assert result.duration_seconds == 5.057
    assert output.read_bytes() == b"verified-video"
    assert result.provider_task_id
    assert len(store.client.posts) == 3
    upload = store.client.posts[1][1]
    assert base64.b64decode(upload["contentBase64"]) == frame.read_bytes()
    repeated = await generator.generate(**options)
    assert repeated.provider_task_id == result.provider_task_id
    assert len(store.client.posts) == 3


@pytest.mark.asyncio
async def test_unknown_outcome_blocks_new_run(tmp_path, monkeypatch):
    from novelvideo.doubao_work.video import DoubaoVideoGenerator
    client = VideoClient(unknown=True)
    store = media_store(tmp_path, monkeypatch, client)
    generator = DoubaoVideoGenerator(store=store)
    options = dict(image_path=None, prompt="转动", output_path=str(tmp_path / "beat.mp4"),
                   max_polls=2, poll_interval=0)
    first = await generator.generate(**options, run_id="first")
    second = await generator.generate(**options, run_id="second")
    assert first.status == second.status == VideoGenStatus.FAILED
    assert "outcome_unknown" in second.error
    assert first.provider_task_id == second.provider_task_id
    assert len(client.posts) == 1


@pytest.mark.asyncio
async def test_download_pending_resumes_original_job_on_new_run(tmp_path, monkeypatch):
    from novelvideo.doubao_work.video import DoubaoVideoGenerator
    from novelvideo.doubao_work import jobs
    store = media_store(tmp_path, monkeypatch)
    def failed_download(url, path):
        raise RuntimeError("delivery unavailable")
    monkeypatch.setattr(jobs, "download", failed_download)
    options = dict(image_path=None, prompt="转动", output_path=str(tmp_path / "beat.mp4"),
                   max_polls=1, poll_interval=0)
    first = await DoubaoVideoGenerator(store=store).generate(**options, run_id="one")
    assert first.status == VideoGenStatus.FAILED
    assert "原任务" in first.error
    monkeypatch.setattr(jobs, "download", lambda url, path: path.write_bytes(b"verified-video"))
    restored = JobStore(store.state, store.output, store.client)
    second = await DoubaoVideoGenerator(store=restored).generate(**options, run_id="two")
    assert second.status == VideoGenStatus.DONE
    assert first.provider_task_id == second.provider_task_id
    assert len(store.client.posts) == 2


@pytest.mark.asyncio
async def test_changed_prompt_does_not_resubmit_pending_job(tmp_path, monkeypatch):
    from novelvideo.doubao_work.video import DoubaoVideoGenerator
    store = media_store(tmp_path, monkeypatch, VideoClient(unknown=True))
    generator = DoubaoVideoGenerator(store=store)
    output = str(tmp_path / "beat.mp4")
    await generator.generate(None, "转动", output, run_id="first", max_polls=1, poll_interval=0)
    result = await generator.generate(None, "跳动", output, run_id="second", max_polls=1)
    assert result.status == VideoGenStatus.FAILED
    assert len(store.client.posts) == 1


@pytest.mark.asyncio
async def test_explicit_new_run_after_success_creates_new_generation(tmp_path, monkeypatch):
    from novelvideo.doubao_work.video import DoubaoVideoGenerator
    store = media_store(tmp_path, monkeypatch)
    generator = DoubaoVideoGenerator(store=store)
    options = dict(image_path=None, prompt="转动", output_path=str(tmp_path / "beat.mp4"),
                   max_polls=1, poll_interval=0)
    first = await generator.generate(**options, run_id="one")
    second = await generator.generate(**options, run_id="two")
    assert first.status == second.status == VideoGenStatus.DONE
    assert first.provider_task_id != second.provider_task_id
    assert len(store.client.posts) == 4


def test_local_menu_exposes_doubao(monkeypatch):
    from novelvideo.shared import runtime_env
    from novelvideo.api.routes.generation import _api_video_backend_options
    monkeypatch.setattr(runtime_env, "is_ce_effective", lambda: True)
    options = _api_video_backend_options()
    assert any(item.value == "doubao_work" and "豆包工作" in item.label for item in options)
    monkeypatch.setattr(runtime_env, "is_ce_effective", lambda: False)
    assert all(item.value != "doubao_work" for item in _api_video_backend_options())


def test_native_billing_model_is_local_only(monkeypatch):
    from fastapi import HTTPException
    from novelvideo.api.routes.model_credits import _video_backend_cost_model
    from novelvideo.shared import runtime_env
    monkeypatch.setattr(runtime_env, "is_ce_effective", lambda: True)
    assert _video_backend_cost_model("doubao_work") == "doubao_work"
    monkeypatch.setattr(runtime_env, "is_ce_effective", lambda: False)
    with pytest.raises(HTTPException):
        _video_backend_cost_model("doubao_work")


@pytest.mark.asyncio
async def test_runner_passes_run_identity_and_indexes_actual_duration(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from novelvideo.generators import video_generator, video_pool_indexer
    from novelvideo.generators.video_generator import VideoGenResult
    from novelvideo.task_backend.runners import video

    captured = {}
    class Generator:
        async def generate(self, **kwargs):
            captured.update(kwargs)
            return VideoGenResult(status=VideoGenStatus.DONE, duration_seconds=5.057,
                                  provider_task_id="native-job")
    class Manager:
        def update_progress_for_project(self, *args, **kwargs):
            pass
    monkeypatch.setattr(video_generator, "create_video_generator", lambda **kwargs: Generator())
    monkeypatch.setattr(video, "get_task_manager", lambda: Manager())
    pool = {}
    def add_video_to_pool(**kwargs):
        pool.update(kwargs)
        return SimpleNamespace(id="entry")
    monkeypatch.setattr(video_pool_indexer, "add_video_to_pool", add_video_to_pool)
    result = await video._run_single_video_async({
        "task_type": "single_video", "episode": 1, "beat_num": 2, "__run_task_id": "runner-id",
        "payload": {"config": {"video_backend": "doubao_work", "video_duration": 9}},
    }, SimpleNamespace(output_dir=tmp_path))
    assert captured["run_id"] == "runner-id"
    assert pool["duration"] == 5.057
    assert result["provider_task_id"] == "native-job"


@pytest.mark.asyncio
@pytest.mark.parametrize("trusted", [False, True])
@pytest.mark.parametrize("mode", ["text_to_video", "image_reference", "first_frame", "first_last_frame"])
async def test_canvas_native_dispatch_preserves_frames_and_run(tmp_path, monkeypatch, trusted, mode):
    from novelvideo.freezone.jobs import run_freezone_video_gen
    from novelvideo.freezone.video_node import run_trusted_freezone_video_gen
    from novelvideo.generators import video_generator
    from novelvideo.generators.video_generator import VideoGenResult

    captured = {}

    class Generator:
        async def generate(self, **kwargs):
            captured.update(kwargs)
            from pathlib import Path
            Path(kwargs["output_path"]).write_bytes(b"verified-video")
            return VideoGenResult(status=VideoGenStatus.DONE)

    monkeypatch.setattr(video_generator, "create_video_generator", lambda **kwargs: Generator())
    refs = [] if mode == "text_to_video" else [{"type": "image", "path": "first.png", "role": "首帧"}]
    if mode == "first_last_frame":
        refs.append({"type": "image", "path": "last.png", "role": "尾帧"})
    options = dict(project_dir=tmp_path, job_id="canvas-run", prompt="转动",
                   backend="doubao_work", gen_mode=mode, reference_items=refs,
                   last_frame_path="last.png" if mode == "first_last_frame" else None)
    if trusted:
        options.update(egress_context=object(), task_type="freezone_video_gen",
                       episode=0, beat_num=0, scope="canvas-run")
    output = await (run_trusted_freezone_video_gen if trusted else run_freezone_video_gen)(**options)
    assert output.read_bytes() == b"verified-video"
    assert captured["run_id"] == "canvas-run"
    assert not captured.get("references")
    assert captured["image_path"] == (None if mode == "text_to_video" else "first.png")
    assert captured["last_frame_path"] == ("last.png" if mode == "first_last_frame" else None)


@pytest.mark.asyncio
@pytest.mark.parametrize("refs", [
    [{"type": "video", "path": "input.mp4"}],
    [{"type": "image", "path": f"{index}.png"} for index in range(3)],
])
async def test_canvas_native_dispatch_rejects_unsupported_references(tmp_path, monkeypatch, refs):
    from novelvideo.freezone.jobs import run_freezone_video_gen
    from novelvideo.generators import video_generator

    class Generator:
        async def generate(self, **kwargs):
            pytest.fail("invalid references must not reach generation")

    monkeypatch.setattr(video_generator, "create_video_generator", lambda **kwargs: Generator())
    with pytest.raises(ValueError, match="豆包工作"):
        await run_freezone_video_gen(project_dir=tmp_path, job_id="invalid", prompt="转动",
                                     backend="doubao_work", reference_items=refs)
