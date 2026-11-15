from unittest.mock import AsyncMock, Mock

import pytest

from novelvideo.doubao_work import service
from novelvideo.doubao_work.jobs import JobStore
from test_doubao_breakdown import AnalysisClient


@pytest.fixture
def recipe_store(tmp_path, monkeypatch):
    store = JobStore(tmp_path / "state", tmp_path / "output",
                     AnalysisClient(response="运行结论：通过\n真实证据已整理。"))
    monkeypatch.setattr(service, "store", lambda: store)
    return store


@pytest.mark.asyncio
async def test_native_recipe_reuses_receipt_after_restart(recipe_store, monkeypatch):
    from novelvideo.doubao_work.recipe import generate_text
    args = dict(scope="local/project/intake", run_id="run-1", prompt="安装的配方和真实输入",
                poll_interval=0, max_polls=1)
    first = await generate_text(**args)
    monkeypatch.setattr(service, "store", lambda: JobStore(
        recipe_store.state, recipe_store.output, recipe_store.client))
    assert await generate_text(**args) == first
    assert len([p for p, _ in recipe_store.client.posts if p.endswith("/events")]) == 1


@pytest.mark.asyncio
async def test_native_recipe_unknown_never_resubmits(recipe_store):
    from novelvideo.doubao_work.recipe import generate_text
    recipe_store.client.unknown = True
    for run_id in ("run-1", "run-2"):
        with pytest.raises(RuntimeError, match="原任务"):
            await generate_text(scope="local/project/intake", run_id=run_id,
                                prompt="安装的配方", poll_interval=0, max_polls=1)
    assert len(recipe_store.client.posts) == 1


@pytest.mark.asyncio
async def test_pending_recipe_refuses_changed_inputs(recipe_store):
    from novelvideo.doubao_work.recipe import generate_text
    recipe_store.client.pending = True
    with pytest.raises(RuntimeError, match="原任务"):
        await generate_text(scope="local/project/intake", run_id="run-1",
                            prompt="商品甲", poll_interval=0, max_polls=1)
    before = len(recipe_store.client.posts)
    with pytest.raises(RuntimeError, match="原任务"):
        await generate_text(scope="local/project/intake", run_id="run-2",
                            prompt="商品乙", poll_interval=0, max_polls=1)
    assert len(recipe_store.client.posts) == before


@pytest.mark.asyncio
async def test_recipe_runtime_uses_installed_method_and_native_receipt(monkeypatch):
    from novelvideo.freezone import recipe_runtime
    from novelvideo.doubao_work import video
    monkeypatch.setattr(video, "local_channel_available", lambda: True)
    recipe = {"id": "test-intake", "version": "1", "output_kind": "text",
              "system_prompt": "检查真实素材，缺失就暂停。"}
    monkeypatch.setattr(recipe_runtime, "list_user_agent_config_items",
                        lambda username, kind: [recipe] if kind == "recipes" else [])
    native = AsyncMock(return_value="运行结论：通过")
    from novelvideo.doubao_work import recipe as native_recipe
    monkeypatch.setattr(native_recipe, "generate_text", native)
    result = await recipe_runtime.generate_recipe_text(
        username="local", recipe_id="test-intake", node_kind="text",
        node_prompt="检查本批次", upstream_text="已核实的原片证据",
        native_scope="local/project/node", native_run_id="operation-1",
    )
    assert result == "运行结论：通过"
    call = native.call_args.kwargs
    assert "检查真实素材，缺失就暂停。" in call["prompt"]
    assert "已核实的原片证据" in call["prompt"]
    assert call["run_id"] == "operation-1"
    assert "文本阶段输出合同" not in call["prompt"]


