"""Native keyframe analysis with durable receipts and no implicit resubmission."""

import asyncio
import base64
import hashlib
import json
import math
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from novelvideo.freezone.jobs import loads_model_json
from novelvideo.utils.state_index_files import index_file_lock

from . import service
from .jobs import JobStore, private_write


def _bind(
    store: JobStore, source: Path, project: Path, run_id: str, prompt: str, refs: list,
) -> tuple[dict, bool]:
    scope = f"{project.resolve()}\n{source.resolve()}"
    key = hashlib.sha256(scope.encode()).hexdigest()
    binding_path = store.state / f"breakdown-binding-{key}"
    with index_file_lock(binding_path):
        if binding_path.exists():
            binding = json.loads(binding_path.read_text())
            previous = store.read(binding["job_id"])
            if binding["run_id"] == run_id or previous["status"] not in {"completed", "failed"}:
                store.create(previous["id"], "text", prompt, refs)
                return store.public(previous), False
        job, fresh = store.create(
            str(uuid5(NAMESPACE_URL, f"doubao-breakdown:{scope}:{run_id}")),
            "text", prompt, refs,
        )
        private_write(binding_path, json.dumps({
            "job_id": job["id"], "run_id": run_id,
        }).encode())
        return job, fresh


async def analyze_breakdown(
    *,
    project: Path,
    source: Path,
    run_id: str,
    prompt: str,
    frames: list[Path],
    duration_sec: float,
    poll_interval: float = 3.0,
    max_polls: int = 200,
) -> tuple[str, str]:
    store = service.store()
    refs = [{
        "name": f"frame_{index + 1:02d}.jpg",
        "contentBase64": base64.b64encode(path.read_bytes()).decode(),
    } for index, path in enumerate(frames)]
    try:
        job, fresh = await asyncio.to_thread(
            _bind, store, source, project, run_id, prompt, refs,
        )
    except ValueError as exc:
        raise RuntimeError("豆包工作输入与原任务冲突，请先核验原任务") from exc
    job_id = job["id"]
    if fresh:
        await asyncio.to_thread(store.submit, job_id)
    for _ in range(max_polls):
        job = await asyncio.to_thread(store.refresh, job_id)
        if job["status"] == "completed":
            text = job["text"]
            try:
                raw = loads_model_json(text)
                shots = raw.get("shots") if isinstance(raw, dict) else None
                if not isinstance(shots, list) or not shots:
                    raise ValueError("no shots")
                for shot in shots:
                    if not isinstance(shot, dict) or not isinstance(shot.get("description"), str):
                        raise ValueError("invalid shot")
                    if not shot["description"].strip():
                        raise ValueError("empty shot")
                    start, end = shot.get("start_time"), shot.get("end_time")
                    if (isinstance(start, bool) or isinstance(end, bool)
                            or not isinstance(start, (float, int))
                            or not isinstance(end, (float, int))
                            or not math.isfinite(start) or not math.isfinite(end)
                            or not 0 <= start < end <= duration_sec + 0.1):
                        raise ValueError("invalid shot timing")
                # This request carries still images only; audio is extracted locally.
                raw["music"] = None
                return "doubao_work", json.dumps(raw, ensure_ascii=False)
            except (ValueError, TypeError) as exc:
                raise RuntimeError(
                    f"豆包工作输出无法解析；原任务 {job_id}，未自动重复提交"
                ) from exc
        if job["status"] in {"failed", "outcome_unknown"}:
            raise RuntimeError(
                f"豆包工作 {job['status']}：{job.get('error')}；原任务 {job_id}"
            )
        await asyncio.sleep(poll_interval)
    raise RuntimeError(f"豆包工作等待超时；原任务 {job_id}，再次点击会查询原任务")
