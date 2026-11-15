"""Offline contracts for the official Ark dual-reference adapter."""

import asyncio
import base64
import importlib
import json
import shutil
import socket
import subprocess
import sys
import types
from pathlib import Path

import httpx
import pytest
from PIL import Image

from novelvideo.generators.video_generator import (
    ShotReference,
    VideoGeneratorBase,
    VideoGenResult,
    VideoGenStatus,
)

MODULE = "novelvideo.generators.ark_dual_reference"
BASE = "https://ark.cn-beijing.volces.com/api/v3"
MODEL = "doubao-seedance-2-5-260628"
KEY = "offline-test-credential"
VIDEO = "https://media.example.test/reference.mp4"
IMAGE = "https://media.example.test/product.png"
RESULT_URL = "https://media.example.test/result.mp4?signature=private-test"
TASK = "cgt-contract-001"


def test_adapter_is_importable():
    assert importlib.util.find_spec(MODULE) is not None, (
        "Ark adapter is not implemented"
    )


@pytest.fixture
def adapter():
    assert importlib.util.find_spec(MODULE) is not None, (
        "Ark adapter is not implemented"
    )
    return importlib.import_module(MODULE)


@pytest.fixture(autouse=True)
def offline_dns(monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))
        ],
    )


@pytest.fixture(scope="module")
def mp4_bytes(tmp_path_factory):
    directory = tmp_path_factory.mktemp("ark-contract-media")
    executable = shutil.which("ffmpeg") or str(Path.home() / ".local/bin/ffmpeg")
    target = directory / "test.mp4"
    subprocess.run(
        [
            executable,
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=size=320x320:rate=24",
            "-t",
            "0.25",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(target),
        ],
        check=True,
        capture_output=True,
        timeout=30,
    )
    return target.read_bytes()


@pytest.fixture(scope="module")
def local_video(tmp_path_factory):
    target = tmp_path_factory.mktemp("ark-reference") / "reference.mp4"
    executable = shutil.which("ffmpeg") or str(Path.home() / ".local/bin/ffmpeg")
    subprocess.run(
        [
            executable,
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=size=640x720:rate=24",
            "-t",
            "5",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(target),
        ],
        check=True,
        capture_output=True,
        timeout=30,
    )
    return target


def refs(image=IMAGE, video=VIDEO):
    return [
        ShotReference("video", video, "reference_video"),
        ShotReference("image", image, "reference_image"),
    ]


def generator(adapter, tmp_path, handler, **kwargs):
    return adapter.ArkDualReferenceVideoGenerator(
        api_key=KEY,
        base_url=BASE,
        model=kwargs.pop("model", MODEL),
        state_dir=tmp_path / "state",
        transport=httpx.MockTransport(handler),
        **kwargs,
    )


def task(status="succeeded", **kwargs):
    return {
        "id": TASK,
        "model": MODEL,
        "status": status,
        "content": {"video_url": RESULT_URL, "last_frame_url": IMAGE},
        "duration": 5,
        "usage": {"completion_tokens": 120, "total_tokens": 120},
        **kwargs,
    }


async def generate(g, tmp_path, **kwargs):
    return await g.generate(
        image_path=None,
        prompt=kwargs.pop("prompt", "Use video motion and image product."),
        output_path=str(tmp_path / "result.mp4"),
        references=kwargs.pop("references", refs()),
        poll_interval=0,
        max_polls=kwargs.pop("max_polls", 2),
        **kwargs,
    )


