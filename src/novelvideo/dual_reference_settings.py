"""Private local authorization for the Ark dual-reference channel."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

DEFAULT_BASE_URL = "https://ark.cn-beijing.volces.com/api/v3"
DEFAULT_MODEL = "doubao-seedance-2-5-260628"
CHANNEL_ID = "ark_dual_reference"
_LOCK = threading.RLock()
_CONFIRMED_MODELS = {
    "doubao-seedance-2-5-260628",
    "doubao-seedance-2-0-260128",
    "doubao-seedance-2-0-fast-260128",
}


def _path() -> Path:
    from novelvideo import config

    return Path(config.STATE_DIR) / "local" / "dual-reference" / "config.json"


def normalize_base_url(value: str) -> str:
    base = value.strip().rstrip("/")
    parsed = urlsplit(base)
    if (parsed.scheme != "https" or parsed.hostname != "ark.cn-beijing.volces.com"
            or parsed.port not in {None, 443} or parsed.path != "/api/v3"
            or parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise ValueError("服务地址须为火山方舟北京区官方 HTTPS 接口")
    return base


def read_config() -> dict[str, Any]:
    with _LOCK:
        try:
            data = json.loads(_path().read_text())
            if not isinstance(data, dict):
                raise ValueError("invalid config")
        except (FileNotFoundError, ValueError):
            data = {}
    return {
        "provider": "ark",
        "baseUrl": data.get("baseUrl") or DEFAULT_BASE_URL,
        "model": data.get("model", DEFAULT_MODEL),
        "apiKey": data.get("apiKey") or os.environ.get("ARK_API_KEY", ""),
        "verification": data.get("verification") or {},
    }


def _identity(data: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(
        [data["baseUrl"], data["model"], data["apiKey"]],
        separators=(",", ":"),
    ).encode()).hexdigest()


def _write(data: dict[str, Any]) -> None:
    path = _path()
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    path.parent.chmod(0o700)
    fd, temporary = tempfile.mkstemp(prefix=".config-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as file:
            json.dump(data, file, ensure_ascii=False)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def save_config(
    *, api_key: str | None = None, base_url: str | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    with _LOCK:
        data = read_config()
        data["baseUrl"] = normalize_base_url(base_url or data["baseUrl"])
        if model is not None:
            if len(model) > 200 or any(c.isspace() for c in model):
                raise ValueError("模型标识格式无效")
            data["model"] = model.strip()
        if api_key is not None:
            if len(api_key) > 4096 or any(c.isspace() for c in api_key.strip()):
                raise ValueError("调用密钥格式无效")
            data["apiKey"] = api_key.strip()
        data["verification"] = {}
        _write(data)
        return public_status()


def public_status() -> dict[str, Any]:
    data = read_config()
    verified = data["verification"]
    connected = bool(
        data["apiKey"] and verified.get("connected")
        and verified.get("identity") == _identity(data)
    )
    return {
        "provider": "ark", "baseUrl": data["baseUrl"], "model": data["model"],
        "configured": bool(data["apiKey"] and data["model"]),
        "connected": connected,
        "supportsReferenceVideo": connected,
        "supportsReferenceImage": connected,
        "verificationLevel": "credentials_and_model_catalog" if connected else "none",
        "generationVerified": False,
        "error": verified.get("error", "") if not connected else "",
        "authorizationUrl": "https://console.volcengine.com/ark/region:cn-beijing/apiKey",
        "capabilitySource": "https://www.volcengine.com/docs/82379/1520757",
    }


async def verify_config(*, client: httpx.AsyncClient | None = None) -> dict[str, Any]:
    data = read_config()
    identity = _identity(data)
    error = ""
    connected = False
    if not data["apiKey"]:
        error = "待授权：请在火山方舟控制台获取调用密钥，并在此页保存"
    elif data["model"] not in _CONFIRMED_MODELS:
        error = "尚未确认该模型支持视频和图片双参考，请填写方舟 Seedance 2.0 或 2.5 模型标识"
    else:
        own_client = client is None
        client = client or httpx.AsyncClient(timeout=20, follow_redirects=False)
        try:
            normalize_base_url(data["baseUrl"])
            response = await client.get(
                data["baseUrl"] + "/models",
                headers={"Authorization": "Bearer " + data["apiKey"]},
            )
            if response.status_code in {401, 403}:
                error = "授权未通过：请核对调用密钥及模型权限"
            elif response.status_code != 200:
                error = f"服务校验失败（状态码 {response.status_code}）"
            else:
                items = response.json().get("data", [])
                connected = any(
                    isinstance(item, dict) and item.get("id") == data["model"]
                    for item in items
                )
                if not connected:
                    error = "调用密钥可访问模型目录，但目录中没有所选模型；请核对模型开通状态"
        except (httpx.HTTPError, ValueError, AttributeError, TypeError):
            error = "服务连接或响应校验失败，请检查网络及官方服务地址"
        finally:
            if own_client:
                await client.aclose()
    with _LOCK:
        current = read_config()
        if _identity(current) == identity:
            current["verification"] = {
                "identity": identity, "connected": connected, "error": error,
            }
            _write(current)
    return public_status()


def catalog_entry() -> dict[str, Any] | None:
    status = public_status()
    if not status["connected"]:
        return None
    is_25 = status["model"].startswith("doubao-seedance-2-5-")
    return {
        "id": CHANNEL_ID, "providerId": "ark", "provider": "ark",
        "apiModel": CHANNEL_ID, "api_model": CHANNEL_ID,
        "label": "火山方舟 · 视频与商品图双参考",
        "upstreamModel": status["model"],
        "supportedModes": ["all_reference", "video_edit"] if is_25 else ["all_reference"],
        "referenceImageMax": 30 if is_25 else 9,
        "referenceVideoMax": 10 if is_25 else 3, "referenceAudioMax": 0,
        "supportsGenerateAudio": True,
        "resolutionOptions": ["720p"],
        "ratioOptions": ["16:9", "9:16", "1:1", "4:3", "3:4"],
        "minDuration": 4, "maxDuration": 30 if is_25 else 15,
        "referenceVideoMinSeconds": 4 if is_25 else 2,
        "referenceVideoMaxSeconds": 30 if is_25 else 15,
        "referenceVideoTotalMaxSeconds": 30 if is_25 else 15,
        "verificationLevel": status["verificationLevel"],
        "generationVerified": False,
    }


def require_authorized_config() -> dict[str, Any]:
    if not public_status()["connected"]:
        raise ValueError("双参考服务尚未授权或所选模型尚未通过校验")
    return read_config()
