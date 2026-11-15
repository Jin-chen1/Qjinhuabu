import io

import pytest
from PIL import Image

from novelvideo.doubao_work.jobs import JobStore
from test_doubao_video import VideoClient


class ImageClient(VideoClient):
    def call(self, method, path, payload=None):
        result = super().call(method, path, payload)
        if method == "GET":
            result["messages"][0]["items"] = [{
                "type": "artifact", "artifact_type": "image", "state": "active",
                "image": {"url": "https://example.com/image"},
            }]
        return result


def image_store(tmp_path, monkeypatch, unknown=False):
    from novelvideo.doubao_work import jobs
    buffer = io.BytesIO()
    Image.new("RGB", (20, 30), "red").save(buffer, format="JPEG")
    monkeypatch.setattr(jobs, "download", lambda url, path: path.write_bytes(buffer.getvalue()))
    return JobStore(tmp_path / "state", tmp_path / "out", ImageClient(unknown))


def test_local_image_selection_and_config(monkeypatch):
    from novelvideo import config
    from novelvideo.shared import runtime_env
    monkeypatch.setattr(runtime_env, "is_ce_effective", lambda: True)
    assert "doubao_work" in config.image_generation_selection_options()
    cfg = config.get_grid_generation_config(selection_override="doubao_work")
    assert cfg["provider"] == "doubao_work"
    assert cfg["api_key"] == ""
    monkeypatch.setattr(runtime_env, "is_ce_effective", lambda: False)
    assert "doubao_work" not in config.image_generation_selection_options()
    with pytest.raises(ValueError):
        config.get_grid_generation_config(provider_override="doubao_work")


@pytest.mark.asyncio
async def test_png_delivery_and_restart_retry(tmp_path, monkeypatch):
    from novelvideo.doubao_work.image import DoubaoImageGenerator
    store = image_store(tmp_path, monkeypatch)
    output = tmp_path / "images" / "result.png"
    opts = dict(prompt="眼镜产品照", output_path=str(output), run_id="one",
                max_polls=1, poll_interval=0)
    await DoubaoImageGenerator(store).generate(**opts)
    with Image.open(output) as image:
        assert image.format == "PNG"
        assert image.size == (20, 30)
    restored = JobStore(store.state, store.output, store.client)
    await DoubaoImageGenerator(restored).generate(**opts)
    assert len(store.client.posts) == 2
    await DoubaoImageGenerator(restored).generate(**dict(opts, run_id="two"))
    assert len(store.client.posts) == 4


@pytest.mark.asyncio
async def test_references_unknown_receipt_never_resubmits(tmp_path, monkeypatch):
    from novelvideo.doubao_work.image import DoubaoImageGenerator
    store = image_store(tmp_path, monkeypatch, unknown=True)
    ref = io.BytesIO()
    Image.new("RGB", (20, 30)).save(ref, format="PNG")
    generator = DoubaoImageGenerator(store)
    opts = dict(prompt="更换背景", reference_bytes=[ref.getvalue()],
                output_path=str(tmp_path / "result.png"), max_polls=1, poll_interval=0)
    with pytest.raises(ValueError, match="outcome_unknown"):
        await generator.generate(**opts, run_id="one")
    with pytest.raises(ValueError, match="outcome_unknown"):
        await generator.generate(**opts, run_id="two")
    assert len(store.client.posts) == 1
    with pytest.raises(ValueError):
        await generator.generate(**dict(opts, prompt="改变镜框"), run_id="three")
    assert len(store.client.posts) == 1


@pytest.mark.asyncio
async def test_shared_entry_routes_native(tmp_path, monkeypatch):
    from novelvideo.doubao_work import image
    from novelvideo.generators.nanobanana_grid import generate_text_to_image
    store = image_store(tmp_path, monkeypatch)
    monkeypatch.setattr(image, "default_store", lambda: store)
    output = tmp_path / "result.png"
    result = await generate_text_to_image(
        "生成眼镜", str(output), config={"provider": "doubao_work", "run_id": "one"},
    )
    assert result == output
    assert Image.open(result).format == "PNG"