async def test_factory_compatible_result_and_exact_official_payload(
    adapter, tmp_path, mp4_bytes
):
    calls = []

    def handler(request):
        calls.append(request)
        if request.method == "POST":
            assert str(request.url) == BASE + "/contents/generations/tasks"
            assert request.headers["authorization"] == "Bearer " + KEY
            assert json.loads(request.content) == {
                "model": MODEL,
                "content": [
                    {"type": "text", "text": "Use video motion and image product."},
                    {
                        "type": "video_url",
                        "video_url": {"url": VIDEO},
                        "role": "reference_video",
                    },
                    {
                        "type": "image_url",
                        "image_url": {"url": IMAGE},
                        "role": "reference_image",
                    },
                ],
                "ratio": "adaptive",
                "duration": -1,
                "resolution": "720p",
                "omni_reference_task_type": "edit",
                "generate_audio": True,
                "return_last_frame": False,
            }
            return httpx.Response(
                200, json={"id": TASK}, headers={"x-request-id": "req-1"}
            )
        if request.url.host == "ark.cn-beijing.volces.com":
            assert request.url.path.endswith("/tasks/" + TASK)
            return httpx.Response(200, json=task())
        assert "authorization" not in request.headers
        assert "cookie" not in request.headers
        return httpx.Response(
            200, content=mp4_bytes, headers={"content-type": "video/mp4"}
        )

    g = generator(adapter, tmp_path, handler)
    assert isinstance(g, VideoGeneratorBase)
    result = await generate(g, tmp_path)
    assert isinstance(result, VideoGenResult)
    assert result.status == VideoGenStatus.DONE
    assert result.task_id == result.provider_task_id == TASK
    assert result.acceptance == "accepted"
    assert result.request_id == "req-1"
    assert result.duration_seconds == pytest.approx(0.25, abs=0.05)
    assert Path(result.video_path).read_bytes() == mp4_bytes
    assert sum(r.method == "POST" for r in calls) == 1
    assert adapter.BACKEND == "ark_dual_reference"
    assert KEY not in repr(g)
    assert RESULT_URL not in repr(result)
    state = b"".join(p.read_bytes() for p in (tmp_path / "state").glob("*.json"))
    assert KEY.encode() not in state
    assert b"signature=" not in state
    assert b"Use video motion" not in state


@pytest.mark.parametrize(
    "model",
    [
        MODEL,
        "doubao-seedance-2-0-260128",
        "doubao-seedance-2-0-fast-260128",
        "doubao-seedance-2-0-mini-260615",
    ],
)
def test_confirmed_model_ids(adapter, tmp_path, model):
    g = generator(adapter, tmp_path, lambda _: pytest.fail("no network"), model=model)
    assert g.model == model


@pytest.mark.parametrize(
    "model",
    [
        "seedance-2.0",
        "seedance-2.0-i2v",
        "ep-unverified",
        "doubao-seedance-1-5-pro-251215",
        "doubao-seedance-2-5",
        "doubao-seedance-2-0-mini-260128",
    ],
)
def test_unconfirmed_models_rejected(adapter, tmp_path, model):
    with pytest.raises(adapter.ArkDualReferenceError) as exc:
        generator(adapter, tmp_path, lambda _: pytest.fail("no network"), model=model)
    assert exc.value.code == "ARK_MODEL_UNSUPPORTED"
    assert model not in str(exc.value)


@pytest.mark.parametrize(
    "base_url",
    [
        "https://attacker.test/api/v3",
        BASE + "?key=" + KEY,
        "http://ark.cn-beijing.volces.com/api/v3",
        "https://user:password@ark.cn-beijing.volces.com/api/v3",
    ],
)
def test_credentials_bound_to_exact_official_origin(adapter, tmp_path, base_url):
    with pytest.raises(adapter.ArkDualReferenceError) as exc:
        adapter.ArkDualReferenceVideoGenerator(
            api_key=KEY, base_url=base_url, model=MODEL
        )
    assert exc.value.code == "ARK_BASE_URL_INVALID"
    assert "password" not in str(exc.value)
    assert KEY not in str(exc.value)


async def test_models_authorization_probe_is_not_generation_verification(
    adapter, tmp_path
):
    calls = []

    def handler(request):
        calls.append(request)
        assert request.method == "GET"
        assert str(request.url) == BASE + "/models"
        return httpx.Response(
            200,
            json={
                "data": [
                    {"id": MODEL},
                    {"id": "doubao-seedance-2-0-fast-260128"},
                    {"id": "seedance-2.0"},
                    {"id": "doubao-seedance-1-0-pro-250528"},
                ]
            },
        )

    result = await generator(adapter, tmp_path, handler).validate_authorization()
    assert result.authorized
    assert result.verification_level == "models"
    assert not result.dual_reference_verified
    assert result.available_models == (MODEL, "doubao-seedance-2-0-fast-260128")
    assert len(calls) == 1
    assert not (tmp_path / "state").exists()


@pytest.mark.parametrize("status", [401, 403, 500])
async def test_models_probe_errors_are_safe(adapter, tmp_path, status):
    g = generator(
        adapter,
        tmp_path,
        lambda _: httpx.Response(
            status,
            json={"error": {"message": KEY + " " + RESULT_URL}},
        ),
    )
    result = await g.validate_authorization()
    assert not result.authorized
    assert not result.dual_reference_verified
    assert result.status_code == status
    assert KEY not in repr(result)
    assert "signature=" not in repr(result)


