"""Native loopback gateway; credentials never leave the backend."""

import hashlib
import os
import re
import subprocess
from collections.abc import Callable
from pathlib import Path
from urllib.parse import quote

import httpx

GATEWAY = "http://127.0.0.1:8791"
_BUNDLED_GATEWAY = Path(__file__).resolve().parents[3] / "extras" / "gateway"
_LOCAL_GATEWAY = Path.home() / "Desktop" / "电商工作台" / "doubao-work-api"
GATEWAY_PROJECT = str(Path(os.environ.get(
    "QJIN_DOUBAO_GATEWAY_PROJECT",
    str(_BUNDLED_GATEWAY if _BUNDLED_GATEWAY.is_dir() else _LOCAL_GATEWAY),
)).expanduser().resolve())


class GatewayError(Exception):
    def __init__(self, code: str, unknown: bool = False):
        self.code = code if re.fullmatch(r"[A-Z0-9_]{1,80}", code) else "GATEWAY_ERROR"
        self.unknown = unknown
        super().__init__(self.code)


def keychain_key() -> str:
    fingerprint = hashlib.sha256(GATEWAY_PROJECT.encode()).hexdigest()[:16]
    result = subprocess.run(
        ["/usr/bin/security", "find-generic-password", "-a", "gateway-key",
         "-s", f"doubao-work-local-api.{fingerprint}", "-w"],
        capture_output=True, text=True, timeout=10, check=False,
    )
    if result.returncode or not result.stdout.strip():
        raise GatewayError("LOCAL_KEY_UNAVAILABLE")
    return result.stdout.strip()


def session_path(session_id: str) -> str:
    return f"/v1/sessions/{quote(session_id, safe='')}"


def completed_reply(messages: list, session_id: str, message_id: str) -> dict | None:
    for message in messages:
        if (message.get("role") == "assistant"
                and message.get("session_id") == session_id
                and message.get("reply_id") == message_id
                and message.get("status") == "completed"):
            return message
    return None


class NativeClient:
    def __init__(
        self, key_provider: Callable[[], str] = keychain_key,
        transport: httpx.BaseTransport | None = None,
    ):
        self.key_provider = key_provider
        self.transport = transport

    def health(self) -> dict:
        try:
            with httpx.Client(trust_env=False, timeout=8) as client:
                data = client.get(GATEWAY + "/healthz").json()
            return {"reachable": True, "connected": bool(data.get("connected"))}
        except Exception:
            return {"reachable": False, "connected": False}

    def call(self, method: str, path: str, payload: dict | None = None) -> dict:
        if not (path == "/v1/sessions" or path == "/v1/files"
                or re.fullmatch(r"/v1/sessions/[A-Za-z0-9%_-]+/(events|messages)", path)):
            raise GatewayError("INVALID_GATEWAY_PATH")
        key = self.key_provider()
        try:
            with httpx.Client(
                trust_env=False, timeout=90, transport=self.transport,
                headers={"Authorization": f"Bearer {key}"},
            ) as client:
                response = client.request(method, GATEWAY + path, json=payload)
                body = response.json()
        except (httpx.ConnectError, httpx.ConnectTimeout):
            # No connection was established, so the gateway received no request.
            raise GatewayError("GATEWAY_UNAVAILABLE") from None
        except Exception:
            raise GatewayError("GATEWAY_UNAVAILABLE", method == "POST") from None
        if response.is_error or body.get("error"):
            error = body.get("error") or {}
            raise GatewayError(
                str(error.get("code", "GATEWAY_ERROR")),
                bool(error.get("outcomeUnknown")) or (
                    method == "POST" and error.get("code") in {
                        "UPSTREAM_TRANSPORT_ERROR", "REQUEST_OUTCOME_UNKNOWN",
                    }
                ),
            )
        if not isinstance(body.get("data"), dict):
            raise GatewayError("INVALID_GATEWAY_RESPONSE", method == "POST")
        return body["data"]
