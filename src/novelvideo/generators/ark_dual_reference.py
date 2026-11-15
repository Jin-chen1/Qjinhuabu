"""Official Ark video + product-image adapter, independent of NewAPI.

Contract checked 2026-11-15:
https://ark.volcengine.com/region:cn-beijing/docs/create-video-generation-task-api
https://ark.volcengine.com/region:cn-beijing/docs/get-video-generation-task-api
https://ark.volcengine.com/region:cn-beijing/docs/seedance-2-5
https://ark.volcengine.com/region:cn-beijing/docs/model-list

Factory integration: backend == BACKEND -> ArkDualReferenceVideoGenerator(
    api_key=config["apiKey"], base_url=config["baseUrl"], model=config["model"]).
The router must obtain config from its authorized server-side store, never from
an untrusted request. This adapter does not read environment/gateway credentials.

generate(..., references=[ShotReference("video", source, "reference_video"),
                         ShotReference("image", product, "reference_image")],
         gen_mode="video_edit", run_id=queue_task_id)
returns a VideoGenResult-compatible result. Inspect acceptance AND error_code:
not_accepted proves no task was created; outcome_unknown without a task ID MUST
NOT be resubmitted; accepted with an ID is recoverable through get_result or
generate on the same output. GET /models checks authentication/catalog only,
not model activation, dual-reference generation, fees, or semantic fidelity.

Default Seedance 2.5 edit preserves source geometry/duration: adaptive/-1.
all_reference requests a new reference video with an explicit 4-30s duration.
Seedance 2.0 models support all_reference only, with 4-15s output duration.
Images support official data URIs. Videos support public URLs/asset IDs ONLY;
local video uses the configured existing relay, or lazily imports the separately
owned dual_reference_media_relay.ensure_public_video. No fabricated upload API.

Receipts contain hashes and safe task metadata, never keys, prompts or signed
URLs. Keep the state directory when recovering. Do not delete a receipt to retry
unknown work. A different explicit run_id may replace ONLY settled receipts.
Platform/organization billing integration remains the factory/router's concern;
organization-scoped calls fail closed here rather than using a local secret.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import io
import ipaddress
import json
import math
import os
import re
import shutil
import socket
import tempfile
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urljoin, urlsplit
from uuid import uuid4

import httpx
import portalocker
from PIL import Image
from pydantic import SecretStr

from novelvideo.egress_context import ambient_egress_context
from novelvideo.generators.video_generator import (
    VideoGeneratorBase,
    VideoGenResult,
    VideoGenStatus,
    _run_video_subprocess,
    _video_egress_context,
)
from novelvideo.shared.provider_costs import is_definite_no_cost_http_rejection

BACKEND = "ark_dual_reference"
ARK_BASE_URL = "https://ark.cn-beijing.volces.com/api/v3"
DEFAULT_MODEL = "doubao-seedance-2-5-260628"
# Exact official IDs, not gateway aliases or unverifiable Endpoint IDs.
SUPPORTED_MODELS = {
    DEFAULT_MODEL: {
        "images": 30,
        "videos": 10,
        "duration": 30,
        "resolutions": ("480p", "720p", "1080p"),
    },
    "doubao-seedance-2-0-260128": {
        "images": 9,
        "videos": 3,
        "duration": 15,
        "resolutions": ("480p", "720p", "1080p", "4k"),
    },
    "doubao-seedance-2-0-fast-260128": {
        "images": 9,
        "videos": 3,
        "duration": 15,
        "resolutions": ("480p", "720p"),
    },
    "doubao-seedance-2-0-mini-260615": {
        "images": 9,
        "videos": 3,
        "duration": 15,
        "resolutions": ("480p", "720p"),
    },
}
_RATIOS = {"adaptive", "16:9", "4:3", "1:1", "3:4", "9:16", "21:9"}
_TASK_ID = re.compile(r"cgt-[A-Za-z0-9_-]{1,120}")
_ASSET_ID = re.compile(r"asset://[A-Za-z0-9_-]{1,128}")
_SAFE_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
_IMAGE_MIMES = {
    "PNG": "image/png",
    "JPEG": "image/jpeg",
    "WEBP": "image/webp",
    "BMP": "image/bmp",
    "TIFF": "image/tiff",
    "GIF": "image/gif",
}
_TERMINAL_PHASES = {"succeeded", "failed", "expired", "cancelled", "not_accepted"}
_PROVIDER_CODES = {
    "InvalidParameter",
    "InvalidParameter.TaskTypeConstraint",
    "InvalidParameter.TaskTypeMismatch",
    "InvalidParameter.UnsupportedValue",
    "InvalidApiKey",
    "AuthenticationError",
    "AccessDenied",
    "ModelNotOpen",
    "QuotaExceeded",
    "RateLimitExceeded",
    "InternalServiceError",
    "InputImageSensitiveContentDetected",
    "InputVideoSensitiveContentDetected",
    "OutputVideoSensitiveContentDetected",
    "OutputAudioSensitiveContentDetected",
    "InputImageSensitiveContentDetected.PolicyViolation",
    "InputVideoSensitiveContentDetected.PolicyViolation",
    "OutputVideoSensitiveContentDetected.PolicyViolation",
    "OutputAudioSensitiveContentDetected.PolicyViolation",
}
_MESSAGES = {
    "ARK_MODEL_UNSUPPORTED": "仅支持已核验协议的火山方舟官方视频模型，请选择受支持的完整模型编号。",
    "ARK_EDIT_MODEL_REQUIRED": "原视频编辑模式需要选择受支持的 Seedance 2.5 模型；2.0 系列仅支持参考生成。",
    "ARK_BASE_URL_INVALID": "火山方舟凭证只能发送至官方北京区域接口。",
    "ARK_KEY_MISSING": "请先在安全配置中完成火山方舟密钥授权。",
    "ARK_INPUT_INVALID": "双参考素材或生成参数不符合通道要求，请检查后重试。",
    "ARK_INPUT_CONFLICT": "输入与已绑定任务不一致，请先核实原任务，禁止重复提交。",
    "ARK_MEDIA_INVALID": "参考素材缺失、格式无效或超出支持范围。",
    "ARK_MEDIA_URL_INVALID": "参考素材必须使用不含账号密码的有效公网链接。",
    "ARK_VIDEO_PUBLIC_URL_REQUIRED": "视频不支持内嵌数据，请使用公网链接或官方素材编号。",
    "ARK_VIDEO_CONSTRAINT_INVALID": "参考视频的时长、格式、尺寸或帧率不符合模型要求。",
    "ARK_VIDEO_RELAY_UNAVAILABLE": "未配置可用的公网视频中转服务，尚未提交生成任务。",
    "ARK_VIDEO_RELAY_FAILED": "公网视频中转失败，尚未提交火山方舟生成任务。",
    "ARK_CONNECT_FAILED": "连接火山方舟失败，生成任务尚未受理。",
    "ARK_SUBMIT_REJECTED": "火山方舟明确拒绝了请求，生成任务未受理。",
    "ARK_SUBMISSION_UNKNOWN": "提交结果未知，请核实原请求，禁止重复提交。",
    "ARK_QUERY_UNKNOWN": "暂时无法核实任务状态，请继续查询同一任务。",
    "ARK_POLL_TIMEOUT": "本轮等待已结束，请继续查询原任务，不要重新提交。",
    "ARK_TASK_FAILED": "火山方舟确认已受理的生成任务失败。",
    "ARK_TASK_EXPIRED": "火山方舟确认已受理的生成任务已过期。",
    "ARK_TASK_CANCELLED": "火山方舟确认已受理的生成任务已取消。",
    "ARK_DELIVERY_FAILED": "原任务已受理，但成片下载或核验失败，请恢复原任务，不要重新生成。",
    "ARK_STATE_UNAVAILABLE": "无法核实本地任务回执，请检查原任务，禁止重复提交。",
    "ARK_ORGANIZATION_DENIED": "本机火山方舟凭证仅支持本机计费主体和本机凭证来源。",
    "ARK_AUTH_CHECK_FAILED": "火山方舟授权或模型目录核验失败。",
}


class ArkDualReferenceError(RuntimeError):
    """Stable, credential-free error; provider free text is never propagated."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(_MESSAGES.get(code, "火山方舟操作失败。"))