async def test_local_image_uses_verified_data_uri_and_reference_not_first_frame(
    adapter, tmp_path
):
    image = tmp_path / "misleading.jpg"
    Image.new("RGB", (320, 320)).save(image, format="PNG")
    payloads = []

    def handler(request):
        payloads.append(json.loads(request.content))
        return httpx.Response(400, json={"error": {"code": "InvalidParameter"}})

    g = generator(adapter, tmp_path, handler)
    result = await generate(g, tmp_path, references=refs(str(image)))
    assert result.acceptance == "not_accepted"
    content = payloads[0]["content"][2]
    assert content["role"] == "reference_image"
    assert content["image_url"]["url"] == (
        "data:image/png;base64," + base64.b64encode(image.read_bytes()).decode()
    )


async def test_video_data_uri_is_not_claimed_supported(adapter, tmp_path):
    g = generator(
        adapter, tmp_path, lambda _: pytest.fail("must fail before submission")
    )
    result = await generate(
        g, tmp_path, references=refs(video="data:video/mp4;base64,AAAA")
    )
    assert result.status == VideoGenStatus.FAILED
    assert result.acceptance == "not_accepted"
    assert result.error_code == "ARK_VIDEO_PUBLIC_URL_REQUIRED"


@pytest.mark.parametrize(
    "image",
    [
        "http://127.0.0.1/private.png",
        "https://169.254.169.254/private.png",
        "https://localhost/private.png",
        "file:///private.png",
        "data:image/png;base64,AAAA",
    ],
)
async def test_invalid_or_nonpublic_image_rejected(adapter, tmp_path, image):
    g = generator(
        adapter, tmp_path, lambda _: pytest.fail("must fail before submission")
    )
    result = await generate(g, tmp_path, references=refs(image=image))
    assert result.acceptance == "not_accepted"
    assert result.error_code in {"ARK_MEDIA_INVALID", "ARK_MEDIA_URL_INVALID"}


@pytest.mark.parametrize(
    "kwargs",
    [
        {"duration": 3, "video_mode": "all_reference"},
        {"duration": 15.5, "video_mode": "all_reference"},
        {"duration": 31, "video_mode": "all_reference"},
        {"aspect_ratio": "auto", "gen_mode": "all_reference"},
        {"last_frame_path": "tail.png"},
        {"video_mode": "first_frame"},
        {"references": [ShotReference("image", IMAGE, "reference_image")]},
        {"references": refs() + [ShotReference("audio", IMAGE, "reference_audio")]},
        {
            "references": refs()
            + [
                ShotReference("image", IMAGE + f"?image={i}", "reference_image")
                for i in range(30)
            ]
        },
        {"references": [ShotReference("image", IMAGE, "first_frame"), refs()[0]]},
    ],
)
async def test_invalid_generation_contract_stops_before_post(adapter, tmp_path, kwargs):
    g = generator(
        adapter, tmp_path, lambda _: pytest.fail("must fail before submission")
    )
    result = await generate(g, tmp_path, **kwargs)
    assert result.status == VideoGenStatus.FAILED
    assert result.acceptance == "not_accepted"


@pytest.mark.parametrize(
    "status, acceptance",
    [
        (400, "not_accepted"),
        (401, "not_accepted"),
        (403, "not_accepted"),
        (413, "not_accepted"),
        (429, "not_accepted"),
        (408, "outcome_unknown"),
        (409, "outcome_unknown"),
        (500, "outcome_unknown"),
    ],
)
async def test_submit_http_classification_and_recreation_no_repeat(
    adapter, tmp_path, status, acceptance
):
    calls = []
    logs = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            status,
            json={
                "error": {
                    "code": "InvalidParameter",
                    "message": KEY + " " + RESULT_URL,
                }
            },
            headers={"x-request-id": "req-safe"},
        )

    first = await generate(
        generator(adapter, tmp_path, handler), tmp_path, on_log=logs.append
    )
    second = await generate(generator(adapter, tmp_path, handler), tmp_path)
    assert first.acceptance == second.acceptance == acceptance
    assert first.request_id == "req-safe"
    assert first.status_code == status
    assert len(calls) == 1
    assert KEY not in repr(first) + repr(second) + str(logs)
    assert "signature=" not in repr(first) + repr(second) + str(logs)


