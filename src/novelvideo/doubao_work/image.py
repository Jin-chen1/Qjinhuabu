"""Native image delivery for canvas, grids and storyboard panels."""

import asyncio
import base64
import hashlib
import json
from pathlib import Path
from uuid import uuid4

from PIL import Image

from novelvideo.utils.state_index_files import index_file_lock

from .jobs import JobStore, private_write
from .service import store as default_store
from .video import local_channel_available


class DoubaoImageGenerator:
    def __init__(self, store: JobStore | None = None):
        if not local_channel_available():
            raise ValueError("豆包工作仅支持本机 macOS 社区版")
        self.store = store or default_store()

    def _bind(self, output: Path, run_id: str, prompt: str, refs: list) -> tuple[dict, bool]:
        key = hashlib.sha256(str(output.resolve()).encode()).hexdigest()
        binding_path = self.store.state / f"image-binding-{key}"
        with index_file_lock(binding_path):
            if binding_path.exists():
                binding = json.loads(binding_path.read_text())
                previous = self.store.read(binding["job_id"])
                if binding["run_id"] == run_id or previous["status"] not in {"completed", "failed"}:
                    self.store.create(previous["id"], "image", prompt, refs)
                    return self.store.public(previous), False
            job, fresh = self.store.create(str(uuid4()), "image", prompt, refs)
            private_write(binding_path, json.dumps({
                "job_id": job["id"], "run_id": run_id,
            }).encode())
            return job, fresh

    @staticmethod
    def _deliver(source: Path, output: Path, job_id: str) -> None:
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.with_name(f".{output.name}.{job_id}.part")
        try:
            with Image.open(source) as image:
                image.load()
                image.save(temporary, format="PNG")
            temporary.chmod(0o600)
            temporary.replace(output)
        finally:
            temporary.unlink(missing_ok=True)

    async def generate(
        self, prompt: str, output_path: str, *,
        reference_bytes: list[bytes] | None = None,
        aspect_ratio: str = "1:1", image_size: str = "2K",
        run_id: str | None = None, max_polls: int = 240, poll_interval: float = 5,
    ) -> Path:
        if not local_channel_available():
            raise ValueError("豆包工作仅支持本机 macOS 社区版")
        refs = [{
            "name": f"reference-{index}.png",
            "contentBase64": base64.b64encode(data).decode(),
        } for index, data in enumerate(reference_bytes or [])]
        output = Path(output_path)
        instructions = (
            f"{prompt.strip()}\n目标画幅{aspect_ratio}，目标分辨率{image_size}，"
            "返回一张完整图片。"
        )
        job, fresh = await asyncio.to_thread(
            self._bind, output, str(run_id or "direct"), instructions, refs,
        )
        job_id = job["id"]
        if fresh:
            await asyncio.to_thread(self.store.submit, job_id)
        for _ in range(max_polls):
            job = await asyncio.to_thread(self.store.refresh, job_id)
            if job["status"] == "completed":
                media = next((item for item in job["files"] if item["kind"] == "image"), None)
                if media is None:
                    raise ValueError(f"豆包工作未返回真实图片；原任务 {job_id}")
                source = self.store.file(job_id, media["name"])
                await asyncio.to_thread(self._deliver, source, output, job_id)
                return output
            if job["status"] in {"failed", "outcome_unknown"}:
                raise ValueError(f"豆包工作 {job['status']}；原任务 {job_id}")
            await asyncio.sleep(poll_interval)
        raise ValueError(f"豆包工作等待超时，可查询原任务 {job_id}，不会重复生成")
