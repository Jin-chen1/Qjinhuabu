from unittest.mock import AsyncMock

import pytest
from starlette.requests import Request

from novelvideo.chat import service as chat_service
from novelvideo.doubao_work import service, video
from novelvideo.doubao_work.jobs import JobStore
from test_doubao_breakdown import AnalysisClient


@pytest.fixture
def native_chat(tmp_path, monkeypatch):
    store = JobStore(tmp_path / "state", tmp_path / "output",
                     AnalysisClient(response="我是豆包工作提供的助手。"))
    monkeypatch.setattr(service, "store", lambda: store)
    monkeypatch.setattr(video, "local_channel_available", lambda: True)
    monkeypatch.setattr(chat_service, "is_codex_backend_available", lambda: True)
    monkeypatch.setenv("DRAMACLAW_CHAT_BACKEND", "codex")
    from novelvideo.chat import hermes_workspace
    monkeypatch.setattr(hermes_workspace, "effective_gateway_credentials", lambda: ("", "https://gateway"))
    return store


def test_local_chat_without_key_selects_native(native_chat):
    assert chat_service.get_chat_backend_name() == "doubao_work"


def test_configured_gateway_keeps_codex(native_chat, monkeypatch):
    from novelvideo.chat import hermes_workspace
    monkeypatch.setattr(hermes_workspace, "effective_gateway_credentials",
                        lambda: ("configured-key", "https://gateway"))
    assert chat_service.get_chat_backend_name() == "codex"


def test_nonlocal_chat_keeps_codex_guard(native_chat, monkeypatch):
    monkeypatch.setattr(video, "local_channel_available", lambda: False)
    assert chat_service.get_chat_backend_name() == "codex"
    with pytest.raises(RuntimeError, match="per-turn"):
        chat_service._codex_turn_gateway_credentials()


@pytest.mark.asyncio
async def test_chat_dispatch_persists_and_emits_reply(native_chat, tmp_path, monkeypatch):
    monkeypatch.setattr(chat_service, "_stream_assistant_reply_codex",
                        AsyncMock(side_effect=AssertionError("must not invoke Codex")))
    on_event = AsyncMock()
    result = await chat_service.stream_assistant_reply(
        "local", "project", "你是什么模型", on_event,
        project_dir=tmp_path / "project", project_state_dir=tmp_path / "chat",
        turn_id="turn-one",
    )
    assert result["content"] == native_chat.client.response
    assert on_event.call_args_list[-1].args[0]["type"] == "done"
    messages = chat_service.list_messages(
        "local", "project", project_dir=tmp_path / "project",
        project_state_dir=tmp_path / "chat",
    )
    assert messages[-1]["content"] == result["content"]


@pytest.mark.asyncio
async def test_timeout_reuses_original_job(native_chat):
    from novelvideo.doubao_work.chat import generate_reply
    native_chat.client.pending = True
    options = dict(scope="user/canvas", prompt="你是什么模型",
                   history=[], poll_interval=0, max_polls=1)
    with pytest.raises(RuntimeError, match="原任务"):
        await generate_reply(**options, turn_id="first")
    native_chat.client.pending = False
    assert await generate_reply(**options, turn_id="second") == native_chat.client.response
    assert len([p for p, _ in native_chat.client.posts if p.endswith("/events")]) == 1


@pytest.mark.asyncio
async def test_unknown_submission_never_repeats(native_chat):
    from novelvideo.doubao_work.chat import generate_reply
    native_chat.client.unknown = True
    for turn in ("first", "second"):
        with pytest.raises(RuntimeError, match="原任务"):
            await generate_reply(scope="user/canvas", prompt="你好", history=[], turn_id=turn)
    assert len(native_chat.client.posts) == 1


@pytest.mark.asyncio
async def test_pending_chat_refuses_changed_prompt(native_chat):
    from novelvideo.doubao_work.chat import generate_reply
    native_chat.client.pending = True
    with pytest.raises(RuntimeError, match="原任务"):
        await generate_reply(scope="user/canvas", prompt="你好", history=[], turn_id="first",
                             poll_interval=0, max_polls=1)
    count = len(native_chat.client.posts)
    with pytest.raises(RuntimeError, match="原任务"):
        await generate_reply(scope="user/canvas", prompt="另一问题", history=[], turn_id="second")
    assert len(native_chat.client.posts) == count


@pytest.mark.parametrize("peer,origin", [
    ("192.0.2.1", None), ("127.0.0.1", "https://external.example"),
])
def test_native_chat_requires_loopback(native_chat, peer, origin):
    from fastapi import HTTPException
    from novelvideo.api.routes.chat import _guard_native_chat
    headers = [(b"host", b"127.0.0.1:8780")]
    if origin:
        headers.append((b"origin", origin.encode()))
    request = Request({"type": "http", "headers": headers, "client": (peer, 1234)})
    with pytest.raises(HTTPException, match="403"):
        _guard_native_chat(request)


def test_websocket_route_still_uses_chat_handler():
    from novelvideo.api.routes.chat import router, chat_ws
    assert next(r for r in router.routes if r.path == "/chat/ws").endpoint is chat_ws


@pytest.mark.asyncio
async def test_home_uses_backend_neutral_service(native_chat, monkeypatch):
    from novelvideo.api.routes import chat
    from novelvideo.chat.store import ChatScope
    runner = AsyncMock()
    monkeypatch.setattr(chat, "_stream_home_turn_codex", runner)
    await chat._stream_home_turn(
        websocket=None, user={}, username="local", scope=ChatScope(kind="home"),
        text="你好", attachments=[], turn_id="home-turn",
    )
    runner.assert_awaited_once()


@pytest.mark.asyncio
async def test_native_rejects_organization_context(native_chat):
    with pytest.raises(RuntimeError, match="个人授权"):
        await chat_service.stream_assistant_reply(
            "local", "project", "你好", AsyncMock(),
            backend="doubao_work", egress_context=object(),
        )
    assert not native_chat.client.posts


@pytest.mark.asyncio
async def test_canvas_scoped_history_survives_repeated_turn(native_chat, tmp_path):
    from novelvideo.chat.store import ChatScope, chat_store
    scope = ChatScope(kind="project", id="test-project", surface="freezone",
                      canvas_id="canvas-a", state_dir=str(tmp_path / "state"))
    await chat_store.append_message_async("local", scope, "user", "我在做眼镜广告")
    await chat_store.append_message_async("local", scope, "assistant", "好的")
    await chat_store.append_message_async("local", scope, "user", "下一步呢")
    for _ in range(2):
        await chat_service.stream_assistant_reply(
            "local", "test-project", "下一步呢", AsyncMock(),
            store_scope=scope, turn_id="same-turn",
        )
    messages = await chat_store.list_messages_async("local", scope)
    assert len(messages) == 4
    event = next(body for path, body in native_chat.client.posts if path.endswith("/events"))
    assert "我在做眼镜广告" in event["text"]
    assert "本机文件读取" in event["text"]
    assert len([p for p, _ in native_chat.client.posts if p.endswith("/events")]) == 1
    other = ChatScope(kind="project", id="test-project", surface="freezone",
                      canvas_id="canvas-b", state_dir=scope.state_dir)
    assert not await chat_store.list_messages_async("local", other)