@dataclass
class ArkVideoGenResult(VideoGenResult):
    video_url: str | None = field(default=None, repr=False)
    last_frame_url: str | None = field(default=None, repr=False)
    acceptance: str = "not_accepted"
    error_code: str = ""
    provider_error_code: str = ""
    request_id: str = ""
    status_code: int | None = None
    usage: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class ArkAuthorizationResult:
    authorized: bool
    available_models: tuple[str, ...] = ()
    verification_level: str = "models"
    dual_reference_verified: bool = False
    error_code: str = ""
    status_code: int | None = None


def _fail(
    code: str,
    *,
    acceptance: str = "not_accepted",
    task_id: str | None = None,
    status: VideoGenStatus = VideoGenStatus.FAILED,
    **kwargs,
) -> ArkVideoGenResult:
    return ArkVideoGenResult(
        status=status,
        task_id=task_id,
        provider_task_id=task_id,
        acceptance=acceptance,
        error_code=code,
        error=_MESSAGES[code],
        **kwargs,
    )


def _url_syntax(value: str, *, https_only: bool = False) -> str:
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").lower()
        port = parsed.port
        if (
            parsed.scheme not in ({"https"} if https_only else {"http", "https"})
            or not host
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or any(char.isspace() or ord(char) < 32 for char in value)
            or host in {"localhost", "localhost.localdomain"}
            or host.endswith((".localhost", ".local", ".internal"))
            or port not in {None, 80, 443}
        ):
            raise ValueError
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            if "." not in host:
                raise ValueError
        else:
            if not address.is_global:
                raise ValueError
    except (TypeError, ValueError):
        raise ArkDualReferenceError("ARK_MEDIA_URL_INVALID") from None
    return value