@pytest.mark.parametrize(
    "error_type, acceptance",
    [
        (httpx.ConnectError, "not_accepted"),
        (httpx.ConnectTimeout, "not_accepted"),
        (httpx.PoolTimeout, "not_accepted"),
        (httpx.WriteTimeout, "outcome_unknown"),
        (httpx.ReadTimeout, "outcome_unknown"),
        (httpx.ReadError, "outcome_unknown"),
        (httpx.RemoteProtocolError, "outcome_unknown"),
    ],
)
async def test_transport_errors_never_retry_post(
    adapter, tmp_path, error_type, acceptance
):
    calls = []

    def handler(request):
        calls.append(request)
        raise error_type(KEY + " " + RESULT_URL, request=request)

    result = await generate(generator(adapter, tmp_path, handler), tmp_path)
    repeated = await generate(
        generator(adapter, tmp_path, handler), tmp_path, run_id="different"
    )
    assert result.acceptance == acceptance
    assert result.status == VideoGenStatus.FAILED
    assert len(calls) == (2 if acceptance == "not_accepted" else 1)
    assert repeated.acceptance == acceptance
    assert KEY not in repr(result)
    assert "signature=" not in repr(result)


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, content=b"<html>not JSON</html>"),
        httpx.Response(200, json={}),
        httpx.Response(200, json={"id": "../bad"}),
        httpx.Response(200, json={"id": KEY}),
    ],
)
async def test_missing_or_unsafe_receipt_is_unknown_and_not_resubmitted(
    adapter, tmp_path, response
):
    calls = []

    def handler(request):
        calls.append(request)
        return response

    result = await generate(generator(adapter, tmp_path, handler), tmp_path)
    repeated = await generate(generator(adapter, tmp_path, handler), tmp_path)
    assert result.acceptance == repeated.acceptance == "outcome_unknown"
    assert result.task_id is None
    assert len(calls) == 1


@pytest.mark.parametrize(
    "status, expected",
    [
        ("queued", VideoGenStatus.PENDING),
        ("running", VideoGenStatus.PROCESSING),
        ("succeeded", VideoGenStatus.DONE),
        ("failed", VideoGenStatus.FAILED),
        ("expired", VideoGenStatus.FAILED),
        ("cancelled", VideoGenStatus.FAILED),
    ],
)
async def test_get_task_status_contract(adapter, tmp_path, status, expected):
    g = generator(
        adapter,
        tmp_path,
        lambda _: httpx.Response(
            200,
            json=task(
                status,
                error={
                    "code": "OutputVideoSensitiveContentDetected",
                    "message": KEY + " " + RESULT_URL,
                },
            ),
        ),
    )
    result = await g.get_result(TASK)
    assert result.status == expected
    assert result.task_id == result.provider_task_id == TASK
    assert result.acceptance == "accepted"
    assert result.usage == {"completion_tokens": 120, "total_tokens": 120}
    assert KEY not in repr(result)
    assert "signature=" not in repr(result)
    if status == "succeeded":
        assert result.video_url == RESULT_URL
        assert result.last_frame_url == IMAGE
    if status == "failed":
        assert result.error_code == "ARK_TASK_FAILED"
        assert result.provider_error_code == "OutputVideoSensitiveContentDetected"


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(404, json={"error": {"message": KEY}}),
        httpx.Response(500, json={}),
        httpx.Response(200, json=task("new-status")),
        httpx.Response(200, json=task(id="cgt-other")),
        httpx.Response(200, json=task(model="unknown-model")),
        httpx.Response(200, json=task(content={})),
        httpx.Response(200, json=[]),
    ],
)
async def test_query_indeterminate_preserves_accepted_task(adapter, tmp_path, response):
    g = generator(adapter, tmp_path, lambda _: response)
    result = await g.get_result(TASK)
    assert result.status == VideoGenStatus.PROCESSING
    assert result.task_id == TASK
    assert result.acceptance == "accepted"
    assert result.error_code == "ARK_QUERY_UNKNOWN"


async def test_poll_timeout_and_recreated_adapter_resume_same_task(
    adapter, tmp_path, mp4_bytes
):
    posts = []
    complete = False

    def handler(request):
        if request.method == "POST":
            posts.append(request)
            return httpx.Response(200, json={"id": TASK})
        if request.url.host == "ark.cn-beijing.volces.com":
            return httpx.Response(
                200, json=task("succeeded" if complete else "running")
            )
        return httpx.Response(200, content=mp4_bytes)

    first = await generate(generator(adapter, tmp_path, handler), tmp_path, max_polls=1)
    assert first.acceptance == "accepted"
    assert first.task_id == TASK
    assert first.status == VideoGenStatus.PROCESSING
    assert first.error_code == "ARK_POLL_TIMEOUT"
    complete = True
    second = await generate(
        generator(adapter, tmp_path, handler), tmp_path, run_id="new-ui-retry"
    )
    assert second.status == VideoGenStatus.DONE
    assert len(posts) == 1