@pytest.mark.asyncio
@pytest.mark.parametrize("origin,peer", [
    ("https://external.example", "127.0.0.1"), (None, "192.0.2.1"),
])
async def test_recipe_route_rejects_nonlocal_implicit_native(monkeypatch, origin, peer):
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
        "path": "/api/v1/freezone/recipes/generate-text",
        "query_string": b"", "headers": headers, "client": (peer, 1234),
        "server": ("127.0.0.1", 8780),
    })
    with pytest.raises(HTTPException) as error:
        await guard_native_media_selection(request)
    assert error.value.status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize("response,expected", [
    ("运行结论：暂停\n当前通道不支持参考视频", None),
    ("运行结论：通过\n", None),
    ("这是候选提示词，但未核实能力", None),
    ("运行结论：通过\n按原片及商品图执行已核实的镜头。", "按原片及商品图执行已核实的镜头。"),
])
async def test_native_video_recipe_requires_explicit_pass(monkeypatch, response, expected):
    from novelvideo.doubao_work import recipe as native_recipe
    from novelvideo.freezone import recipe_runtime, text_node

    recipe = {"id": "viral-video-render", "version": "1.1.0",
              "output_kind": "video", "system_prompt": "参考视频与单一商品图换商品。"}
    monkeypatch.setattr(recipe_runtime, "get_recipe_for_runtime", lambda **kw: recipe)
    monkeypatch.setattr(recipe_runtime, "get_recipe_pipeline_for_runtime", lambda **kw: [recipe])
    monkeypatch.setattr(recipe_runtime, "get_skill_for_runtime",
                        lambda **kw: {"id": "viral-video-oneclick", "version": "1.1.0"})
    monkeypatch.setattr(text_node, "_native_script_available", lambda context: True)
    native = AsyncMock(return_value=response)
    monkeypatch.setattr(native_recipe, "generate_text", native)
    args = dict(username="local", recipe_id=recipe["id"], node_kind="video",
                node_prompt="本批次原片与商品图", skill_id="viral-video-oneclick")
    if expected is None:
        with pytest.raises(recipe_runtime.RecipeRuntimeError, match="阶段暂停"):
            await recipe_runtime.compile_recipe_prompt_result(**args)
    else:
        compiled = await recipe_runtime.compile_recipe_prompt_result(**args)
        assert compiled.prompt == expected
        assert compiled.mode == "model"
    assert native.await_count == 1
    assert recipe["system_prompt"] in native.call_args.kwargs["prompt"]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["image", "video"])
@pytest.mark.parametrize("response,expected", [
    ("运行结论：暂停\n商品外观尚未确认", None),
    ("运行结论：待核验\n镜头证据缺失", None),
    ("运行结论：通过\n", None),
    ("候选提示词", None),
    ("运行结论：通过\n保留原镜头构图与当前商品的真实外观。", "保留原镜头构图与当前商品的真实外观。"),
])
async def test_keyframe_native_media_recipe_requires_pass(monkeypatch, kind, response, expected):
    from novelvideo.doubao_work import recipe as native_recipe
    from novelvideo.freezone import recipe_runtime, text_node

    recipe = {"id": f"keyframe-{kind}", "version": "2.0.0",
              "output_kind": kind, "system_prompt": "关键帧重建配方。"}
    monkeypatch.setattr(recipe_runtime, "get_recipe_for_runtime", lambda **kw: recipe)
    monkeypatch.setattr(recipe_runtime, "get_recipe_pipeline_for_runtime", lambda **kw: [recipe])
    monkeypatch.setattr(recipe_runtime, "get_skill_for_runtime",
                        lambda **kw: {"id": "viral-video-keyframe-oneclick", "version": "2.0.0"})
    monkeypatch.setattr(text_node, "_native_script_available", lambda context: True)
    native = AsyncMock(return_value=response)
    monkeypatch.setattr(native_recipe, "generate_text", native)
    args = dict(username="local", recipe_id=recipe["id"], node_kind=kind,
                node_prompt="原片关键帧与新商品图",
                skill_id="viral-video-keyframe-oneclick")
    if expected is None:
        with pytest.raises(recipe_runtime.RecipeRuntimeError, match="阶段暂停"):
            await recipe_runtime.compile_recipe_prompt_result(**args)
    else:
        result = await recipe_runtime.compile_recipe_prompt_result(**args)
        assert result.prompt == expected
        assert result.mode == "model"
    assert native.await_count == 1
    if kind == "image":
        assert "最终图片提示词" in native.call_args.kwargs["prompt"]


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy", ["llm_refine", "template", "previous_output"])
async def test_keyframe_media_cannot_bypass_native_gate(monkeypatch, strategy):
    from novelvideo.freezone import recipe_runtime, text_node

    recipe = {"id": "keyframe-image", "version": "2.0.0", "output_kind": "image",
              "system_prompt": "关键帧重建配方。"}
    monkeypatch.setattr(recipe_runtime, "get_recipe_for_runtime", lambda **kw: recipe)
    monkeypatch.setattr(recipe_runtime, "get_recipe_pipeline_for_runtime", lambda **kw: [recipe])
    monkeypatch.setattr(recipe_runtime, "get_skill_for_runtime",
                        lambda **kw: {"id": "viral-video-keyframe-oneclick", "version": "2.0.0"})
    monkeypatch.setattr(text_node, "_native_script_available", lambda context: False)
    generic = AsyncMock(side_effect=AssertionError("must not call generic gateway"))
    monkeypatch.setattr(recipe_runtime, "_compile_with_music_intent", generic)
    with pytest.raises(recipe_runtime.RecipeRuntimeError, match="豆包工作"):
        await recipe_runtime.compile_recipe_prompt_result(
            username="local", recipe_id=recipe["id"], node_kind="image",
            node_prompt="原帧与商品图", skill_id="viral-video-keyframe-oneclick",
            prompt_strategy=strategy,
        )
    generic.assert_not_awaited()


