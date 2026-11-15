import base64
import io
import json
import shutil
import subprocess
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from PIL import Image

from novelvideo.doubao_work.client import GatewayError, NativeClient, completed_reply
from novelvideo.doubao_work.jobs import JobStore, validate_reference


def test_only_matching_completed_reply():
    messages = [
        {"role": "assistant", "reply_id": "other", "status": "completed"},
        {"role": "assistant", "reply_id": "q", "status": "running"},
        {"role": "assistant", "reply_id": "q", "session_id": "s", "status": "completed"},
    ]
    assert completed_reply(messages, "s", "q") == messages[2]
    assert completed_reply(messages, "wrong", "q") is None


class FakeClient:
    def __init__(self):
        self.posts = []
        self.fail = False

    def call(self, method, path, payload=None):
        if method == "POST":
            self.posts.append(path)
            if self.fail:
                raise GatewayError("REQUEST_OUTCOME_UNKNOWN", True)
            if path == "/v1/sessions":
                return {"id": "s"}
            return {"messages": [{"id": "q"}]}
        return {"messages": [
            {"role": "assistant", "reply_id": "q", "session_id": "s",
             "status": "completed", "items": [{"type": "text", "text": "成功"}]}
        ]}


def test_idempotence_conflict_and_restart(tmp_path):
    client = FakeClient()
    store = JobStore(tmp_path / "state", tmp_path / "out", client)
    identifier = str(uuid4())
    job, fresh = store.create(identifier, "text", "测试", [])
    assert fresh
    assert store.create(identifier, "text", "测试", [])[1] is False
    with pytest.raises(ValueError, match="UUID_CONFLICT"):
        store.create(identifier, "text", "另一条", [])
    store.submit(job["id"])
    store.submit(job["id"])
    assert len(client.posts) == 2
    restored = JobStore(tmp_path / "state", tmp_path / "out", client)
    result = restored.refresh(identifier)
    assert result["status"] == "completed"
    assert result["text"] == "成功"
    assert len(client.posts) == 2
    assert (tmp_path / "state" / f"{identifier}.json").stat().st_mode & 0o777 == 0o600


def test_unknown_outcome_is_never_resubmitted(tmp_path):
    client = FakeClient()
    client.fail = True
    store = JobStore(tmp_path / "state", tmp_path / "out", client)
    job, _ = store.create(str(uuid4()), "text", "test", [])
    store.submit(job["id"])
    restored = JobStore(tmp_path / "state", tmp_path / "out", client)
    restored.submit(job["id"])
    assert len(client.posts) == 1
    assert restored.read(job["id"])["status"] == "outcome_unknown"


@pytest.mark.parametrize("case", ["exact", "ambiguous", "paged", "mismatch", "missing_refs"])
def test_unknown_event_recovers_only_unique_exact_receipt(tmp_path, case):
    client = FakeClient()
    store = JobStore(tmp_path / "state", tmp_path / "out", client)
    job, _ = store.create(str(uuid4()), "text", "test", [])
    saved = store.read(job["id"])
    saved.update(status="outcome_unknown", session_id="s",
                 submission_stage="event_pending", error="UPSTREAM_TRANSPORT_ERROR")
    if case == "missing_refs":
        saved["refs"] = [{"name": "reference.png"}]
    store.save(saved)
    user = {"role": "user", "session_id": "s", "id": "q",
            "items": [{"type": "text", "text": "wrong" if case == "mismatch" else "test"}]}
    calls = []
    def call(method, path, payload=None):
        calls.append(method)
        return {"has_more": case == "paged", "messages": [user] * (2 if case == "ambiguous" else 1) + [
            {"role": "assistant", "session_id": "s", "reply_id": "q",
             "status": "completed", "items": [{"type": "text", "text": "done"}]}]}
    client.call = call
    result = store.refresh(job["id"])
    assert result["status"] == ("completed" if case == "exact" else "outcome_unknown")
    assert calls == ["GET"]
    store.submit(job["id"])
    assert calls == ["GET"]