async def test_changed_input_does_not_resubmit_unsettled_task(adapter, tmp_path):
    posts = []

    def handler(request):
        if request.method == "POST":
            posts.append(request)
            return httpx.Response(200, json={"id": TASK})
        return httpx.Response(200, json=task("running"))

    g = generator(adapter, tmp_path, handler)
    await generate(g, tmp_path, max_polls=1)
    result = await generate(g, tmp_path, prompt="Changed request", run_id="new")
    assert result.error_code == "ARK_INPUT_CONFLICT"
    assert result.task_id == TASK
    assert len(posts) == 1


async def test_concurrent_generators_only_one_post(adapter, tmp_path):
    posts = []
    submitted = asyncio.Event()
    release = asyncio.Event()

    async def handler(request):
        if request.method == "POST":
            posts.append(request)
            submitted.set()
            await release.wait()
            return httpx.Response(200, json={"id": TASK})
        return httpx.Response(200, json=task("running"))

    pending = asyncio.create_task(
        generate(generator(adapter, tmp_path, handler), tmp_path)
    )
    await submitted.wait()
    concurrent = await generate(generator(adapter, tmp_path, handler), tmp_path)
    release.set()
    first = await pending
    assert concurrent.acceptance == "outcome_unknown"
    assert first.acceptance == "accepted"
    assert len(posts) == 1


async def test_post_cancellation_leaves_unknown_receipt_binding(adapter, tmp_path):
    entered = asyncio.Event()
    posts = []

    async def handler(request):
        posts.append(request)
        entered.set()
        await asyncio.Event().wait()

    g = generator(adapter, tmp_path, handler)
    pending = asyncio.create_task(generate(g, tmp_path))
    await entered.wait()
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    result = await generate(
        generator(adapter, tmp_path, handler), tmp_path, run_id="new"
    )
    assert result.acceptance == "outcome_unknown"
    assert len(posts) == 1


async def test_callback_failure_after_acceptance_cannot_erase_task_id(
    adapter, tmp_path
):
    posts = []

    def handler(request):
        if request.method == "POST":
            posts.append(request)
            return httpx.Response(200, json={"id": TASK})
        return httpx.Response(200, json=task("running"))

    def broken_callback(_):
        raise RuntimeError(KEY)

    first = await generate(
        generator(adapter, tmp_path, handler),
        tmp_path,
        on_progress=broken_callback,
        on_log=broken_callback,
    )
    second = await generate(generator(adapter, tmp_path, handler), tmp_path)
    assert first.task_id == second.task_id == TASK
    assert len(posts) == 1


@pytest.mark.parametrize(
    "download",
    [
        httpx.Response(500, content=b"private server detail"),
        httpx.Response(200, content=b"not a video"),
        httpx.Response(302, headers={"location": "http://127.0.0.1/private"}),
    ],
)
async def test_download_failure_preserves_task_and_existing_file(
    adapter, tmp_path, download
):
    calls = []
    output = tmp_path / "result.mp4"
    output.write_bytes(b"existing-user-video")

    def handler(request):
        calls.append(request)
        if request.method == "POST":
            return httpx.Response(200, json={"id": TASK})
        if request.url.host == "ark.cn-beijing.volces.com":
            return httpx.Response(200, json=task())
        assert "authorization" not in request.headers
        return download

    result = await generate(generator(adapter, tmp_path, handler), tmp_path)
    repeated = await generate(generator(adapter, tmp_path, handler), tmp_path)
    assert result.error_code == repeated.error_code == "ARK_DELIVERY_FAILED"
    assert result.acceptance == "accepted"
    assert result.task_id == TASK
    assert output.read_bytes() == b"existing-user-video"
    assert len([r for r in calls if r.method == "POST"]) == 1
    assert not any(r.url.host == "127.0.0.1" for r in calls)
    assert not list(tmp_path.glob("*.part"))


async def test_corrupt_state_fails_closed_without_post(adapter, tmp_path):
    g = generator(adapter, tmp_path, lambda _: httpx.Response(200, json={"id": TASK}))
    await generate(g, tmp_path, max_polls=1)
    binding = next((tmp_path / "state").glob("*.json"))
    binding.write_text("{broken")
    result = await generate(
        generator(
            adapter, tmp_path, lambda _: pytest.fail("corrupt ledger must not submit")
        ),
        tmp_path,
    )
    assert result.error_code == "ARK_STATE_UNAVAILABLE"
    assert result.acceptance == "outcome_unknown"


