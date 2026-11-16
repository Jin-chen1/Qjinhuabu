"""Persistent, at-most-once native jobs with verified local delivery."""

import base64
import hashlib
import io
import ipaddress
import json
import os
import re
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import httpx
from PIL import Image

from .client import GatewayError, NativeClient, completed_reply, session_path

MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_TOTAL_BYTES = 40 * 1024 * 1024
TERMINAL = {"completed", "failed", "outcome_unknown"}
INSTRUCTIONS = {
    "text": "",
    "image": "请直接使用图片生成工具生成并返回真实图片产物。不要仅提供提示词或链接。\n",
    "video": "请直接使用视频生成工具生成并返回真实视频产物。时长遵循下方请求。不要仅提供脚本或链接。\n",
}


def identifier(value: str) -> str:
    return str(UUID(value))


def validate_reference(ref: dict) -> tuple[str, bytes]:
    value = ref.get("contentBase64", "")
    if not isinstance(value, str) or len(value) > MAX_IMAGE_BYTES * 4 // 3 + 4:
        raise ValueError("IMAGE_TOO_LARGE")
    try:
        data = base64.b64decode(value, validate=True)
        if not data or len(data) > MAX_IMAGE_BYTES:
            raise ValueError()
        with Image.open(io.BytesIO(data)) as image:
            if image.format not in {"PNG", "JPEG", "WEBP"}:
                raise ValueError()
            image.verify()
        with Image.open(io.BytesIO(data)) as image:
            image.load()
            ext = {"PNG": ".png", "JPEG": ".jpg", "WEBP": ".webp"}[image.format]
    except Exception:
        raise ValueError("INVALID_REFERENCE_IMAGE") from None
    name = Path(str(ref.get("name", "reference"))).stem[:80]
    name = re.sub(r"[^\w.-]", "_", name) or "reference"
    return name + ext, data


def private_write(path: Path, data: bytes) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)
    path.chmod(0o600)


def safe_text(value: str) -> str:
    # Signed links remain only in memory; delivery uses our local file endpoint.
    return re.sub(r"https?://[^\s<>\"）)]+", "[链接已隐藏，请使用下方本地文件]", value)[:100000]


def validate_public_url(url: str) -> None:
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username
            or parsed.password or parsed.port not in {None, 443}):
        raise GatewayError("INVALID_ARTIFACT_URL")
    addresses = socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
        raise GatewayError("INVALID_ARTIFACT_URL")


def download(url: str, path: Path) -> None:
    # Separate client deliberately carries no gateway Authorization header.
    with httpx.Client(trust_env=False, timeout=120, follow_redirects=False) as client:
        for _ in range(5):
            validate_public_url(url)
            with client.stream("GET", url) as response:
                if response.is_redirect:
                    url = str(response.url.join(response.headers["location"]))
                    continue
                response.raise_for_status()
                size = 0
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "wb") as stream:
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if size > 250 * 1024 * 1024:
                            raise GatewayError("ARTIFACT_TOO_LARGE")
                        stream.write(chunk)
                return
    raise GatewayError("ARTIFACT_REDIRECT_LIMIT")


