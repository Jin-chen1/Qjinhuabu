import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from novelvideo.freezone import workflow_runs as runs


CANVAS = "canvas_new_product_fullrun_20261115"


@pytest.fixture
def create_route(tmp_path, monkeypatch):
    from novelvideo.api.routes import freezone

    ctx = SimpleNamespace(project_id="01M7F56ZS6C9NCC6YV9ZM3Q4VW")
    monkeypatch.setattr(
        freezone, "_resolve_freezone_project",
        AsyncMock(return_value=(ctx, "local", "project", tmp_path, tmp_path)),
    )
    monkeypatch.setattr(freezone, "_canvas_state_project_dir", lambda *_: tmp_path)
    monkeypatch.setattr(freezone, "get_usage_meter", freezone.NoOpUsageMeter)
    monkeypatch.setattr(freezone, "_canvas_actor_id", lambda _: "local")
    nodes = []

    async def maintain(**kwargs):
        assert kwargs["canvas_id"] == CANVAS
        assert kwargs["canvas_project_dir"] == tmp_path
        runs.interrupt_stale_workflow_runs(
            project_dir=tmp_path, canvas_id=CANVAS, stale_after_seconds=300,
        )
        runs.reconcile_workflow_runs_with_canvas_results(
            project_dir=tmp_path, canvas_id=CANVAS, canvas_nodes=nodes,
        )

    maintenance = AsyncMock(side_effect=maintain)
    monkeypatch.setattr(freezone, "_maintain_canvas_workflow_runs", maintenance)

    async def create(key="next"):
        return await freezone.create_canvas_workflow_run(
            ctx.project_id, CANVAS,
            {"actions": [{"node_id": "next", "action": "prepare_frames"}],
             "runner_id": f"runner:{key}", "idempotency_key": key},
            {"id": "local"},
        )

    return tmp_path, nodes, maintenance, create


def old_run(path, *, partial=False):
    actions = [{"node_id": "script", "action": "generate_story_script"}]
    if partial:
        actions.append({"node_id": "video", "action": "generate_video"})
    return runs.create_workflow_run(
        project_dir=path, project_id="01M7F56ZS6C9NCC6YV9ZM3Q4VW",
        canvas_id=CANVAS, actions=actions, runner_id="runner:old",
    )


@pytest.mark.asyncio
async def test_create_reconciles_durable_completion_before_lease_check(create_route):
    path, nodes, maintenance, create = create_route
    old = old_run(path)
    nodes.append({"id": "script", "data": {"scriptResult": {"rows": [{"text": "done"}]}}})
    result = await create()
    maintenance.assert_awaited_once()
    assert result["data"]["run_id"] != old["run_id"]
    assert runs.read_workflow_run(
        project_dir=path, canvas_id=CANVAS, run_id=old["run_id"],
    )["status"] == "completed"


@pytest.mark.asyncio
async def test_create_preserves_active_partial_runner(create_route):
    path, nodes, maintenance, create = create_route
    old = old_run(path, partial=True)
    nodes.append({"id": "script", "data": {"scriptResult": {"rows": [{}]}}})
    with pytest.raises(HTTPException) as exc:
        await create()
    assert exc.value.status_code == 409
    maintenance.assert_awaited_once()
    stored = runs.read_workflow_run(project_dir=path, canvas_id=CANVAS, run_id=old["run_id"])
    assert stored["status"] == "running"
    assert stored["actions"][0]["status"] == "completed"
    assert stored["actions"][1]["status"] == "pending"
    assert stored["lease_expires_at"] == old["lease_expires_at"]


@pytest.mark.asyncio
async def test_create_reconciles_expired_partial_runner_without_get(create_route):
    path, nodes, maintenance, create = create_route
    old = old_run(path, partial=True)
    with runs._connect(path) as conn:
        conn.execute(
            "UPDATE workflow_runs SET lease_expires_at = ? WHERE run_id = ?",
            ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(), old["run_id"]),
        )
    nodes.append({"id": "script", "data": {"scriptResult": {"rows": [{}]}}})
    await create()
    maintenance.assert_awaited_once()
    stored = runs.read_workflow_run(project_dir=path, canvas_id=CANVAS, run_id=old["run_id"])
    assert stored["status"] == "cancelled"
    assert stored["actions"][0]["status"] == "completed"
    assert stored["actions"][1]["status"] == "pending"
    assert stored["metadata"]["interrupt_reason"] == "runner_heartbeat_expired"
    assert stored["metadata"]["superseded_by_run_id"]
    # Delayed heartbeats after recovery cannot reopen the old run.
    assert runs.update_workflow_run(
        project_dir=path, canvas_id=CANVAS, run_id=old["run_id"],
        status="running", runner_id="runner:old",
    )["status"] == "cancelled"


@pytest.mark.asyncio
async def test_concurrent_creates_keep_single_runner(create_route):
    path, _, _, create = create_route
    results = await asyncio.gather(create("one"), create("two"), return_exceptions=True)
    assert sum(isinstance(result, dict) for result in results) == 1
    errors = [result for result in results if isinstance(result, HTTPException)]
    assert len(errors) == 1 and errors[0].status_code == 409
    assert len(runs.list_workflow_runs(project_dir=path, canvas_id=CANVAS)) == 1


@pytest.mark.asyncio
async def test_create_does_not_treat_canvas_media_url_as_completion(create_route):
    path, nodes, _, create = create_route
    old = old_run(path, partial=True)
    nodes.extend([
        {"id": "script", "data": {"scriptResult": {"rows": [{}]}}},
        {"id": "video", "data": {"videoUrl": "/unverified.mp4"}},
    ])
    with pytest.raises(HTTPException) as exc:
        await create()
    assert exc.value.status_code == 409
    stored = runs.read_workflow_run(project_dir=path, canvas_id=CANVAS, run_id=old["run_id"])
    assert stored["actions"][1]["status"] == "pending"


def test_recent_heartbeat_prevents_stale_recovery(tmp_path):
    old = old_run(tmp_path, partial=True)
    with runs._connect(tmp_path) as conn:
        conn.execute(
            "UPDATE workflow_runs SET lease_expires_at = ? WHERE run_id = ?",
            ((datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat(), old["run_id"]),
        )
    refreshed = runs.update_workflow_run(
        project_dir=tmp_path, canvas_id=CANVAS, run_id=old["run_id"],
        status="running", runner_id="runner:old",
    )
    assert runs.interrupt_stale_workflow_runs(
        project_dir=tmp_path, canvas_id=CANVAS, stale_after_seconds=60,
        now=datetime.now(timezone.utc) + timedelta(seconds=2),
    ) == []
    assert refreshed["runner_id"] == "runner:old"


@pytest.mark.asyncio
async def test_create_idempotent_replay_keeps_same_runner(create_route):
    path, _, _, create = create_route
    first = await create("same")
    second = await create("same")
    assert first["data"]["run_id"] == second["data"]["run_id"]
    assert len(runs.list_workflow_runs(project_dir=path, canvas_id=CANVAS)) == 1
