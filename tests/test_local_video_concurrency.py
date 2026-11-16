"""Local video capacity policy without starting services or contacting providers."""

import json
import os
import shlex
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import pytest
from dotenv import dotenv_values

from novelvideo.doubao_work.jobs import JobStore
from novelvideo.ports.local.tasks import InlineTaskBackend
from novelvideo.task_backend.limits import (
    global_lane_concurrency,
    project_lane_active_limit,
    project_lane_effective_active_limit,
    project_user_lane_active_limit,
)

ROOT = Path(__file__).resolve().parents[1]
VIDEO_KEYS = (
    "ST_PROJECT_MAX_ACTIVE_VIDEO_TASKS",
    "ST_PROJECT_USER_MAX_ACTIVE_VIDEO_TASKS",
    "ST_CE_GLOBAL_MAX_ACTIVE_VIDEO_TASKS",
)


def assert_video_capacity():
    assert project_lane_active_limit("video") == 10
    assert project_user_lane_active_limit("video") == 10
    assert project_lane_effective_active_limit("video", eligible_user_count=1) == 10
    assert global_lane_concurrency("video") == 10
    backend = InlineTaskBackend()
    try:
        assert backend.lane_snapshot()["video"]["concurrency"] == 10
        assert backend._lanes["video"].executor._max_workers == 10
        assert backend.lane_snapshot()["default"]["concurrency"] == 8
        assert backend.lane_snapshot()["world"]["concurrency"] == 1
        assert backend.lane_snapshot()["ffmpeg"]["concurrency"] == 1
    finally:
        for lane in backend._lanes.values():
            lane.executor.shutdown()


def test_local_env_sets_project_personal_and_global_video_capacity(monkeypatch):
    if not (ROOT / ".env").is_file():
        pytest.skip("local .env is not included in public source")
    values = dotenv_values(ROOT / ".env")
    for key in VIDEO_KEYS:
        monkeypatch.setenv(key, values.get(key) or "1")
    assert_video_capacity()


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS local launcher policy")
@pytest.mark.parametrize("inherited", ["1", "2", ""])
def test_local_launcher_overrides_stale_video_env_after_dotenv(monkeypatch, inherited):
    script = (ROOT / "scripts/start-ce.sh").read_text()
    policy = script.split("# Force standalone CE mode.", 1)[1].split(
        "# This launcher provisions Codex only.", 1
    )[0]
    environment = dict(os.environ)
    environment.update({key: inherited for key in VIDEO_KEYS})
    # Execute the actual post-dotenv policy only, never the service launcher.
    result = subprocess.run(
        ["bash", "-c", "root_dir=$PWD\napi_host=127.0.0.1\napi_port=8780\n"
         + policy + "\nprintf '%s\\n' " + " ".join(f'"${{{key}:-}}"' for key in VIDEO_KEYS)],
        cwd=ROOT, env=environment, check=True, capture_output=True, text=True,
    )
    assert result.stdout.splitlines() == ["10", "10", "10"]
    for key, value in zip(VIDEO_KEYS, result.stdout.splitlines()):
        monkeypatch.setenv(key, value)
    assert_video_capacity()


def test_native_job_store_does_not_serialize_ten_video_submissions(tmp_path):
    barrier = Barrier(10, timeout=5)

    class Client:
        def call(self, method, path, payload=None):
            assert method == "POST"
            if path == "/v1/sessions":
                barrier.wait()
                return {"id": payload["uuid"]}
            return {"messages": [{"id": payload["uuid"]}]}

    store = JobStore(tmp_path / "state", tmp_path / "out", Client())
    jobs = [store.create(str(uuid4()), "video", "isolated test", [])[0] for _ in range(10)]
    with ThreadPoolExecutor(max_workers=10) as executor:
        list(executor.map(store.submit, [job["id"] for job in jobs]))
    assert [store.read(job["id"])["status"] for job in jobs] == ["submitted"] * 10


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS local launcher policy")
def test_backend_only_launcher_overrides_stale_dotenv():
    config = ROOT.parent / ".trae/environments/run.json"
    if not config.is_file() or not (ROOT / ".env").is_file():
        pytest.skip("local startup configuration is not included in public source")
    actions = json.loads(config.read_text())["actions"]
    command = next(action["command"] for action in actions if action["id"] == "dramaclaw-backend")
    policy = shlex.split(command)[2].split('backend_codex_bin=', 1)[0]
    policy = policy.replace("source .env", "\n".join(f"export {key}=1" for key in VIDEO_KEYS))
    result = subprocess.run(
        ["bash", "-c", policy + "\nprintf '%s\\n' "
         + " ".join(f'"${{{key}:-}}"' for key in VIDEO_KEYS)],
        cwd=ROOT, check=True, capture_output=True, text=True,
    )
    assert result.stdout.splitlines() == ["10", "10", "10"]