@pytest.mark.asyncio
async def test_keyframe_text_requires_doubao_authorization(monkeypatch):
    from novelvideo.freezone import recipe_runtime, text_node

    recipe = {"id": "keyframe-intake", "version": "2.0.0", "output_kind": "text",
              "system_prompt": "检查素材与能力。"}
    monkeypatch.setattr(recipe_runtime, "get_recipe_for_runtime", lambda **kw: recipe)
    monkeypatch.setattr(recipe_runtime, "get_recipe_pipeline_for_runtime", lambda **kw: [recipe])
    monkeypatch.setattr(recipe_runtime, "get_skill_for_runtime",
                        lambda **kw: {"id": "viral-video-keyframe-oneclick", "version": "2.0.0"})
    monkeypatch.setattr(text_node, "_native_script_available", lambda context: False)
    with pytest.raises(recipe_runtime.RecipeRuntimeError, match="豆包工作"):
        await recipe_runtime.generate_recipe_text(
            username="local", recipe_id=recipe["id"], node_kind="text",
            node_prompt="检查本批次", skill_id="viral-video-keyframe-oneclick",
        )


@pytest.fixture
def keyframe_text_args(monkeypatch):
    from novelvideo import config
    from novelvideo.freezone import recipe_runtime, text_node

    recipe = {"id": "keyframe-intake", "version": "2.0.0", "output_kind": "text",
              "system_prompt": "检查当前阶段真实素材，缺失就暂停。"}
    skill = {"id": "viral-video-keyframe-oneclick", "version": "2.0.0",
             "allowed_recipe_ids": [recipe["id"]],
             "planning": {"conduct_rules": ["不得编造媒体核验记录。"]}}
    monkeypatch.setattr(recipe_runtime, "list_user_agent_config_items",
                        lambda username, kind: [recipe] if kind == "recipes" else [skill])
    monkeypatch.setattr(text_node, "_native_script_available", lambda context: True)
    monkeypatch.setattr(config, "get_newapi_text_pydantic_model",
                        Mock(side_effect=AssertionError("must not call generic gateway")))
    return dict(
        username="local", recipe_id=recipe["id"], node_kind="text",
        skill_id=skill["id"], node_prompt="检查本批次素材",
        upstream_text="本地探测记录已提供；重建图和成片尚未验收。",
        reference_media=[{"kind": "video", "label": "原片", "url": "file:///source.mp4"}],
        native_scope="local/project/intake", native_run_id="operation-1",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [
    "运行结论：通过\n当前素材证据齐全，后续阶段尚未验收。",
    "运行结论：暂停\n当前阶段素材缺失。",
    "运行结论：待核验\n当前阶段证据不足。",
    "**阶段①运行结论：素材本地探测...通过，可进入阶段②...**",
])
async def test_keyframe_text_enforces_stage_output_contract(monkeypatch, keyframe_text_args, response):
    from novelvideo.doubao_work import recipe as native_recipe
    from novelvideo.freezone import recipe_runtime

    native = AsyncMock(return_value=response)
    monkeypatch.setattr(native_recipe, "generate_text", native)
    result = await recipe_runtime.generate_recipe_text(**keyframe_text_args)
    assert result == response
    native.assert_awaited_once()
    call = native.call_args.kwargs
    assert call["scope"] == keyframe_text_args["native_scope"]
    assert call["run_id"] == keyframe_text_args["native_run_id"]
    prompt = call["prompt"]
    assert "第一非空行必须精确为以下三者之一" in prompt
    for conclusion in ("运行结论：通过", "运行结论：暂停", "运行结论：待核验"):
        assert conclusion in prompt
    assert "不能用阶段前缀、标题或说明替代首行" in prompt
    assert "不得添加 Markdown 标记或其他文字" in prompt
    assert "只评估当前阶段" in prompt
    assert "正文可以报告后续阶段尚未验收" in prompt
    assert "中文输出" in prompt
    assert "仅根据已提供的文本证据" in prompt
    assert "不能声称已读取媒体或执行工具" in prompt
    assert "检查当前阶段真实素材，缺失就暂停。" in prompt
    assert "不得编造媒体核验记录。" in prompt
    assert keyframe_text_args["upstream_text"] in prompt
    assert "file:///source.mp4" not in prompt


@pytest.mark.asyncio
async def test_keyframe_text_unknown_has_no_retry_or_gateway_fallback(monkeypatch, keyframe_text_args):
    from novelvideo.doubao_work import recipe as native_recipe
    from novelvideo.freezone import recipe_runtime

    native = AsyncMock(side_effect=RuntimeError("结果未知；请核验原任务"))
    monkeypatch.setattr(native_recipe, "generate_text", native)
    with pytest.raises(RuntimeError, match="结果未知.*原任务"):
        await recipe_runtime.generate_recipe_text(**keyframe_text_args)
    native.assert_awaited_once()