@pytest.mark.parametrize(
    "model, duration",
    [
        (MODEL, 30),
        ("doubao-seedance-2-0-260128", 15),
    ],
)
async def test_reference_mode_duration_and_model_specific_parameters(
    adapter, tmp_path, model, duration
):
    payloads = []

    def handler(request):
        payloads.append(json.loads(request.content))
        return httpx.Response(400, json={"error": {"code": "InvalidParameter"}})

    result = await generate(
        generator(adapter, tmp_path, handler, model=model),
        tmp_path,
        duration=duration,
        aspect_ratio="9:16",
        video_mode="all_reference",
    )
    assert result.acceptance == "not_accepted"
    payload = payloads[0]
    assert payload["duration"] == duration
    assert payload["ratio"] == "9:16"
    if model == MODEL:
        assert payload["omni_reference_task_type"] == "reference"
    else:
        assert "omni_reference_task_type" not in payload


async def test_default_model_is_official_seedance25_edit(adapter, tmp_path):
    g = adapter.ArkDualReferenceVideoGenerator(
        api_key=KEY, state_dir=tmp_path / "state"
    )
    assert g.model == MODEL


async def test_local_video_calls_separate_relay_and_never_sends_base64(
    adapter,
    tmp_path,
    local_video,
    monkeypatch,
):
    from novelvideo.storage import media_relay

    calls = []
    payloads = []

    def unavailable():
        raise media_relay.MediaRelayConfigError("not configured")

    async def ensure_public_video(path):
        calls.append(Path(path))
        return VIDEO

    monkeypatch.setattr(media_relay, "get_media_relay", unavailable)
    module = types.ModuleType("novelvideo.dual_reference_media_relay")
    module.ensure_public_video = ensure_public_video
    monkeypatch.setitem(sys.modules, module.__name__, module)

    def handler(request):
        payloads.append(json.loads(request.content))
        return httpx.Response(400, json={})

    await generate(
        generator(adapter, tmp_path, handler),
        tmp_path,
        references=refs(video=str(local_video)),
    )
    assert calls == [local_video]
    assert payloads[0]["content"][1]["video_url"]["url"] == VIDEO
    assert not any("data:video" in json.dumps(p) for p in payloads)


async def test_configured_existing_relay_is_preferred(
    adapter,
    tmp_path,
    local_video,
    monkeypatch,
):
    from novelvideo.storage import media_relay

    calls = []

    class Relay:
        def upload_bytes(self, data, **kwargs):
            calls.append((data, kwargs))
            return VIDEO

    monkeypatch.setattr(media_relay, "get_media_relay", lambda: Relay())
    module = types.ModuleType("novelvideo.dual_reference_media_relay")

    async def forbidden(_):
        pytest.fail("existing configured relay should be used")

    module.ensure_public_video = forbidden
    monkeypatch.setitem(sys.modules, module.__name__, module)
    await generate(
        generator(adapter, tmp_path, lambda _: httpx.Response(400, json={})),
        tmp_path,
        references=refs(video=str(local_video)),
    )
    assert len(calls) == 1
    assert calls[0][0] == local_video.read_bytes()
    assert calls[0][1]["resource_type"] == "video"
    assert calls[0][1]["ttl"] >= 3600


async def test_missing_local_relay_fails_before_paid_submission(
    adapter,
    tmp_path,
    local_video,
    monkeypatch,
):
    from novelvideo.storage import media_relay

    def unavailable():
        raise media_relay.MediaRelayConfigError("contains " + KEY)

    monkeypatch.setattr(media_relay, "get_media_relay", unavailable)
    monkeypatch.setitem(sys.modules, "novelvideo.dual_reference_media_relay", None)
    result = await generate(
        generator(adapter, tmp_path, lambda _: pytest.fail("must not submit")),
        tmp_path,
        references=refs(video=str(local_video)),
    )
    assert result.acceptance == "not_accepted"
    assert result.error_code == "ARK_VIDEO_RELAY_UNAVAILABLE"
    assert KEY not in repr(result)


async def test_official_asset_video_needs_no_upload(adapter, tmp_path):
    payloads = []

    def handler(request):
        payloads.append(json.loads(request.content))
        return httpx.Response(400, json={})

    await generate(
        generator(adapter, tmp_path, handler),
        tmp_path,
        references=refs(video="asset://contract-video"),
    )
    assert payloads[0]["content"][1]["video_url"]["url"] == "asset://contract-video"


async def test_short_local_video_edit_rejected_before_relay(
    adapter, tmp_path, mp4_bytes
):
    video = tmp_path / "short.mp4"
    video.write_bytes(mp4_bytes)
    result = await generate(
        generator(adapter, tmp_path, lambda _: pytest.fail("must not submit")),
        tmp_path,
        references=refs(video=str(video)),
    )
    assert result.acceptance == "not_accepted"
    assert result.error_code == "ARK_VIDEO_CONSTRAINT_INVALID"