def verified_media(path: Path, kind: str) -> dict:
    if kind == "image":
        with Image.open(path) as image:
            image.verify()
        with Image.open(path) as image:
            image.load()
            return {
                "extension": {"PNG": ".png", "JPEG": ".jpg", "WEBP": ".webp"}[image.format],
                "width": image.width, "height": image.height,
            }
    with path.open("rb") as stream:
        if stream.read(12)[4:8] != b"ftyp":
            raise GatewayError("INVALID_VIDEO_FILE")
    ffprobe = shutil.which("ffprobe") or str(Path.home() / ".local/bin/ffprobe")
    ffmpeg = shutil.which("ffmpeg") or str(Path.home() / ".local/bin/ffmpeg")
    result = subprocess.run(
        [ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
        capture_output=True, timeout=45, check=True,
    )
    probe = json.loads(result.stdout)
    video = next(stream for stream in probe["streams"] if stream["codec_type"] == "video")
    duration = float(probe["format"]["duration"])
    if duration <= 0 or video["width"] <= 0 or video["height"] <= 0:
        raise GatewayError("INVALID_VIDEO_FILE")
    subprocess.run(
        [ffmpeg, "-v", "error", "-xerror", "-i", str(path), "-f", "null", "-"],
        capture_output=True, timeout=180, check=True,
    )
    return {
        "extension": ".mp4", "width": video["width"], "height": video["height"],
        "duration": round(duration, 3), "codec": video["codec_name"],
    }


class JobStore:
    def __init__(self, state: Path, output: Path, client: NativeClient):
        self.state, self.output, self.client = state, output, client
        self.lock = threading.RLock()
        self.job_locks: dict[str, threading.RLock] = {}
        for directory in (state, output):
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            directory.chmod(0o700)
        for path in state.glob("*.json"):
            job = json.loads(path.read_text())
            if job["status"] in {"queued", "session_pending", "upload_pending", "event_pending"}:
                job["submission_stage"] = job["status"]
                job.update(status="outcome_unknown", error="INTERRUPTED_BEFORE_RECEIPT")
                self.save(job)

    def save(self, job: dict) -> None:
        job["updated"] = time.time()
        private_write(
            self.state / f"{identifier(job['id'])}.json",
            json.dumps(job, ensure_ascii=False).encode(),
        )

    def read(self, job_id: str) -> dict:
        return json.loads((self.state / f"{identifier(job_id)}.json").read_text())

    def public(self, job: dict) -> dict:
        return {key: job[key] for key in (
            "id", "mode", "prompt", "status", "created", "updated", "text", "files", "error",
        )}

    def list(self) -> list:
        with self.lock:
            jobs = [self.public(json.loads(p.read_text())) for p in self.state.glob("*.json")]
            return sorted(jobs, key=lambda job: job["created"], reverse=True)[:100]

    def create(self, job_id: str, mode: str, prompt: str, refs: list) -> tuple[dict, bool]:
        job_id = identifier(job_id)
        prompt = prompt.strip()
        if mode not in {"text", "image", "video"} or not prompt or len(prompt) > 20000:
            raise ValueError("INVALID_REQUEST")
        if len(refs) > 9:
            raise ValueError("TOO_MANY_REFERENCES")
        images = [validate_reference(ref) for ref in refs]
        if sum(len(data) for _, data in images) > MAX_TOTAL_BYTES:
            raise ValueError("REFERENCES_TOO_LARGE")
        digest = hashlib.sha256(json.dumps({
            "mode": mode, "prompt": prompt,
            "refs": [(name, hashlib.sha256(data).hexdigest()) for name, data in images],
        }, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        with self.lock:
            if (self.state / f"{job_id}.json").exists():
                job = self.read(job_id)
                if job["digest"] != digest:
                    raise ValueError("UUID_CONFLICT")
                return self.public(job), False
            job = {
                "id": job_id, "digest": digest, "mode": mode, "prompt": prompt,
                "status": "queued", "created": time.time(), "updated": time.time(),
                "text": "", "files": [], "error": None,
                "session_uuid": str(uuid4()), "event_uuid": str(uuid4()),
                "refs": [],
            }
            for index, (name, data) in enumerate(images):
                filename = f"{job_id}-ref-{index}"
                private_write(self.state / filename, data)
                from .media_library_runtime import capture_reference

                capture_reference(self.state / filename, name, job_id, index)
                job["refs"].append({"name": name, "file": filename, "uuid": str(uuid4())})
            self.save(job)
            return self.public(job), True

    def submit(self, job_id: str) -> None:
        with self.lock:
            job = self.read(job_id)
            if job["status"] != "queued":
                return
            job["status"] = "session_pending"
            job["submission_stage"] = "session_pending"
            self.save(job)
        try:
            session = self.client.call("POST", "/v1/sessions", {
                "uuid": job["session_uuid"], "name": f"DramaClaw · {job['mode']} · {job_id[:8]}",
            })
            job["session_id"] = str(session["id"])
            attachments = []
            for ref in job["refs"]:
                job["status"] = "upload_pending"
                job["submission_stage"] = "upload_pending"
                self.save(job)
                result = self.client.call("POST", "/v1/files", {
                    "uuid": ref["uuid"], "filename": ref["name"],
                    "contentBase64": base64.b64encode((self.state / ref["file"]).read_bytes()).decode(),
                })
                attachments.append({"uri": result["uri"], "name": ref["name"]})
            job["attachments"] = attachments
            job["status"] = "event_pending"
            job["submission_stage"] = "event_pending"
            self.save(job)
            response = self.client.call("POST", session_path(job["session_id"]) + "/events", {
                "uuid": job["event_uuid"], "text": INSTRUCTIONS[job["mode"]] + job["prompt"],
                "files": attachments,
            })
            job["message_id"] = str(response["messages"][0]["id"])
            job.update(status="submitted", error=None)
            self.save(job)
            for ref in job["refs"]:
                (self.state / ref["file"]).unlink(missing_ok=True)
        except GatewayError as error:
            job.update(status="outcome_unknown" if error.unknown else "failed", error=error.code)
            self.save(job)
        except Exception:
            job.update(status="outcome_unknown", error="RECEIPT_UNAVAILABLE")
            self.save(job)

    def refresh(self, job_id: str) -> dict:
        with self.lock:
            job_lock = self.job_locks.setdefault(identifier(job_id), threading.RLock())
        with job_lock:
            job = self.read(job_id)
            unknown_event = (
                job["status"] == "outcome_unknown"
                and job.get("submission_stage") == "event_pending"
                and job.get("session_id")
            )
            recoverable_video_delivery = (
                job["status"] == "failed" and job.get("error") == "NO_VIDEO_ARTIFACT"
                and job["mode"] == "video" and job.get("session_id") and job.get("message_id")
            )
            if (job["status"] not in {"submitted", "delivery_pending"}
                    and not recoverable_video_delivery and not unknown_event):
                return self.public(job)
            try:
                data = self.client.call("GET", session_path(job["session_id"]) + "/messages")
                if unknown_event and not job.get("message_id"):
                    # A private single-event session and complete exact contents
                    # are required; absence or ambiguity never permits a POST.
                    users = [m for m in data.get("messages", [])
                             if m.get("role") == "user"
                             and m.get("session_id") == job["session_id"]]
                    expected = [{"type": "text", "text": INSTRUCTIONS[job["mode"]] + job["prompt"]}]
                    expected += [{"type": "file", **a} for a in job.get("attachments", [])]
                    if (data.get("has_more") is not False or len(users) != 1
                            or (job["refs"] and "attachments" not in job)):
                        return self.public(job)
                    contents = [{k: item[k] for k in ("type", "text", "uri", "name") if k in item}
                                for item in users[0].get("items", [])]
                    if contents != expected or not users[0].get("id"):
                        return self.public(job)
                    job.update(message_id=str(users[0]["id"]), status="submitted", error=None,
                               receipt_recovered=True)
                    self.save(job)
                reply = completed_reply(data.get("messages", []), job["session_id"], job["message_id"])
                if reply is None:
                    messages = data.get("messages", [])
                    if any(m.get("reply_id") == job["message_id"]
                           and m.get("session_id") == job["session_id"]
                           and m.get("role") == "assistant"
                           and m.get("status") in {"failed", "cancelled"} for m in messages):
                        job.update(status="failed", error="GENERATION_FAILED")
                        self.save(job)
                    return self.public(job)
                items = reply.get("items", [])
                job["text"] = safe_text("\n\n".join(
                    str(item.get("text", "")) for item in items if item.get("type") == "text"
                ))
                if job["mode"] == "text":
                    job.update(status="completed", error=None)
                else:
                    artifacts = [item for item in items if item.get("type") == "artifact"
                                 and item.get("artifact_type") == job["mode"]
                                 and item.get("state") == "active"]
                    if not artifacts and job["mode"] == "video":
                        # Some native video tools return an official file-link artifact.
                        # Only the matching completed reply supplies candidates; decoding
                        # below still determines whether the downloaded bytes are video.
                        artifacts = [
                            {"video": {"url": item["link"]["url"]}}
                            for item in items
                            if item.get("type") == "artifact"
                            and item.get("artifact_type") == "link"
                            and item.get("state") == "active"
                            and isinstance(item.get("link"), dict)
                            and isinstance(item["link"].get("url"), str)
                            and urlsplit(item["link"]["url"]).scheme == "https"
                            and urlsplit(item["link"]["url"]).hostname == "aka.doubaocdn.com"
                        ]
                    if not artifacts:
                        job.update(status="failed", error=f"NO_{job['mode'].upper()}_ARTIFACT")
                    else:
                        job["status"] = "delivery_pending"
                        self.save(job)
                        files = []
                        for index, artifact in enumerate(artifacts[:9]):
                            temporary = self.output / f"{job_id}-{index}.part"
                            try:
                                download(artifact[job["mode"]]["url"], temporary)
                                metadata = verified_media(temporary, job["mode"])
                                name = f"{job_id}-{index}{metadata.pop('extension')}"
                                temporary.replace(self.output / name)
                                files.append({"name": name, "kind": job["mode"], **metadata})
                            finally:
                                temporary.unlink(missing_ok=True)
                        job.update(status="completed", files=files, error=None)
                self.save(job)
            except Exception:
                job["error"] = "QUERY_OR_DELIVERY_FAILED"
                self.save(job)
            return self.public(job)

    def file(self, job_id: str, name: str) -> Path:
        job = self.read(job_id)
        if not any(item["name"] == name for item in job["files"]):
            raise FileNotFoundError()
        return self.output / name