def test_upstream_transport_post_remains_unknown_without_flag():
    client = NativeClient(key_provider=lambda: "PRIVATE", transport=httpx.MockTransport(
        lambda request: httpx.Response(502, json={"error": {"code": "UPSTREAM_TRANSPORT_ERROR"}})
    ))
    with pytest.raises(GatewayError) as error:
        client.call("POST", "/v1/sessions/s/events", {})
    assert error.value.unknown


def test_crash_reservation_never_reposts(tmp_path):
    client = FakeClient()
    store = JobStore(tmp_path / "state", tmp_path / "out", client)
    job, _ = store.create(str(uuid4()), "text", "test", [])
    job["status"] = "event_pending"
    store.save(job)
    restored = JobStore(tmp_path / "state", tmp_path / "out", client)
    restored.submit(job["id"])
    assert not client.posts
    assert restored.read(job["id"])["status"] == "outcome_unknown"


def test_reference_requires_real_decodable_image():
    with pytest.raises(ValueError):
        validate_reference({"name": "fake.png", "contentBase64": base64.b64encode(b"fake").decode()})
    data = io.BytesIO()
    Image.new("RGB", (8, 8)).save(data, format="PNG")
    assert validate_reference({"name": "x.png", "contentBase64": base64.b64encode(data.getvalue()).decode()})[1] == data.getvalue()


def test_native_error_never_exposes_upstream_secret():
    transport = httpx.MockTransport(lambda req: httpx.Response(
        500, json={"error": {"code": "UPSTREAM_500", "message": "SECRET", "outcomeUnknown": True}}
    ))
    client = NativeClient(key_provider=lambda: "PRIVATE", transport=transport)
    with pytest.raises(GatewayError) as error:
        client.call("POST", "/v1/sessions", {})
    assert "SECRET" not in str(error.value)
    assert error.value.unknown


@pytest.mark.parametrize("failure,unknown", [
    (httpx.ConnectError, False),
    (httpx.ConnectTimeout, False),
    (httpx.ReadTimeout, True),
    (httpx.WriteTimeout, True),
])
def test_transport_failure_preserves_submission_certainty(tmp_path, failure, unknown):
    requests = []

    def fail(request):
        requests.append(request)
        raise failure("PRIVATE", request=request)

    client = NativeClient(key_provider=lambda: "PRIVATE", transport=httpx.MockTransport(fail))
    store = JobStore(tmp_path / "state", tmp_path / "out", client)
    job, _ = store.create(str(uuid4()), "video", "测试视频", [])
    store.submit(job["id"])
    result = store.refresh(job["id"])
    assert result["status"] == ("outcome_unknown" if unknown else "failed")
    assert result["error"] == "GATEWAY_UNAVAILABLE"
    assert "PRIVATE" not in (tmp_path / "state" / f"{job['id']}.json").read_text()

    store.submit(job["id"])
    assert len(requests) == 1


def test_text_link_is_not_image_success(tmp_path):
    store = JobStore(tmp_path / "state", tmp_path / "out", FakeClient())
    job, _ = store.create(str(uuid4()), "image", "make image", [])
    store.submit(job["id"])
    result = store.refresh(job["id"])
    assert result["status"] == "failed"
    assert result["error"] == "NO_IMAGE_ARTIFACT"
    assert json.loads((tmp_path / "state" / f"{job['id']}.json").read_text())["files"] == []


