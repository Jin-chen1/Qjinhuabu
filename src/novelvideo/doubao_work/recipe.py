"""Execute installed text recipes with durable native task receipts."""

import asyncio
import hashlib
import json
from uuid import NAMESPACE_URL, uuid5

from novelvideo.utils.state_index_files import index_file_lock

from . import service
from .jobs import JobStore, private_write


def _bind(store: JobStore, scope: str, run_id: str, prompt: str) -> tuple[dict, bool]:
    path = store.state / f"recipe-binding-{hashlib.sha256(scope.encode()).hexdigest()}"
    with index_file_lock(path):
        if path.exists():
            binding = json.loads(path.read_text())
            previous = store.read(binding["job_id"])
            if binding["run_id"] == run_id or previous["status"] not in {"completed", "failed"}:
                store.create(previous["id"], "text", prompt, [])
                return store.public(previous), False
        job, fresh = store.create(
            str(uuid5(NAMESPACE_URL, f"doubao-recipe:{scope}:{run_id}")),
            "text", prompt, [],
        )
        private_write(path, json.dumps({"job_id": job["id"], "run_id": run_id}).encode())
        return job, fresh


async def generate_text(
    *, scope: str, run_id: str, prompt: str,
    poll_interval: float = 3.0, max_polls: int = 200,
) -> str:
    store = service.store()
    try:
        job, fresh = await asyncio.to_thread(_bind, store, scope, run_id, prompt)
    except ValueError as exc:
        raise RuntimeError("豆包工作输入无效或与原任务冲突，请先核验原任务") from exc
    job_id = job["id"]
    if fresh:
        await asyncio.to_thread(store.submit, job_id)
    for _ in range(max_polls):
        job = await asyncio.to_thread(store.refresh, job_id)
        if job["status"] == "completed":
            content = str(job.get("text") or "").strip()
            if not content:
                raise RuntimeError(f"豆包工作未返回文本；原任务 {job_id}，未自动重复提交")
            return content
        if job["status"] in {"failed", "outcome_unknown"}:
            raise RuntimeError(f"豆包工作 {job['status']}：{job.get('error')}；原任务 {job_id}")
        if poll_interval:
            await asyncio.sleep(poll_interval)
    raise RuntimeError(f"豆包工作等待超时；原任务 {job_id}，再次点击会查询原任务")