@pytest.mark.parametrize(
    "gen_mode, ratio, seconds, task_type",
    [
        ("video_edit", "adaptive", -1, "edit"),
        ("all_reference", "9:16", 5, "reference"),
    ],
)
async def test_main_factory_arguments_and_gen_mode(
    adapter,
    tmp_path,
    gen_mode,
    ratio,
    seconds,
    task_type,
):
    payloads = []

    def handler(request):
        payloads.append(json.loads(request.content))
        return httpx.Response(400, json={})

    g = generator(adapter, tmp_path, handler, resolution="1080p", generate_audio=False)
    await generate(g, tmp_path, gen_mode=gen_mode, aspect_ratio=ratio)
    assert payloads[0]["omni_reference_task_type"] == task_type
    assert payloads[0]["ratio"] == ratio
    assert payloads[0]["duration"] == seconds
    assert payloads[0]["resolution"] == "1080p"
    assert payloads[0]["generate_audio"] is False


async def test_edit_does_not_silently_override_explicit_geometry(adapter, tmp_path):
    result = await generate(
        generator(adapter, tmp_path, lambda _: pytest.fail("must not submit")),
        tmp_path,
        gen_mode="video_edit",
        aspect_ratio="9:16",
    )
    assert result.error_code == "ARK_INPUT_INVALID"
    assert result.acceptance == "not_accepted"


async def test_route_auto_geometry_only_normalized_for_video_edit(adapter, tmp_path):
    payloads = []
    logs = []

    def handler(request):
        payloads.append(json.loads(request.content))
        return httpx.Response(400, json={})

    result = await generate(
        generator(adapter, tmp_path, handler),
        tmp_path,
        gen_mode="video_edit",
        aspect_ratio="auto",
        on_log=logs.append,
    )
    assert payloads[0]["ratio"] == "adaptive"
    assert payloads[0]["duration"] == -1
    assert result.error_code == "ARK_SUBMIT_REJECTED"
    assert any("\u4e00" <= char <= "\u9fff" for char in result.error)
    assert logs and all(
        any("\u4e00" <= char <= "\u9fff" for char in log) for log in logs
    )


def test_all_display_errors_are_chinese(adapter):
    assert all(
        any("\u4e00" <= char <= "\u9fff" for char in message)
        for message in adapter._MESSAGES.values()
    )


@pytest.mark.parametrize(
    "model",
    [
        "doubao-seedance-2-0-260128",
        "doubao-seedance-2-0-fast-260128",
        "doubao-seedance-2-0-mini-260615",
    ],
)
async def test_reference_only_model_cannot_silently_replace_edit_mode(
    adapter, tmp_path, model
):
    result = await generate(
        generator(
            adapter, tmp_path, lambda _: httpx.Response(400, json={}), model=model
        ),
        tmp_path,
        gen_mode="video_edit",
        aspect_ratio="auto",
    )
    assert result.error_code == "ARK_EDIT_MODEL_REQUIRED"
    assert result.acceptance == "not_accepted"


async def test_download_pins_checked_ip_and_preserves_tls_hostname(
    adapter,
    tmp_path,
    mp4_bytes,
    monkeypatch,
):
    dns = []
    requests = []

    def rebinding_dns(*args, **kwargs):
        dns.append(args[0])
        address = "8.8.8.8" if len(dns) == 1 else "127.0.0.1"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))]

    def handler(request):
        requests.append(request)
        assert request.url.host == "8.8.8.8"
        assert request.headers["host"] == "media.example.test"
        assert request.extensions["sni_hostname"] == "media.example.test"
        assert request.url.query == b"signature=private-test"
        assert "authorization" not in request.headers
        return httpx.Response(200, content=mp4_bytes)

    monkeypatch.setattr(socket, "getaddrinfo", rebinding_dns)
    await generator(adapter, tmp_path, handler)._download(
        RESULT_URL,
        str(tmp_path / "pinned.mp4"),
    )
    assert dns == ["media.example.test"]
    assert len(requests) == 1


async def test_redirect_revalidates_host_and_drops_cookies(
    adapter, tmp_path, mp4_bytes
):
    requests = []

    def handler(request):
        requests.append(request)
        assert request.url.host == "8.8.8.8"
        assert "authorization" not in request.headers
        assert "cookie" not in request.headers
        hostname = "media.example.test" if len(requests) == 1 else "cdn.example.test"
        assert request.headers["host"] == request.extensions["sni_hostname"] == hostname
        if len(requests) == 1:
            return httpx.Response(
                302,
                headers={
                    "location": "https://cdn.example.test/video.mp4",
                    "set-cookie": "private=do-not-forward; Domain=example.test",
                },
            )
        return httpx.Response(200, content=mp4_bytes)

    await generator(adapter, tmp_path, handler)._download(
        RESULT_URL,
        str(tmp_path / "redirect.mp4"),
    )
    assert len(requests) == 2


