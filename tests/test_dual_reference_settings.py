import json

import httpx
import pytest


@pytest.fixture
def settings_path(tmp_path, monkeypatch):
    from novelvideo import config

    monkeypatch.setattr(config, "STATE_DIR", str(tmp_path))
    return tmp_path


def test_saved_key_is_private_and_never_returned(settings_path):
    from novelvideo.dual_reference_settings import read_config, save_config, public_status

    save_config(api_key="private-test-secret", model="doubao-seedance-2-0-260128")
    status = public_status()
    assert status["configured"] is True
    assert status["connected"] is False
    assert status["supportsReferenceVideo"] is False
    assert "private-test-secret" not in json.dumps(status)
    assert read_config()["apiKey"] == "private-test-secret"
    path = settings_path / "local" / "dual-reference" / "config.json"
    assert path.stat().st_mode & 0o777 == 0o600
    save_config(api_key=None, model="doubao-seedance-2-0-fast-260128")
    assert read_config()["apiKey"] == "private-test-secret"


def test_endpoint_cannot_redirect_secret_to_another_host(settings_path):
    from novelvideo.dual_reference_settings import save_config

    for url in ("http://ark.cn-beijing.volces.com/api/v3",
                "https://example.com/api/v3",
                "https://ark.cn-beijing.volces.com/api/v3?token=x"):
        with pytest.raises(ValueError):
            save_config(base_url=url)


@pytest.mark.asyncio
async def test_no_key_no_remote_request(settings_path):
    from novelvideo.dual_reference_settings import verify_config

    def fail(request):
        raise AssertionError("unauthorized configuration must not call cloud")

    async with httpx.AsyncClient(transport=httpx.MockTransport(fail)) as client:
        result = await verify_config(client=client)
    assert not result["connected"]
    assert result["verificationLevel"] == "none"


@pytest.mark.asyncio
async def test_model_list_is_credentials_evidence_not_generation_proof(settings_path):
    from novelvideo.dual_reference_settings import save_config, verify_config, public_status

    model = "doubao-seedance-2-0-260128"
    save_config(api_key="private-test-secret", model=model)

    def reply(request):
        assert request.method == "GET"
        assert request.url.path == "/api/v3/models"
        return httpx.Response(200, json={"data": [{"id": model}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
        result = await verify_config(client=client)
    assert result["connected"] is True
    assert result["supportsReferenceVideo"] is True
    assert result["verificationLevel"] == "credentials_and_model_catalog"
    assert result["generationVerified"] is False
    assert "private-test-secret" not in json.dumps(result)
    save_config(model=model)
    assert public_status()["connected"] is False


@pytest.mark.asyncio
async def test_key_without_requested_model_does_not_enable_channel(settings_path):
    from novelvideo.dual_reference_settings import save_config, verify_config

    save_config(api_key="test-key", model="doubao-seedance-2-0-260128")
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"data": [{"id": "unrelated-model"}]})
    )) as client:
        result = await verify_config(client=client)
    assert not result["connected"]
    assert not result["supportsReferenceVideo"]


@pytest.mark.asyncio
async def test_unauthorized_response_does_not_echo_upstream_or_secret(settings_path):
    from novelvideo.dual_reference_settings import save_config, verify_config

    save_config(api_key="private-test-secret", model="doubao-seedance-2-0-260128")
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(401, text="private-test-secret")
    )) as client:
        result = await verify_config(client=client)
    assert not result["connected"]
    assert "private-test-secret" not in json.dumps(result)


@pytest.mark.asyncio
async def test_verified_channel_is_added_without_changing_native_capability(settings_path, monkeypatch):
    from types import SimpleNamespace
    from novelvideo.api.routes import freezone
    from novelvideo.doubao_work import video
    from novelvideo import dual_reference_settings as settings

    async def project(*args, **kwargs):
        return SimpleNamespace(requester_user_id="local"), None, None, None, None

    monkeypatch.setattr(freezone, "_resolve_freezone_project", project)
    monkeypatch.setattr(video, "local_channel_available", lambda: True)
    monkeypatch.setattr(settings, "catalog_entry", lambda: {
        "id": settings.CHANNEL_ID, "referenceVideoMax": 10,
        "supportedModes": ["all_reference", "video_edit"],
    })
    result = await freezone.freezone_video_models("demo", {})
    assert [item["id"] for item in result["data"]] == ["doubao_work", settings.CHANNEL_ID]
    assert result["data"][0]["referenceVideoMax"] == 0


@pytest.mark.asyncio
async def test_dual_channel_cannot_be_selected_before_authorization(settings_path, monkeypatch):
    from fastapi import HTTPException
    from novelvideo.api.routes import freezone
    from novelvideo.doubao_work import video

    monkeypatch.setattr(video, "local_channel_available", lambda: True)
    with pytest.raises(HTTPException) as failure:
        await freezone._resolve_catalog_video_backend("ark_dual_reference")
    assert failure.value.status_code == 409


@pytest.mark.asyncio
async def test_route_validation_does_not_echo_submitted_secret(settings_path, monkeypatch):
    from fastapi import FastAPI
    from novelvideo.api.routes import dual_reference
    from novelvideo.shared import runtime_env

    monkeypatch.setattr(runtime_env, "is_ce_effective", lambda: True)
    app = FastAPI()
    app.include_router(dual_reference.router)
    secret = "private-test-secret-" * 300
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 50000)),
        base_url="http://127.0.0.1:8780",
    ) as client:
        response = await client.put(
            "/freezone/video/dual-reference/config", json={"apiKey": secret},
        )
    assert response.status_code in {400, 422}
    assert secret not in response.text


def test_dual_reference_factory_uses_independent_authorized_key(settings_path, monkeypatch):
    from novelvideo import dual_reference_settings as settings
    from novelvideo.generators.video_generator import create_video_generator
    from novelvideo.doubao_work import video

    monkeypatch.setattr(video, "local_channel_available", lambda: True)
    monkeypatch.setattr(settings, "require_authorized_config", lambda: {
        "apiKey": "test-independent-key", "baseUrl": settings.DEFAULT_BASE_URL,
        "model": settings.DEFAULT_MODEL,
    })
    generator = create_video_generator(backend="ark_dual_reference")
    assert type(generator).__name__ == "ArkDualReferenceVideoGenerator"
