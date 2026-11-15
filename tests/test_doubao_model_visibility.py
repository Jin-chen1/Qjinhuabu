from types import SimpleNamespace

import pytest


def test_local_image_menu_and_old_selection_fallback(monkeypatch):
    from novelvideo import config
    from novelvideo.doubao_work import video

    monkeypatch.setattr(video, "local_channel_available", lambda: True)
    assert list(config.image_generation_selection_options()) == ["doubao_work"]
    assert config.normalize_image_generation_selection("newapi_gpt_image2") == "doubao_work"
    monkeypatch.setattr(video, "local_channel_available", lambda: False)
    assert "newapi_gpt_image2" in config.image_generation_selection_options()
    assert config.normalize_image_generation_selection("newapi_gpt_image2") == "newapi_gpt_image2"


def test_local_episode_video_menu(monkeypatch):
    from novelvideo.api.routes.generation import _api_video_backend_options
    from novelvideo.doubao_work import video

    monkeypatch.setattr(video, "local_channel_available", lambda: True)
    options = _api_video_backend_options()
    assert [item.value for item in options] == ["doubao_work"]
    assert options[0].is_default
    monkeypatch.setattr(video, "local_channel_available", lambda: False)
    assert all(item.value != "doubao_work" for item in _api_video_backend_options())


@pytest.mark.asyncio
async def test_local_canvas_image_menu_hides_configured_models(monkeypatch):
    from novelvideo.api.routes import freezone
    from novelvideo.doubao_work import video

    async def project(*args, **kwargs):
        return SimpleNamespace(requester_user_id="local"), None, None, None, None

    async def catalog(*args, **kwargs):
        return [{"id": "old", "providerId": "newapi", "apiModel": "old", "label": "旧模型"}]

    monkeypatch.setattr(freezone, "_resolve_freezone_project", project)
    monkeypatch.setattr(freezone, "_scoped_media_model_catalog", catalog)
    monkeypatch.setattr(video, "local_channel_available", lambda: True)
    result = await freezone.freezone_image_models("demo", {})
    assert [item["id"] for item in result["data"]] == ["doubao_work"]


@pytest.mark.asyncio
async def test_local_canvas_video_menu_hides_configured_models(monkeypatch):
    from novelvideo.api.routes import freezone
    from novelvideo.doubao_work import video

    async def project(*args, **kwargs):
        return SimpleNamespace(requester_user_id="local"), None, None, None, None

    async def catalog(*args, **kwargs):
        return [{"id": "old", "providerId": "newapi", "apiModel": "old", "label": "旧模型"}]

    monkeypatch.setattr(freezone, "_resolve_freezone_project", project)
    monkeypatch.setattr(freezone, "_scoped_media_model_catalog", catalog)
    monkeypatch.setattr(video, "local_channel_available", lambda: True)
    result = await freezone.freezone_video_models("demo", {})
    assert [item["id"] for item in result["data"]] == ["doubao_work"]
    assert result["data"][0]["supportedModes"] == [
        "text_to_video", "first_frame", "first_last_frame", "image_to_video",
    ]
    monkeypatch.setattr(video, "local_channel_available", lambda: False)
    result = await freezone.freezone_video_models("demo", {})
    assert [item["id"] for item in result["data"]] == ["old"]


@pytest.mark.asyncio
async def test_canvas_native_video_resolvers_are_local_only(monkeypatch):
    from fastapi import HTTPException
    from novelvideo.api.routes import freezone
    from novelvideo.doubao_work import video

    monkeypatch.setattr(video, "local_channel_available", lambda: True)
    assert await freezone._resolve_catalog_video_backend("doubao_work") == "doubao_work"
    schema, params, entry = await freezone._resolve_catalog_request(
        "video", "doubao_work", {}, mode="firstLastFrame",
    )
    assert schema == params == {}
    assert entry["apiModel"] == "doubao_work"
    assert (await freezone._require_scoped_media_model(
        "video", "doubao_work", requester_user_id="local",
    ))["id"] == "doubao_work"
    with pytest.raises(HTTPException) as unsupported:
        await freezone._resolve_catalog_request("video", "doubao_work", {}, mode="videoEdit")
    assert unsupported.value.status_code == 400
    monkeypatch.setattr(video, "local_channel_available", lambda: False)
    for resolve in (
        freezone._resolve_catalog_video_backend("doubao_work"),
        freezone._resolve_catalog_request("video", "doubao_work", {}),
        freezone._require_scoped_media_model("video", "doubao_work", requester_user_id="ee"),
    ):
        with pytest.raises(HTTPException) as denied:
            await resolve
        assert denied.value.status_code == 403


def test_default_freezone_image_model_uses_doubao_work_when_local(monkeypatch):
    from novelvideo.doubao_work import video
    from novelvideo.freezone.route_helpers import (
        FREEZONE_DEFAULT_IMAGE_MODEL,
        default_freezone_image_model,
    )

    monkeypatch.setattr(video, "local_channel_available", lambda: True)
    assert default_freezone_image_model() == "doubao_work"
    monkeypatch.setattr(video, "local_channel_available", lambda: False)
    assert default_freezone_image_model() == FREEZONE_DEFAULT_IMAGE_MODEL


@pytest.mark.asyncio
async def test_template_edit_without_model_resolves_local_doubao_work(monkeypatch):
    from pathlib import Path
    from types import SimpleNamespace

    from novelvideo.api.routes import freezone
    from novelvideo.api.schemas import FreezoneTemplateEditRequest
    from novelvideo.doubao_work import video

    monkeypatch.setattr(video, "local_channel_available", lambda: True)
    requested = []

    async def resolve_catalog(media_type, model, params, *, mode=None, requester_user_id=None):
        requested.append((media_type, model, mode))
        return {}, {}, freezone._native_image_catalog_entry()

    captured = {}

    async def enqueue(**kwargs):
        captured.update(kwargs)
        return {"ok": True, "data": {"task_type": "freezone_edit", "job_id": "x"}}

    async def project(*args, **kwargs):
        return SimpleNamespace(requester_user_id="local"), "local", "demo", Path("/tmp"), "/tmp"

    monkeypatch.setattr(freezone, "_resolve_freezone_project", project)
    monkeypatch.setattr(freezone, "_resolve_catalog_request", resolve_catalog)
    monkeypatch.setattr(freezone, "_start_or_enqueue_freezone_edit_job", enqueue)

    body = FreezoneTemplateEditRequest(
        source_url="/static/a.png",
        mode="product_three_view",
        prompt="产品三视图",
    )
    await freezone.freezone_template_edit("demo", body, {})
    assert requested == [("image", "doubao_work", "image_to_image")]
    assert captured["provider"] == "doubao_work"
    assert captured["model"] == "doubao_work"