async def _public_address(value: str, *, https_only: bool = False) -> str:
    _url_syntax(value, https_only=https_only)
    parsed = urlsplit(value)
    try:
        addresses = await asyncio.wait_for(
            asyncio.to_thread(
                socket.getaddrinfo,
                parsed.hostname,
                parsed.port or (443 if parsed.scheme == "https" else 80),
                type=socket.SOCK_STREAM,
            ),
            timeout=10,
        )
        if not addresses or any(
            not ipaddress.ip_address(item[4][0]).is_global for item in addresses
        ):
            raise ValueError
    except (OSError, ValueError, TimeoutError):
        raise ArkDualReferenceError("ARK_MEDIA_URL_INVALID") from None
    return addresses[0][4][0]


async def _public_url(value: str, *, https_only: bool = False) -> str:
    await _public_address(value, https_only=https_only)
    return value


def _image_data(source: str) -> str:
    try:
        if source.startswith("data:"):
            header, encoded = source.split(",", 1)
            if not header.startswith("data:image/") or not header.endswith(";base64"):
                raise ValueError
            if len(encoded) >= 40 * 1024 * 1024:
                raise ValueError
            data = base64.b64decode(encoded, validate=True)
        else:
            path = Path(source)
            if (
                path.is_symlink()
                or not path.is_file()
                or path.stat().st_size >= 30 * 1024 * 1024
            ):
                raise ValueError
            data = path.read_bytes()
        if not data or len(data) >= 30 * 1024 * 1024:
            raise ValueError
        with Image.open(io.BytesIO(data)) as image:
            width, height = image.size
            mime = _IMAGE_MIMES[image.format]
            if not (
                300 <= width <= 6000
                and 300 <= height <= 6000
                and 0.4 <= width / height <= 2.5
            ):
                raise ValueError
            image.verify()
        # MIME follows actual bytes, never the filename or untrusted data prefix.
        return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"
    except (
        OSError,
        ValueError,
        KeyError,
        binascii.Error,
        Image.DecompressionBombError,
    ):
        raise ArkDualReferenceError("ARK_MEDIA_INVALID") from None


def _inspect_video(path: Path) -> dict[str, Any]:
    """Probe AND fully decode; never treat a JSON success/MP4 header as delivery."""
    ffprobe = shutil.which("ffprobe") or str(Path.home() / ".local/bin/ffprobe")
    ffmpeg = shutil.which("ffmpeg") or str(Path.home() / ".local/bin/ffmpeg")
    try:
        probe = _run_video_subprocess(
            [
                ffprobe,
                "-v",
                "error",
                "-show_streams",
                "-show_format",
                "-of",
                "json",
                str(path),
            ],
            timeout=60,
        )
        if probe.returncode != 0:
            raise ValueError
        metadata = json.loads(probe.stdout)
        stream = next(s for s in metadata["streams"] if s.get("codec_type") == "video")
        duration = float(metadata["format"]["duration"])
        fps = float(Fraction(stream["avg_frame_rate"]))
        if (
            not math.isfinite(duration)
            or duration <= 0
            or not math.isfinite(fps)
            or fps <= 0
        ):
            raise ValueError
        decoded = _run_video_subprocess(
            [ffmpeg, "-v", "error", "-xerror", "-i", str(path), "-f", "null", "-"],
            timeout=180,
        )
        if decoded.returncode != 0:
            raise ValueError
        return {
            "duration": duration,
            "width": int(stream["width"]),
            "height": int(stream["height"]),
            "fps": fps,
            "codec": stream["codec_name"],
        }
    except Exception:
        raise ArkDualReferenceError("ARK_MEDIA_INVALID") from None


