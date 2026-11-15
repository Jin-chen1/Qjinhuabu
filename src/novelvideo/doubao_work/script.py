"""Structured script generation through native jobs with durable node receipts."""

import asyncio
import base64
import hashlib
import json
from uuid import NAMESPACE_URL, uuid5

from novelvideo.api.schemas import FreezoneStoryScriptGenerateData
from novelvideo.freezone.jobs import loads_model_json
from novelvideo.utils.state_index_files import index_file_lock

from . import service
from .jobs import JobStore, private_write


def _bind(store: JobStore, scope: str, run_id: str, prompt: str, refs: list) -> tuple[dict, bool]:
    key = hashlib.sha256(scope.encode()).hexdigest()
    binding_path = store.state / f"script-binding-{key}"
    with index_file_lock(binding_path):
        if binding_path.exists():
            binding = json.loads(binding_path.read_text())
            previous = store.read(binding["job_id"])
            if binding["run_id"] == run_id or previous["status"] not in {"completed", "failed"}:
                store.create(previous["id"], "text", prompt, refs)
                return store.public(previous), False
        job, fresh = store.create(
            str(uuid5(NAMESPACE_URL, f"doubao-script:{scope}:{run_id}")),
            "text", prompt, refs,
        )
        private_write(binding_path, json.dumps({"job_id": job["id"], "run_id": run_id}).encode())
        return job, fresh


async def generate_script(
    *, scope: str, run_id: str, prompt: str, images: list,
    poll_interval: float = 3.0, max_polls: int = 200,
) -> FreezoneStoryScriptGenerateData:
    store = service.store()
    refs = [{
        "name": f"reference_{index + 1:02d}.jpg",
        "contentBase64": base64.b64encode(image.data).decode(),
    } for index, image in enumerate(images)]
    try:
        job, fresh = await asyncio.to_thread(_bind, store, scope, run_id, prompt, refs)
    except ValueError as exc:
        raise RuntimeError("豆包工作输入无效或与原任务冲突，请先核验原任务") from exc
    job_id = job["id"]
    if fresh:
        await asyncio.to_thread(store.submit, job_id)
    for _ in range(max_polls):
        job = await asyncio.to_thread(store.refresh, job_id)
        if job["status"] == "completed":
            try:
                result = FreezoneStoryScriptGenerateData.model_validate(loads_model_json(job["text"]))
                if not result.rows or any(
                    not row.visual_description.strip()
                    or not row.shot_prompt.strip()
                    or not row.video_motion_prompt.strip()
                    or row.duration <= 0
                    or row.shot_no != index + 1
                    for index, row in enumerate(result.rows)
                ):
                    raise ValueError("invalid or empty script rows")
                return result
            except (ValueError, TypeError) as exc:
                raise RuntimeError(
                    f"豆包工作输出无法解析；原任务 {job_id}，未自动重复提交"
                ) from exc
        if job["status"] in {"failed", "outcome_unknown"}:
            raise RuntimeError(f"豆包工作 {job['status']}：{job.get('error')}；原任务 {job_id}")
        await asyncio.sleep(poll_interval)
    raise RuntimeError(f"豆包工作等待超时；原任务 {job_id}，再次点击会查询原任务")
