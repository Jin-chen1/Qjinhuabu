import json
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI


NAME = "viral_replica_keyframes"
DISPLAY_NAME = "爆款复刻·关键帧版"
USER = {"username": "local", "user_id": "local"}


@pytest.fixture
def project_api(tmp_path, monkeypatch):
    from novelvideo import config, project_context
    from novelvideo.api.routes import projects
    from novelvideo.ports.local.project import AllowAllProjectAccess, SQLiteProjectRegistry

    for key, folder in (
        ("OUTPUT_DIR", "output"), ("STATE_DIR", "state"), ("RUNTIME_DIR", "runtime")
    ):
        monkeypatch.setattr(config, key, str(tmp_path / folder))

    def reopen_registry():
        registry = SQLiteProjectRegistry()
        access = AllowAllProjectAccess()
        for module in (projects, project_context):
            monkeypatch.setattr(module, "get_project_registry", lambda: registry)
            monkeypatch.setattr(module, "get_project_access", lambda: access)
        return registry

    reopen_registry()
    app = FastAPI()
    app.include_router(projects.router)
    # Override session auth, not project authorization or filesystem operations.
    app.dependency_overrides[projects.get_api_user] = lambda: USER
    return app, reopen_registry


def client_for(app):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


async def create(client, **fields):
    response = await client.post("/projects", json={"name": NAME, **fields})
    assert response.status_code == 200, response.text
    return response.json()["data"]


@pytest.mark.asyncio
async def test_create_persists_chinese_display_name_without_changing_storage(project_api):
    app, reopen = project_api
    async with client_for(app) as client:
        created = await create(client, display_name=DISPLAY_NAME)
    record = await reopen().get_project(created["id"])
    saved = json.loads((Path(record.state_dir) / "project_config.json").read_text())
    assert saved["display_name"] == DISPLAY_NAME
    assert saved["user"] == "local"
    assert saved["knowledge_pipeline"] == "structured_v1"
    assert record.name == created["name"] == NAME
    for directory in (record.output_dir, record.state_dir, record.runtime_dir):
        assert Path(directory).name == NAME
        assert Path(directory).is_dir()


@pytest.mark.asyncio
async def test_reopened_list_summary_and_detail_return_display_name(project_api):
    app, reopen = project_api
    async with client_for(app) as client:
        created = await create(client, display_name=DISPLAY_NAME)
        reopen()
        for path in ("/projects", "/projects/summaries?status=all"):
            response = await client.get(path)
            assert response.status_code == 200
            row = response.json()["data"][0]
            assert row["display_name"] == DISPLAY_NAME
            assert row["id"] == created["id"]
            assert row["name"] == NAME
        detail = (await client.get(f"/projects/{created['id']}")).json()["data"]
        assert detail["display_name"] == DISPLAY_NAME
        assert detail["name"] == NAME


@pytest.mark.asyncio
@pytest.mark.parametrize("fields", [{}, {"display_name": None}, {"display_name": ""}])
async def test_missing_display_name_preserves_old_project(project_api, fields):
    app, reopen = project_api
    async with client_for(app) as client:
        created = await create(client, **fields)
        record = await reopen().get_project(created["id"])
        config_path = Path(record.state_dir) / "project_config.json"
        before = config_path.read_bytes()
        for path in ("/projects", "/projects/summaries?status=all"):
            row = (await client.get(path)).json()["data"][0]
            assert row["name"] == NAME
            assert not row.get("display_name")
        assert config_path.read_bytes() == before
        detail = (await client.get(f"/projects/{created['id']}")).json()["data"]
        assert detail["name"] == NAME
        assert not detail.get("display_name")


@pytest.mark.asyncio
async def test_existing_config_updates_preserve_display_name(project_api):
    from novelvideo.project_config import save_project_config_in_state_dir

    app, reopen = project_api
    async with client_for(app) as client:
        created = await create(client, display_name=DISPLAY_NAME)
        record = await reopen().get_project(created["id"])
        save_project_config_in_state_dir(record.state_dir, custom_styles={"custom": {"label": "测试"}})
        response = await client.patch(
            f"/projects/{created['id']}", json={"aspect_ratio": "16:9"}
        )
        assert response.status_code == 200, response.text
        saved = json.loads((Path(record.state_dir) / "project_config.json").read_text())
        assert saved["display_name"] == DISPLAY_NAME
        assert saved["aspect_ratio"] == "16:9"
        assert saved["custom_styles"] == {"custom": {"label": "测试"}}


@pytest.mark.asyncio
@pytest.mark.parametrize("name", [
    DISPLAY_NAME, "../outside", "folder/name", r"folder\name", "_reserved", "", "a" * 65,
])
async def test_display_name_does_not_relax_project_path_validation(project_api, name):
    app, reopen = project_api
    async with client_for(app) as client:
        response = await client.post(
            "/projects", json={"name": name, "display_name": DISPLAY_NAME}
        )
    assert response.status_code in {400, 422}
    assert await reopen().list_accessible_projects([("user", "local")]) == []


@pytest.mark.asyncio
async def test_display_name_does_not_bypass_storage_ownership(project_api, tmp_path, monkeypatch):
    from novelvideo.api.routes import projects

    app, reopen = project_api
    registry = reopen()
    original_create = registry.create_project
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel.txt"
    sentinel.write_text("keep")

    async def forged_record(**kwargs):
        return replace(await original_create(**kwargs), state_dir=str(outside))

    monkeypatch.setattr(registry, "create_project", forged_record)
    async with client_for(app) as client:
        response = await client.post(
            "/projects", json={"name": NAME, "display_name": DISPLAY_NAME}
        )
    assert response.status_code == 500
    assert sentinel.read_text() == "keep"
    assert not (outside / "project_config.json").exists()
    assert await projects.get_project_registry().list_accessible_projects([("user", "local")]) == []


@pytest.mark.asyncio
async def test_non_home_project_lists_do_not_read_local_config(project_api, monkeypatch):
    from novelvideo.api.routes import projects
    from novelvideo.ports.project import ProjectRecord

    app, _ = project_api
    record = ProjectRecord(
        id="remote-id", name=NAME, owner_type="user", owner_id="local",
        owner_username="local", home_node_id="other-node", status="active",
        output_dir="/unavailable/output", state_dir="/unavailable/state",
        runtime_dir="/unavailable/runtime",
    )

    async def records(_principals):
        return [record]

    def forbid_read(_state_dir):
        raise AssertionError("non-home config must not be read")

    monkeypatch.setattr(projects.get_project_registry(), "list_accessible_projects", records)
    monkeypatch.setattr(projects, "is_record_home_node", lambda _record: False)
    monkeypatch.setattr(projects, "load_project_config_file_from_state_dir", forbid_read)
    async with client_for(app) as client:
        for path in ("/projects", "/projects/summaries?status=all"):
            row = (await client.get(path)).json()["data"][0]
            assert row["name"] == NAME
            assert not row.get("display_name")