class ArkDualReferenceVideoGenerator(VideoGeneratorBase):
    """Async official Ark adapter; construction never submits or probes a model."""

    def __init__(
        self,
        api_key: str | SecretStr,
        base_url: str = ARK_BASE_URL,
        model: str = DEFAULT_MODEL,
        resolution: str = "720p",
        generate_audio: bool = True,
        return_last_frame: bool = False,
        state_dir: str | Path | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        egress_context: object | None = None,
    ):
        if not isinstance(base_url, str) or base_url.rstrip("/") != ARK_BASE_URL:
            raise ArkDualReferenceError("ARK_BASE_URL_INVALID")
        if model not in SUPPORTED_MODELS:
            raise ArkDualReferenceError("ARK_MODEL_UNSUPPORTED")
        key = api_key.get_secret_value() if isinstance(api_key, SecretStr) else api_key
        if (
            not isinstance(key, str)
            or not key.strip()
            or key != key.strip()
            or any(ord(c) < 33 or ord(c) > 126 for c in key)
        ):
            raise ArkDualReferenceError("ARK_KEY_MISSING")
        if (
            resolution not in SUPPORTED_MODELS[model]["resolutions"]
            or not isinstance(generate_audio, bool)
            or not isinstance(return_last_frame, bool)
        ):
            raise ArkDualReferenceError("ARK_INPUT_INVALID")
        self._api_key = SecretStr(key)
        self.base_url = ARK_BASE_URL
        self.model = model
        self.resolution = resolution
        self.generate_audio = generate_audio
        self.return_last_frame = return_last_frame
        self._transport = transport
        self._egress_context = egress_context
        if state_dir is None:
            from novelvideo.config import STATE_DIR

            state_dir = Path(STATE_DIR) / "ark-dual-reference"
        self.state_dir = Path(state_dir)

    def _guard(self, context: object | None = None) -> None:
        try:
            resolved = _video_egress_context(context or self._egress_context)
            resolved = resolved or ambient_egress_context()
            if resolved is not None and (
                resolved.billing_principal.kind != "local"
                or resolved.credential.source != "local"
            ):
                raise ArkDualReferenceError("ARK_ORGANIZATION_DENIED")
        except ArkDualReferenceError:
            raise
        except Exception:
            raise ArkDualReferenceError("ARK_ORGANIZATION_DENIED") from None

    def _client(self, *, authenticated: bool = True) -> httpx.AsyncClient:
        headers = (
            {"Authorization": f"Bearer {self._api_key.get_secret_value()}"}
            if authenticated
            else {}
        )
        return httpx.AsyncClient(
            headers=headers,
            timeout=httpx.Timeout(60, connect=10),
            follow_redirects=False,
            trust_env=False,
            transport=self._transport,
        )

    def _safe_id(self, value: object, pattern: re.Pattern = _SAFE_ID) -> str:
        return (
            value
            if isinstance(value, str)
            and pattern.fullmatch(value)
            and self._api_key.get_secret_value() not in value
            else ""
        )

    def _response_metadata(self, response: httpx.Response) -> dict[str, Any]:
        request_id = self._safe_id(response.headers.get("x-request-id", ""))
        provider_code = ""
        try:
            data = response.json()
            error = data.get("error") if isinstance(data, dict) else None
            code = error.get("code") if isinstance(error, dict) else None
            if code in _PROVIDER_CODES and self._api_key.get_secret_value() not in code:
                provider_code = code
        except (ValueError, TypeError):
            pass
        return {
            "request_id": request_id,
            "status_code": response.status_code,
            "provider_error_code": provider_code,
        }

    async def validate_authorization(self) -> ArkAuthorizationResult:
        """GET /models only: does not establish model activation or paid capability."""
        try:
            self._guard()
            async with self._client() as client:
                response = await client.get(self.base_url + "/models")
            if response.status_code != 200:
                return ArkAuthorizationResult(
                    False,
                    status_code=response.status_code,
                    error_code="ARK_AUTH_CHECK_FAILED",
                )
            data = response.json().get("data")
            if not isinstance(data, list):
                raise ValueError
            advertised = {
                item.get("id")
                for item in data
                if isinstance(item, dict) and isinstance(item.get("id"), str)
            }
            models = tuple(model for model in SUPPORTED_MODELS if model in advertised)
            return ArkAuthorizationResult(
                True, available_models=models, status_code=200
            )
        except Exception:
            return ArkAuthorizationResult(False, error_code="ARK_AUTH_CHECK_FAILED")

    async def _prepare(
        self,
        image_path: str | None,
        prompt: str,
        references: Any,
        gen_mode: str,
        aspect_ratio: str,
        duration: float,
        last_frame_path: str | None,
    ) -> tuple[dict[str, Any], str, dict[int, Path]]:
        if last_frame_path or not isinstance(prompt, str) or not prompt.strip():
            raise ArkDualReferenceError("ARK_INPUT_INVALID")
        if (
            gen_mode not in {"video_edit", "all_reference"}
            or aspect_ratio not in _RATIOS
        ):
            raise ArkDualReferenceError("ARK_INPUT_INVALID")
        is_25 = self.model == DEFAULT_MODEL
        if gen_mode == "video_edit" and not is_25:
            raise ArkDualReferenceError("ARK_EDIT_MODEL_REQUIRED")
        edit = is_25 and gen_mode == "video_edit"
        if edit:
            if aspect_ratio != "adaptive":
                raise ArkDualReferenceError("ARK_INPUT_INVALID")
            ratio, seconds = "adaptive", -1
        else:
            try:
                seconds = float(duration)
                if (
                    isinstance(duration, bool)
                    or not math.isfinite(seconds)
                    or seconds != int(seconds)
                    or not 4 <= seconds <= SUPPORTED_MODELS[self.model]["duration"]
                ):
                    raise ValueError
                seconds = int(seconds)
            except (TypeError, ValueError, OverflowError):
                raise ArkDualReferenceError("ARK_INPUT_INVALID") from None
            ratio = aspect_ratio
        if references is not None and not isinstance(references, (list, tuple)):
            raise ArkDualReferenceError("ARK_INPUT_INVALID")
        raw_refs = list(references or [])
        if image_path:
            raw_refs.append({"type": "image", "path": image_path})
        counts = {"image": 0, "video": 0}
        seen = set()
        content = [{"type": "text", "text": prompt.strip()}]
        identities = []
        local_videos = {}
        local_duration = 0.0
        for ref in raw_refs:
            if isinstance(ref, dict):
                kind, source, role = (
                    ref.get("type"),
                    ref.get("path") or ref.get("url"),
                    ref.get("role"),
                )
            else:
                kind, source, role = (
                    getattr(ref, "type", None),
                    getattr(ref, "path", None),
                    getattr(ref, "role", None),
                )
            if (
                kind not in counts
                or not isinstance(source, (str, Path))
                or not str(source)
            ):
                raise ArkDualReferenceError("ARK_INPUT_INVALID")
            source = str(source)
            if role in {"first_frame", "last_frame", "reference_audio"} or (
                role in {"reference_image", "reference_video"}
                and role != f"reference_{kind}"
            ):
                raise ArkDualReferenceError("ARK_INPUT_INVALID")
            if (kind, source) in seen:
                continue
            seen.add((kind, source))
            counts[kind] += 1
            identity = source
            if source.startswith("asset://"):
                if not _ASSET_ID.fullmatch(source):
                    raise ArkDualReferenceError("ARK_MEDIA_INVALID")
                url = source
            elif source.startswith(("https://", "http://")):
                url = await _public_url(source)
            elif kind == "image":
                url = await asyncio.to_thread(_image_data, source)
                identity = hashlib.sha256(url.encode()).hexdigest()
            else:
                if source.startswith("data:"):
                    raise ArkDualReferenceError("ARK_VIDEO_PUBLIC_URL_REQUIRED")
                path = Path(source)
                try:
                    if (
                        path.is_symlink()
                        or not path.is_file()
                        or path.suffix.lower() not in {".mp4", ".mov"}
                        or path.stat().st_size > 200 * 1024 * 1024
                    ):
                        raise ValueError
                    info = await asyncio.to_thread(_inspect_video, path)
                    width, height = info["width"], info["height"]
                    if (
                        not (4 if edit else 2)
                        <= info["duration"]
                        <= SUPPORTED_MODELS[self.model]["duration"]
                    ):
                        raise ValueError
                    if not (
                        300 <= width <= 6000
                        and 300 <= height <= 6000
                        and 0.4 <= width / height <= 2.5
                        and 407696 <= width * height <= 8295044
                        and 24 <= info["fps"] <= 60
                        and info["codec"] in {"h264", "hevc"}
                    ):
                        raise ValueError
                    local_duration += info["duration"]
                    identity = await asyncio.to_thread(
                        lambda: hashlib.sha256(path.read_bytes()).hexdigest(),
                    )
                except Exception:
                    raise ArkDualReferenceError(
                        "ARK_VIDEO_CONSTRAINT_INVALID"
                    ) from None
                local_videos[len(content)] = path
                url = ""  # Filled only after the stable receipt has been claimed.
            identities.append((kind, identity))
            content.append(
                {
                    "type": f"{kind}_url",
                    f"{kind}_url": {"url": url},
                    "role": f"reference_{kind}",
                }
            )
        limits = SUPPORTED_MODELS[self.model]
        if (
            not counts["image"]
            or not counts["video"]
            or counts["image"] > limits["images"]
            or counts["video"] > limits["videos"]
            or local_duration > limits["duration"]
        ):
            raise ArkDualReferenceError("ARK_INPUT_INVALID")
        payload = {
            "model": self.model,
            "content": content,
            "ratio": ratio,
            "duration": seconds,
            "resolution": self.resolution,
            "generate_audio": self.generate_audio,
            "return_last_frame": self.return_last_frame,
        }
        if is_25:
            payload["omni_reference_task_type"] = "edit" if edit else "reference"
        # Hash logical source content before relay tokens/signatures are allocated.
        fingerprint = {key: value for key, value in payload.items() if key != "content"}
        fingerprint.update(prompt=prompt.strip(), references=identities)
        digest = hashlib.sha256(
            self._api_key.get_secret_value().encode()
            + b"\0"
            + json.dumps(fingerprint, sort_keys=True).encode(),
        ).hexdigest()
        return payload, digest, local_videos

    async def _relay_video(self, path: Path) -> str:
        from novelvideo.storage.media_relay import (
            MediaRelayConfigError,
            get_media_relay,
        )

        try:
            relay = await asyncio.to_thread(get_media_relay)
        except MediaRelayConfigError:
            try:
                from novelvideo.dual_reference_media_relay import ensure_public_video
            except ImportError:
                raise ArkDualReferenceError("ARK_VIDEO_RELAY_UNAVAILABLE") from None
            try:
                url = await ensure_public_video(path)
            except Exception:
                raise ArkDualReferenceError("ARK_VIDEO_RELAY_FAILED") from None
        except Exception:
            raise ArkDualReferenceError("ARK_VIDEO_RELAY_FAILED") from None
        else:
            # Only configuration absence selects the fallback. An upload failure
            # must not silently transmit the user's video to another service.
            try:
                data = await asyncio.to_thread(path.read_bytes)
                url = await asyncio.to_thread(
                    relay.upload_bytes,
                    data,
                    ext=path.suffix.lower().lstrip("."),
                    ttl=3600,
                    resource_type="video",
                )
            except Exception:
                raise ArkDualReferenceError("ARK_VIDEO_RELAY_FAILED") from None
        try:
            return await _public_url(url, https_only=True)
        except Exception:
            raise ArkDualReferenceError("ARK_VIDEO_RELAY_FAILED") from None

    def _receipt_path(self, output_path: str) -> Path:
        binding = hashlib.sha256(str(Path(output_path).resolve()).encode()).hexdigest()
        return self.state_dir / f"receipt-{binding}.json"

    @staticmethod
    def _write_receipt(path: Path, record: dict) -> None:
        fd, name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(record, stream, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, path)
        finally:
            Path(name).unlink(missing_ok=True)

    def _read_receipt(self, path: Path) -> dict | None:
        if path.is_symlink():
            raise ValueError
        if not path.exists():
            return None
        record = json.loads(path.read_text(encoding="utf-8"))
        if (
            not isinstance(record, dict)
            or record.get("version") != 1
            or record.get("acceptance")
            not in {"not_accepted", "accepted", "outcome_unknown"}
            or record.get("phase")
            not in _TERMINAL_PHASES | {"submitting", "accepted", "delivery_pending"}
            or any(
                not isinstance(record.get(key), str)
                for key in ("token", "digest", "run_digest", "error_code")
            )
            or not re.fullmatch(r"[a-f0-9]{32}", record["token"])
            or not re.fullmatch(r"[a-f0-9]{64}", record["digest"])
            or not re.fullmatch(r"[a-f0-9]{64}", record["run_digest"])
            or record["error_code"]
            and record["error_code"] not in _MESSAGES
            or record.get("task_id")
            and not self._safe_id(record["task_id"], _TASK_ID)
            or record.get("request_id")
            and not self._safe_id(record["request_id"])
            or not isinstance(record.get("provider_error_code"), str)
            or record["provider_error_code"]
            and (
                record["provider_error_code"] not in _PROVIDER_CODES
                or self._api_key.get_secret_value() in record["provider_error_code"]
            )
            or record.get("status_code") is not None
            and (
                type(record["status_code"]) is not int
                or not 100 <= record["status_code"] <= 599
            )
        ):
            raise ValueError
        phase, acceptance, task_id = (
            record["phase"],
            record["acceptance"],
            record.get("task_id"),
        )
        if (
            (
                phase == "submitting"
                and (acceptance != "outcome_unknown" or task_id is not None)
            )
            or (
                phase == "not_accepted"
                and (acceptance != "not_accepted" or task_id is not None)
            )
            or (
                phase not in {"submitting", "not_accepted"}
                and (acceptance != "accepted" or not task_id)
            )
        ):
            raise ValueError
        return record

    def _claim(self, output: str, digest: str, run_id: str) -> tuple[dict, bool, bool]:
        path = self._receipt_path(output)
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock_path = Path(str(path) + ".lock")
        with portalocker.Lock(str(lock_path), timeout=10):
            lock_path.chmod(0o600)
            record = self._read_receipt(path)
            run_digest = hashlib.sha256(run_id.encode()).hexdigest()
            if record and (
                record["run_digest"] == run_digest
                or record["phase"] not in _TERMINAL_PHASES
            ):
                return record, False, record["digest"] != digest
            record = {
                "version": 1,
                "token": uuid4().hex,
                "digest": digest,
                "run_digest": run_digest,
                "acceptance": "outcome_unknown",
                "phase": "submitting",
                "task_id": None,
                "error_code": "ARK_SUBMISSION_UNKNOWN",
                "request_id": "",
                "status_code": None,
                "provider_error_code": "",
            }
            self._write_receipt(path, record)
            return record, True, False

    def _save(
        self, output: str, record: dict, result: ArkVideoGenResult, phase: str
    ) -> dict:
        path = self._receipt_path(output)
        with portalocker.Lock(str(path) + ".lock", timeout=10):
            current = self._read_receipt(path)
            if not current or current["token"] != record["token"]:
                raise ValueError
            current.update(
                acceptance=result.acceptance,
                phase=phase,
                task_id=result.task_id,
                error_code=result.error_code,
                request_id=result.request_id,
                status_code=result.status_code,
                provider_error_code=result.provider_error_code,
            )
            self._write_receipt(path, current)
            return current

    @staticmethod
    def _cached(record: dict) -> ArkVideoGenResult:
        result = _fail(
            record["error_code"] or "ARK_QUERY_UNKNOWN",
            acceptance=record["acceptance"],
            task_id=record.get("task_id"),
        )
        result.request_id = record.get("request_id", "")
        result.status_code = record.get("status_code")
        result.provider_error_code = record.get("provider_error_code", "")
        return result

    async def _submit(self, payload: dict) -> ArkVideoGenResult:
        try:
            async with self._client() as client:
                response = await client.post(
                    self.base_url + "/contents/generations/tasks", json=payload
                )
            meta = self._response_metadata(response)
            if not 200 <= response.status_code < 300:
                rejected = is_definite_no_cost_http_rejection(response.status_code)
                return _fail(
                    "ARK_SUBMIT_REJECTED" if rejected else "ARK_SUBMISSION_UNKNOWN",
                    acceptance="not_accepted" if rejected else "outcome_unknown",
                    **meta,
                )
            data = response.json()
            task_id = self._safe_id(
                data.get("id") if isinstance(data, dict) else None, _TASK_ID
            )
            if not task_id:
                return _fail(
                    "ARK_SUBMISSION_UNKNOWN", acceptance="outcome_unknown", **meta
                )
            return ArkVideoGenResult(
                status=VideoGenStatus.PENDING,
                task_id=task_id,
                provider_task_id=task_id,
                acceptance="accepted",
                **meta,
            )
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
            return _fail("ARK_CONNECT_FAILED")
        except Exception:
            return _fail("ARK_SUBMISSION_UNKNOWN", acceptance="outcome_unknown")

    async def get_result(self, task_id: str) -> ArkVideoGenResult:
        """Read the original Ark task once. No POST, upload, download or retry."""
        if not self._safe_id(task_id, _TASK_ID):
            return _fail("ARK_INPUT_INVALID")
        meta: dict[str, Any] = {}
        try:
            self._guard()
            async with self._client() as client:
                response = await client.get(
                    self.base_url + "/contents/generations/tasks/" + task_id
                )
            meta = self._response_metadata(response)
            data = response.json()
            if (
                response.status_code != 200
                or not isinstance(data, dict)
                or data.get("id") != task_id
                or data.get("model") != self.model
            ):
                raise ValueError
            status = data.get("status")
            statuses = {
                "queued": VideoGenStatus.PENDING,
                "running": VideoGenStatus.PROCESSING,
                "succeeded": VideoGenStatus.DONE,
                "failed": VideoGenStatus.FAILED,
                "expired": VideoGenStatus.FAILED,
                "cancelled": VideoGenStatus.FAILED,
            }
            if status not in statuses:
                raise ValueError
            usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
            usage = {
                key: value
                for key, value in usage.items()
                if key in {"completion_tokens", "total_tokens"}
                and type(value) is int
                and value >= 0
            }
            if status in {"failed", "expired", "cancelled"}:
                return _fail(
                    {
                        "failed": "ARK_TASK_FAILED",
                        "expired": "ARK_TASK_EXPIRED",
                        "cancelled": "ARK_TASK_CANCELLED",
                    }[status],
                    acceptance="accepted",
                    task_id=task_id,
                    usage=usage,
                    **meta,
                )
            result = ArkVideoGenResult(
                status=statuses[status],
                task_id=task_id,
                provider_task_id=task_id,
                acceptance="accepted",
                usage=usage,
                **meta,
            )
            if status == "succeeded":
                content = data["content"]
                result.video_url = _url_syntax(content["video_url"], https_only=True)
                if content.get("last_frame_url"):
                    result.last_frame_url = _url_syntax(
                        content["last_frame_url"], https_only=True
                    )
                duration = data.get("duration", 0)
                if (
                    type(duration) in {int, float}
                    and math.isfinite(duration)
                    and duration >= 0
                ):
                    result.duration_seconds = float(duration)
            return result
        except Exception:
            return _fail(
                "ARK_QUERY_UNKNOWN",
                acceptance="accepted",
                task_id=task_id,
                status=VideoGenStatus.PROCESSING,
                **meta,
            )

    async def _download(self, url: str, output_path: str) -> float:
        destination = Path(output_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{destination.name}.", suffix=".part", dir=destination.parent
        )
        temporary = Path(temp_name)
        try:
            with os.fdopen(fd, "wb") as stream:
                for _ in range(4):
                    address = await _public_address(url, https_only=True)
                    original = httpx.URL(url)
                    pinned = original.copy_with(host=address)
                    hostname = original.raw_host.decode("ascii")
                    # Connect only to the verified IP, with original TLS identity.
                    # Each hop has its own pool so shared CDN IPs cannot reuse a
                    # connection authenticated for another hostname.
                    async with self._client(authenticated=False) as client:
                        async with client.stream(
                            "GET",
                            pinned,
                            headers={"Host": original.netloc.decode("ascii")},
                            extensions={"sni_hostname": hostname},
                        ) as response:
                            if response.is_redirect:
                                url = urljoin(url, response.headers["location"])
                                continue
                            if response.status_code != 200:
                                raise ValueError
                            size = 0
                            async for chunk in response.aiter_bytes(1024 * 1024):
                                size += len(chunk)
                                if size > 1024 * 1024 * 1024:
                                    raise ValueError
                                stream.write(chunk)
                            if not size:
                                raise ValueError
                            break
                else:
                    raise ValueError
                stream.flush()
                os.fsync(stream.fileno())
            info = await asyncio.to_thread(_inspect_video, temporary)
            os.replace(temporary, destination)
            return info["duration"]
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _notify(callback: Callable | None, value: Any) -> None:
        if callback is not None:
            try:
                callback(value)
            except Exception:
                pass  # UI callbacks never alter paid-operation state or leak their exceptions.

    async def generate(
        self,
        image_path: str | None,
        prompt: str,
        output_path: str,
        aspect_ratio: str = "adaptive",
        duration: float = 5.0,
        poll_interval: float = 5.0,
        max_polls: int = 240,
        on_log: Callable | None = None,
        on_progress: Callable | None = None,
        last_frame_path: str | None = None,
        references: Any = None,
        gen_mode: str | None = None,
        run_id: str | None = None,
        **kwargs,
    ) -> ArkVideoGenResult:
        """Claim stable output receipt, submit once, poll, verify, atomic delivery."""
        record = None
        accepted = None
        try:
            self._guard(kwargs.get("egress_context"))
            if (
                type(max_polls) is not int
                or max_polls < 1
                or not isinstance(poll_interval, (int, float))
                or not math.isfinite(poll_interval)
                or poll_interval < 0
                or not isinstance(output_path, (str, Path))
                or not str(output_path)
                or run_id is not None
                and not isinstance(run_id, str)
            ):
                raise ArkDualReferenceError("ARK_INPUT_INVALID")
            mode = (
                gen_mode
                or kwargs.get("video_mode")
                or kwargs.get("mode")
                or "video_edit"
            )
            if mode == "video_edit" and aspect_ratio == "auto":
                aspect_ratio = "adaptive"
            payload, digest, local_videos = await self._prepare(
                image_path,
                prompt,
                references,
                mode,
                aspect_ratio,
                duration,
                last_frame_path,
            )
            try:
                record, fresh, conflict = await asyncio.to_thread(
                    self._claim,
                    str(output_path),
                    digest,
                    run_id or "direct",
                )
            except Exception:
                return _fail("ARK_STATE_UNAVAILABLE", acceptance="outcome_unknown")
            if conflict:
                return _fail(
                    "ARK_INPUT_CONFLICT",
                    acceptance=record["acceptance"],
                    task_id=record.get("task_id"),
                )
            if fresh:
                try:
                    for index, path in local_videos.items():
                        payload["content"][index]["video_url"][
                            "url"
                        ] = await self._relay_video(path)
                    if len(json.dumps(payload).encode()) > 64 * 1024 * 1024:
                        raise ArkDualReferenceError("ARK_INPUT_INVALID")
                except ArkDualReferenceError as exc:
                    result = _fail(exc.code)
                    await asyncio.to_thread(
                        self._save, str(output_path), record, result, "not_accepted"
                    )
                    return result
                self._notify(on_log, "正在提交一个火山方舟生成任务，不会自动重复提交。")
                accepted = await self._submit(payload)
                record = await asyncio.to_thread(
                    self._save,
                    str(output_path),
                    record,
                    accepted,
                    "accepted"
                    if accepted.acceptance == "accepted"
                    else "not_accepted"
                    if accepted.acceptance == "not_accepted"
                    else "submitting",
                )
                if accepted.acceptance != "accepted":
                    return accepted
            elif not record.get("task_id"):
                return self._cached(record)
            elif record["phase"] in {"failed", "expired", "cancelled"}:
                return self._cached(record)
            task_id = record["task_id"]
            self._notify(on_log, "正在查询已受理的原任务，完成后下载并核验成片。")
            for index in range(max_polls):
                result = await self.get_result(task_id)
                # Preserve the submission request ID even when a poll has none.
                result.request_id = result.request_id or record.get("request_id", "")
                if result.status == VideoGenStatus.FAILED:
                    phase = {
                        "ARK_TASK_EXPIRED": "expired",
                        "ARK_TASK_CANCELLED": "cancelled",
                    }.get(
                        result.error_code,
                        "failed",
                    )
                    await asyncio.to_thread(
                        self._save, str(output_path), record, result, phase
                    )
                    return result
                if result.status == VideoGenStatus.DONE:
                    record = await asyncio.to_thread(
                        self._save,
                        str(output_path),
                        record,
                        result,
                        "delivery_pending",
                    )
                    try:
                        result.duration_seconds = await self._download(
                            result.video_url, str(output_path)
                        )
                    except Exception:
                        return _fail(
                            "ARK_DELIVERY_FAILED",
                            acceptance="accepted",
                            task_id=task_id,
                            request_id=result.request_id,
                            usage=result.usage,
                        )
                    result.video_path = str(output_path)
                    await asyncio.to_thread(
                        self._save, str(output_path), record, result, "succeeded"
                    )
                    self._notify(on_progress, 1.0)
                    return result
                if result.error_code:
                    return result  # Query errors preserve the ID; never loop into submission.
                self._notify(on_progress, min(0.9, 0.2 + 0.7 * index / max_polls))
                if index + 1 < max_polls:
                    await asyncio.sleep(poll_interval)
            return _fail(
                "ARK_POLL_TIMEOUT",
                acceptance="accepted",
                task_id=task_id,
                status=VideoGenStatus.PROCESSING,
                request_id=record.get("request_id", ""),
            )
        except ArkDualReferenceError as exc:
            return _fail(
                exc.code,
                acceptance=record["acceptance"] if record else "not_accepted",
                task_id=record.get("task_id") if record else None,
            )
        except Exception:
            # A receipt write may fail AFTER a successful POST. Surface the in-memory
            # task ID, while the pre-POST receipt still forbids any repeat submission.
            task_id = (
                accepted.task_id
                if accepted is not None
                else record.get("task_id")
                if record
                else None
            )
            return _fail(
                "ARK_STATE_UNAVAILABLE",
                task_id=task_id,
                acceptance="accepted" if task_id else "outcome_unknown",
            )


__all__ = [
    "ARK_BASE_URL",
    "BACKEND",
    "DEFAULT_MODEL",
    "SUPPORTED_MODELS",
    "ArkAuthorizationResult",
    "ArkDualReferenceError",
    "ArkDualReferenceVideoGenerator",
    "ArkVideoGenResult",
]