@pytest.mark.asyncio
async def test_panel_reference_delivery(tmp_path, monkeypatch):
    from novelvideo import config
    from novelvideo.doubao_work import image
    from novelvideo.generators.nanobanana_grid import NanoBananaGridGenerator
    store = image_store(tmp_path, monkeypatch)
    monkeypatch.setattr(image, "default_store", lambda: store)
    frame = tmp_path / "ref.png"
    Image.new("RGB", (20, 30)).save(frame)
    output = tmp_path / "panel.png"
    generator = NanoBananaGridGenerator(config=config.get_grid_generation_config(
        selection_override="doubao_work",
    ))
    result = await generator._render_single_panel_gemini(
        str(frame), "更换背景", str(output),
    )
    assert result == str(output)
    assert len(store.client.posts) == 3
    assert Image.open(output).format == "PNG"


@pytest.mark.asyncio
async def test_download_pending_resumes_original_job(tmp_path, monkeypatch):
    from novelvideo.doubao_work import jobs
    from novelvideo.doubao_work.image import DoubaoImageGenerator
    store = image_store(tmp_path, monkeypatch)
    download = jobs.download
    def unavailable(url, path):
        raise RuntimeError("delivery unavailable")
    monkeypatch.setattr(jobs, "download", unavailable)
    opts = dict(prompt="生成眼镜", output_path=str(tmp_path / "result.png"),
                max_polls=1, poll_interval=0)
    with pytest.raises(ValueError, match="原任务"):
        await DoubaoImageGenerator(store).generate(**opts, run_id="one")
    monkeypatch.setattr(jobs, "download", download)
    restored = JobStore(store.state, store.output, store.client)
    await DoubaoImageGenerator(restored).generate(**opts, run_id="two")
    assert len(store.client.posts) == 2


@pytest.mark.asyncio
async def test_native_catalog_selection_and_provider_validation(monkeypatch):
    from fastapi import HTTPException
    from novelvideo.api.routes.freezone import (
        _resolve_catalog_request, _catalog_image_execution_selection,
    )
    from novelvideo.shared import runtime_env
    monkeypatch.setattr(runtime_env, "is_ce_effective", lambda: True)
    schema, params, entry = await _resolve_catalog_request("image", "doubao_work", None)
    assert not schema and not params
    assert entry["referenceImageMax"] == 9
    assert _catalog_image_execution_selection(
        entry, requested_provider="doubao_work", requested_model="doubao_work",
    ) == ("doubao_work", "doubao_work")
    with pytest.raises(HTTPException):
        _catalog_image_execution_selection(entry, requested_provider="newapi", requested_model=None)
    monkeypatch.setattr(runtime_env, "is_ce_effective", lambda: False)
    with pytest.raises(HTTPException):
        await _resolve_catalog_request("image", "doubao_work", None)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["organization", "platform"])
async def test_native_image_rejects_nonlocal_principal(tmp_path, kind):
    from novelvideo.egress_context import TrustedEgressContext
    from novelvideo.generators.nanobanana_grid import generate_text_to_image
    from novelvideo.ports.authz import BillingPrincipal
    from novelvideo.ports.egress import EgressError
    from novelvideo.ports.model_credentials import CredentialReference
    context = TrustedEgressContext(
        envelope_id="env", project_id="project", task_type="freezone_gen",
        requester_user_id="user", root_task_id="run", admission_id="admission",
        admitted_at="2026-11-05", membership_id=None, authz_version=1,
        billing_principal=BillingPrincipal(kind=kind, id="local"),
        credential=CredentialReference(source="local", credential_id="local", key_version=1),
    )
    with pytest.raises(EgressError):
        await generate_text_to_image(
            "眼镜", str(tmp_path / "output.png"),
            config={"provider": "doubao_work"}, egress_context=context,
        )