@pytest.mark.parametrize(
    "kind, source",
    [
        ("organization", "local"),
        ("platform", "local"),
        ("local", "platform"),
    ],
)
async def test_direct_adapter_rejects_nonlocal_principal_or_credential(
    adapter,
    tmp_path,
    kind,
    source,
):
    from novelvideo.egress_context import TrustedEgressContext
    from novelvideo.ports.authz import BillingPrincipal
    from novelvideo.ports.model_credentials import CredentialReference

    context = TrustedEgressContext(
        envelope_id="env",
        project_id="project",
        task_type="single_video",
        requester_user_id="user",
        root_task_id="run",
        admission_id="admission",
        admitted_at="2026-11-15",
        membership_id=None,
        authz_version=1,
        billing_principal=BillingPrincipal(kind=kind, id="local"),
        credential=CredentialReference(
            source=source, credential_id="local", key_version=1
        ),
    )
    result = await generate(
        generator(adapter, tmp_path, lambda _: pytest.fail("must not submit")),
        tmp_path,
        egress_context=context,
    )
    assert result.error_code == "ARK_ORGANIZATION_DENIED"
    assert result.acceptance == "not_accepted"


@pytest.mark.parametrize(
    "mutation",
    [
        {"phase": "succeeded", "acceptance": "outcome_unknown"},
        {"phase": "accepted", "acceptance": "accepted", "task_id": None},
        {"phase": "not_accepted", "acceptance": "accepted"},
        {"token": "bad-token"},
        {"digest": "bad-digest"},
        {"provider_error_code": KEY},
        {"status_code": "untrusted"},
    ],
)
async def test_structurally_corrupt_receipt_blocks_new_run(
    adapter,
    tmp_path,
    mutation,
):
    posts = []

    def handler(request):
        posts.append(request)
        return httpx.Response(500, json={})

    await generate(generator(adapter, tmp_path, handler), tmp_path)
    binding = next((tmp_path / "state").glob("*.json"))
    record = json.loads(binding.read_text())
    record.update(mutation)
    binding.write_text(json.dumps(record))
    result = await generate(
        generator(adapter, tmp_path, handler), tmp_path, run_id="new"
    )
    assert result.error_code == "ARK_STATE_UNAVAILABLE"
    assert result.acceptance == "outcome_unknown"
    assert len(posts) == 1
    assert KEY not in repr(result)


async def test_post_receipt_write_failure_surfaces_id_and_never_resubmits(
    adapter,
    tmp_path,
    monkeypatch,
):
    posts = []

    def handler(request):
        posts.append(request)
        return httpx.Response(200, json={"id": TASK})

    g = generator(adapter, tmp_path, handler)

    def failed_save(*args):
        raise OSError("private " + KEY)

    monkeypatch.setattr(g, "_save", failed_save)
    result = await generate(g, tmp_path)
    assert result.error_code == "ARK_STATE_UNAVAILABLE"
    assert result.task_id == TASK
    assert result.acceptance == "accepted"
    repeated = await generate(
        generator(adapter, tmp_path, handler), tmp_path, run_id="new"
    )
    assert repeated.acceptance == "outcome_unknown"
    assert len(posts) == 1


async def test_rotated_key_cannot_rebind_unsettled_receipt(adapter, tmp_path):
    posts = []

    def handler(request):
        posts.append(request)
        return httpx.Response(500, json={})

    await generate(generator(adapter, tmp_path, handler), tmp_path)
    rotated = adapter.ArkDualReferenceVideoGenerator(
        api_key="rotated-offline-credential",
        state_dir=tmp_path / "state",
        transport=httpx.MockTransport(handler),
    )
    result = await generate(rotated, tmp_path, run_id="new")
    assert result.error_code == "ARK_INPUT_CONFLICT"
    assert result.acceptance == "outcome_unknown"
    assert len(posts) == 1


async def test_error_code_cannot_echo_even_a_whitelisted_credential(adapter, tmp_path):
    g = adapter.ArkDualReferenceVideoGenerator(
        api_key="InvalidParameter",
        state_dir=tmp_path / "state",
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                400,
                json={"error": {"code": "InvalidParameter"}},
            )
        ),
    )
    result = await generate(g, tmp_path)
    assert result.provider_error_code == ""
    assert "InvalidParameter" not in repr(result)