@pytest.mark.parametrize("legacy_failure", [False, True])
def test_official_video_link_delivers_without_resubmission(tmp_path, monkeypatch, legacy_failure):
    from novelvideo.doubao_work import jobs

    fixture = tmp_path / "fixture.mp4"
    ffmpeg = shutil.which("ffmpeg") or str(Path.home() / ".local/bin/ffmpeg")
    subprocess.run([
        ffmpeg, "-v", "error", "-f", "lavfi", "-i", "color=size=16x16:rate=5",
        "-t", "0.2", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(fixture),
    ], check=True, capture_output=True, timeout=30)
    client = FakeClient()
    original = client.call

    def call(method, path, payload=None):
        if method == "POST":
            return original(method, path, payload)
        return {"messages": [{
            "role": "assistant", "reply_id": "q", "session_id": "s", "status": "completed",
            "items": [{"type": "artifact", "artifact_type": "link", "state": "active",
                       "link": {"url": "https://aka.doubaocdn.com/file?signature=PRIVATE"}}],
        }]}

    client.call = call
    monkeypatch.setattr(jobs, "download", lambda url, path: shutil.copyfile(fixture, path))
    store = JobStore(tmp_path / "state", tmp_path / "out", client)
    job, _ = store.create(str(uuid4()), "video", "生成视频", [])
    store.submit(job["id"])
    if legacy_failure:
        saved = store.read(job["id"])
        saved.update(status="failed", error="NO_VIDEO_ARTIFACT")
        store.save(saved)
    result = store.refresh(job["id"])
    assert result["status"] == "completed"
    assert result["files"][0]["width"] == 16
    assert result["files"][0]["duration"] > 0
    store.submit(job["id"])
    assert len(client.posts) == 2
    assert "PRIVATE" not in (tmp_path / "state" / f"{job['id']}.json").read_text()


@pytest.mark.parametrize("item", [
    {"type": "text", "text": "https://aka.doubaocdn.com/file"},
    {"type": "artifact", "artifact_type": "link", "state": "active",
     "link": {"url": "https://example.com/file"}},
    {"type": "artifact", "artifact_type": "link", "state": "active",
     "link": {"url": "https://aka.doubaocdn.com.evil.example/file"}},
    {"type": "artifact", "artifact_type": "link", "state": "deleted",
     "link": {"url": "https://aka.doubaocdn.com/file"}},
])
def test_unrelated_video_links_are_not_downloaded(tmp_path, monkeypatch, item):
    from novelvideo.doubao_work import jobs

    client = FakeClient()
    original = client.call
    client.call = lambda method, path, payload=None: (
        original(method, path, payload) if method == "POST" else {"messages": [{
            "role": "assistant", "reply_id": "q", "session_id": "s", "status": "completed",
            "items": [item],
        }]}
    )
    downloads = []
    monkeypatch.setattr(jobs, "download", lambda url, path: downloads.append(url))
    store = JobStore(tmp_path / "state", tmp_path / "out", client)
    job, _ = store.create(str(uuid4()), "video", "生成视频", [])
    store.submit(job["id"])
    assert store.refresh(job["id"])["status"] == "failed"
    assert not downloads


def test_official_link_requires_real_video_bytes(tmp_path, monkeypatch):
    from novelvideo.doubao_work import jobs

    client = FakeClient()
    original = client.call
    client.call = lambda method, path, payload=None: (
        original(method, path, payload) if method == "POST" else {"messages": [{
            "role": "assistant", "reply_id": "q", "session_id": "s", "status": "completed",
            "items": [{"type": "artifact", "artifact_type": "link", "state": "active",
                       "link": {"url": "https://aka.doubaocdn.com/file"}}],
        }]}
    )
    monkeypatch.setattr(jobs, "download", lambda url, path: path.write_bytes(b"<html>not video</html>"))
    store = JobStore(tmp_path / "state", tmp_path / "out", client)
    job, _ = store.create(str(uuid4()), "video", "生成视频", [])
    store.submit(job["id"])
    result = store.refresh(job["id"])
    assert result["status"] == "delivery_pending"
    assert result["files"] == []
    assert result["error"] == "QUERY_OR_DELIVERY_FAILED"
    assert len(client.posts) == 2


