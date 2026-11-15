"""Single-user macOS CE workbench endpoints."""

import ipaddress
import json
import os
import sys
from urllib.parse import urlsplit

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import FileResponse

from novelvideo.doubao_work.client import NativeClient
from novelvideo.doubao_work.service import store
from novelvideo.shared import runtime_env


def local_only(request: Request) -> None:
    try:
        local_ports = {5173, 8780}
        bound_port = int(os.environ.get("NOVELVIDEO_API_PORT", "8780"))
        if not 1 <= bound_port <= 65535:
            raise ValueError("invalid local API port")
        local_ports.add(bound_port)
        local_peer = request.client and ipaddress.ip_address(request.client.host).is_loopback
        host = urlsplit("//" + request.headers.get("host", ""))
        origin = request.headers.get("origin")
        valid_origin = not origin or (
            urlsplit(origin).scheme == "http"
            and urlsplit(origin).hostname in {"127.0.0.1", "localhost", "::1"}
            and urlsplit(origin).port in local_ports
        )
        valid_host = host.hostname in {"127.0.0.1", "localhost", "::1"} and host.port in local_ports
    except ValueError:
        local_peer = valid_host = valid_origin = False
    if not (sys.platform == "darwin" and runtime_env.is_ce_effective()
            and local_peer and valid_host and valid_origin):
        raise HTTPException(403, "仅本机 macOS 社区版可使用豆包工作")


router = APIRouter(prefix="/doubao-work", dependencies=[Depends(local_only)])


async def guard_native_media_selection(request: Request) -> None:
    """Apply the native boundary to media requests in the existing routers."""
    if request.method not in {"POST", "PUT", "PATCH"}:
        return
    # Breakdown and script select the native channel implicitly in local CE; their bodies
    # contains no model selection for the recursive check below to detect.
    if request.url.path.endswith((
        "/freezone/video-breakdown", "/freezone/text/story-script",
        "/freezone/recipes/generate-text",
        "/freezone/recipes/compile", "/freezone/recipes/compile-batch",
    )):
        from novelvideo.doubao_work.video import local_channel_available

        if local_channel_available():
            local_only(request)
    if "application/json" not in request.headers.get("content-type", ""):
        return
    try:
        body = await request.json()
    except ValueError:
        return
    def selected(value):
        if isinstance(value, dict):
            return any(selected(item) for item in value.values())
        if isinstance(value, list):
            return any(selected(item) for item in value)
        return isinstance(value, str) and value.strip().lower() in {
            "doubao_work", "doubao_work/doubao_work", "豆包工作（本机授权）",
            "ark_dual_reference",
        }
    if selected(body):
        local_only(request)


@router.get("/status")
def status() -> dict:
    return NativeClient().health()


@router.get("/media-library")
def media_library_status() -> dict:
    from novelvideo.doubao_work.media_library_runtime import status
    return status()


@router.post("/media-library/sync", status_code=202)
def media_library_sync() -> dict:
    from novelvideo.doubao_work.media_library_runtime import request_sync, status
    request_sync()
    return status()


@router.get("/jobs")
def jobs() -> list:
    return store().list()


@router.post("/jobs", status_code=202)
async def create_job(request: Request, background: BackgroundTasks) -> dict:
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > 58 * 1024 * 1024:
            raise HTTPException(413, "参考图片总大小超限")
    try:
        payload = json.loads(body)
        job, fresh = store().create(
            payload["id"], payload["mode"], payload["prompt"], payload.get("refs", []),
        )
    except (ValueError, KeyError, TypeError, AttributeError):
        raise HTTPException(400, "请求无效或任务编号与内容冲突") from None
    if fresh:
        background.add_task(store().submit, job["id"])
    return job


@router.get("/jobs/{job_id}")
def get_job(job_id: str) -> dict:
    try:
        return store().refresh(job_id)
    except (ValueError, FileNotFoundError):
        raise HTTPException(404, "任务不存在") from None


@router.get("/jobs/{job_id}/files/{name}")
def get_file(job_id: str, name: str) -> FileResponse:
    try:
        path = store().file(job_id, name)
        if not path.is_file():
            raise FileNotFoundError()
        return FileResponse(
            path, filename=name, content_disposition_type="inline",
            headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
        )
    except (ValueError, FileNotFoundError):
        raise HTTPException(404, "文件不存在") from None
