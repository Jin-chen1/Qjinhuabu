"""Adapt native Doubao jobs to the existing beat video-generation pipeline."""

import asyncio
import base64
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path
from uuid import uuid4

from novelvideo.generators.video_generator import (
    VideoGeneratorBase,
    VideoGenResult,
    VideoGenStatus,
)
from novelvideo.shared import runtime_env
from novelvideo.utils.state_index_files import index_file_lock

from .jobs import JobStore, private_write
from .service import store as default_store


def local_channel_available() -> bool:
    return (
        sys.platform == "darwin"
        and runtime_env.is_ce_effective()
        and os.environ.get("SUPERTALE_ENV", "").lower() != "production"
    )


class DoubaoVideoGenerator(VideoGeneratorBase):
    def __init__(self, store: JobStore | None = None):
        if not local_channel_available():
            raise ValueError("豆包工作仅支持本机 macOS 社区版")
        self.store = store or default_store()

    def _bind(self, output: Path, run_id: str, prompt: str, refs: list) -> tuple[dict, bool]:
        # A new UI retry resumes any unsettled native request for this beat.
        # Only a completed/known-failed request may be replaced by an explicit new run.
        binding_key = hashlib.sha256(str(output.resolve()).encode()).hexdigest()
        binding_path = self.store.state / f"video-binding-{binding_key}"
        with index_file_lock(binding_path):
            if binding_path.exists():
                binding = json.loads(binding_path.read_text())
                previous = self.store.read(binding["job_id"])
                if (binding["run_id"] == run_id
                        or previous["status"] not in {"completed", "failed"}):
                    # Validate that a changed prompt/reference cannot silently become a retry.
                    self.store.create(previous["id"], "video", prompt, refs)
                    return self.store.public(previous), False
            job, fresh = self.store.create(str(uuid4()), "video", prompt, refs)
            private_write(binding_path, json.dumps({
                "job_id": job["id"], "run_id": run_id,
            }).encode())
            return job, fresh

    async def generate(
        self,
        image_path: str | None,
        prompt: str,
        output_path: str,
        aspect_ratio: str = "adaptive",
        duration: float = 5.0,
        poll_interval: float = 5.0,
        max_polls: int = 240,
        run_id: str | None = None,
        last_frame_path: str | None = None,
        on_log=None,
        on_progress=None,
        references=None,
        **kwargs,
    ) -> VideoGenResult:
        job_id = None
        output = Path(output_path)
        log = on_log or (lambda message: None)
        try:
            if not local_channel_available():
                raise ValueError("豆包工作仅支持本机 macOS 社区版")
            if references:
                raise ValueError("豆包工作主线目前仅支持首帧和可选尾帧图片")
            refs = []
            for name, path in (("first_frame", image_path), ("last_frame", last_frame_path)):
                if path:
                    source = Path(path)
                    if source.stat().st_size > 20 * 1024 * 1024:
                        raise ValueError("参考图片超过20 MiB")
                    refs.append({
                        "name": name + source.suffix,
                        "contentBase64": base64.b64encode(source.read_bytes()).decode(),
                    })
            generation_prompt = (
                f"{prompt.strip()}\n目标时长约{float(duration):g}秒。"
                + (f"目标画幅{aspect_ratio}。" if aspect_ratio not in {"adaptive", "auto"} else
                   "画幅跟随首帧。")
                + ("附件first_frame为首帧，last_frame为期望尾帧，请尽量保持画面连续。"
                   if last_frame_path else "")
            )
            # Missing run identity means a conservative repeat, never an implicit fresh generation.
            identity = str(run_id or "direct")
            job, fresh = await asyncio.to_thread(
                self._bind, output, identity, generation_prompt, refs,
            )
            job_id = job["id"]
            log(f"豆包工作任务 {job_id}：{'提交生成' if fresh else '查询原任务'}")
            if fresh:
                await asyncio.to_thread(self.store.submit, job_id)
            for index in range(max_polls):
                job = await asyncio.to_thread(self.store.refresh, job_id)
                if job["status"] == "completed":
                    media = next((item for item in job["files"] if item["kind"] == "video"), None)
                    if media is None:
                        raise ValueError("豆包工作未返回真实视频")
                    source = self.store.file(job_id, media["name"])
                    output.parent.mkdir(parents=True, exist_ok=True)
                    temporary = output.with_name(f".{output.name}.{job_id}.part")
                    try:
                        await asyncio.to_thread(shutil.copyfile, source, temporary)
                        temporary.chmod(0o600)
                        temporary.replace(output)
                    finally:
                        temporary.unlink(missing_ok=True)
                    log(f"视频已进入分镜：{media['width']}×{media['height']}，{media['duration']}秒")
                    if on_progress:
                        on_progress(1.0)
                    return VideoGenResult(
                        status=VideoGenStatus.DONE, video_path=str(output),
                        duration_seconds=float(media["duration"]), provider_task_id=job_id,
                    )
                if job["status"] in {"failed", "outcome_unknown"}:
                    return VideoGenResult(
                        status=VideoGenStatus.FAILED, provider_task_id=job_id,
                        error=f"豆包工作 {job['status']}：{job.get('error')}；原任务 {job_id}",
                    )
                if on_progress:
                    on_progress(min(0.9, 0.05 + index / max(1, max_polls)))
                await asyncio.sleep(poll_interval)
            return VideoGenResult(
                status=VideoGenStatus.FAILED, provider_task_id=job_id,
                error=f"等待超时，可再次点击查询原任务 {job_id}，不会重复生成",
            )
        except (OSError, ValueError, KeyError, TypeError):
            return VideoGenResult(
                status=VideoGenStatus.FAILED, provider_task_id=job_id,
                error="豆包工作输入无效或与原任务冲突；请先核验豆包工作任务再更改输入",
            )