def test_failed_download_recovers_without_generation(tmp_path, monkeypatch):
    from novelvideo.doubao_work import jobs

    client = FakeClient()
    original = client.call

    def call(method, path, payload=None):
        if method == "POST":
            return original(method, path, payload)
        return {"messages": [{
            "role": "assistant", "reply_id": "q", "session_id": "s", "status": "completed",
            "items": [{"type": "artifact", "artifact_type": "image", "state": "active",
                       "image": {"url": "https://example.com/result?signature=PRIVATE"}}],
        }]}

    client.call = call
    attempts = []

    def download(url, path):
        attempts.append(url)
        if len(attempts) == 1:
            raise RuntimeError("PRIVATE")
        Image.new("RGB", (16, 12)).save(path, format="PNG")

    monkeypatch.setattr(jobs, "download", download)
    store = JobStore(tmp_path / "state", tmp_path / "out", client)
    job, _ = store.create(str(uuid4()), "image", "make image", [])
    store.submit(job["id"])
    assert store.refresh(job["id"])["status"] == "delivery_pending"
    result = store.refresh(job["id"])
    assert result["status"] == "completed"
    assert result["files"][0]["width"] == 16
    assert result["files"][0]["name"].endswith(".png")
    assert len(client.posts) == 2
    assert "PRIVATE" not in (tmp_path / "state" / f"{job['id']}.json").read_text()


def test_artifact_download_carries_no_credentials(tmp_path, monkeypatch):
    from novelvideo.doubao_work import jobs

    requests = []
    original_client = httpx.Client
    transport = httpx.MockTransport(lambda request: (
        requests.append(request) or httpx.Response(200, content=b"media")
    ))
    monkeypatch.setattr(jobs, "validate_public_url", lambda url: None)
    monkeypatch.setattr(jobs.httpx, "Client", lambda **kwargs: original_client(transport=transport, **kwargs))
    jobs.download("https://example.com/image", tmp_path / "image")
    assert requests
    assert "authorization" not in requests[0].headers


def test_local_ce_guard_rejects_foreign_origin(monkeypatch):
    from fastapi import HTTPException
    from starlette.requests import Request
    from novelvideo.api.routes.doubao_work import local_only
    from novelvideo.shared import runtime_env

    monkeypatch.setattr(runtime_env, "is_ce_effective", lambda: True)
    request = Request({
        "type": "http", "client": ("127.0.0.1", 12345),
        "headers": [(b"host", b"127.0.0.1:8780"), (b"origin", b"https://evil.example")],
    })
    with pytest.raises(HTTPException) as error:
        local_only(request)
    assert error.value.status_code == 403


@pytest.mark.parametrize("port", [18780, 18879])
def test_local_ce_guard_accepts_bound_desktop_port(monkeypatch, port):
    from starlette.requests import Request
    from novelvideo.api.routes.doubao_work import local_only
    from novelvideo.shared import runtime_env

    monkeypatch.setattr(runtime_env, "is_ce_effective", lambda: True)
    monkeypatch.setenv("NOVELVIDEO_API_PORT", str(port))
    request = Request({
        "type": "http", "client": ("127.0.0.1", 12345),
        "headers": [
            (b"host", f"127.0.0.1:{port}".encode()),
            (b"origin", f"http://127.0.0.1:{port}".encode()),
        ],
    })
    local_only(request)


@pytest.mark.parametrize("peer,host,origin", [
    ("127.0.0.1", "127.0.0.1:18780", "http://127.0.0.1:18880"),
    ("127.0.0.1", "evil.example:18780", "http://127.0.0.1:18780"),
    ("192.0.2.1", "127.0.0.1:18780", "http://127.0.0.1:18780"),
])
def test_desktop_port_keeps_loopback_boundary(monkeypatch, peer, host, origin):
    from fastapi import HTTPException
    from starlette.requests import Request
    from novelvideo.api.routes.doubao_work import local_only
    from novelvideo.shared import runtime_env

    monkeypatch.setattr(runtime_env, "is_ce_effective", lambda: True)
    monkeypatch.setenv("NOVELVIDEO_API_PORT", "18780")
    request = Request({
        "type": "http", "client": (peer, 12345),
        "headers": [(b"host", host.encode()), (b"origin", origin.encode())],
    })
    with pytest.raises(HTTPException) as error:
        local_only(request)
    assert error.value.status_code == 403
