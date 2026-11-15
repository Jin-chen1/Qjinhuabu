"""Offline protocol and lifecycle contracts for the single-purpose MP4 relay."""

import asyncio
import importlib
import io
import json
import os
import signal
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import pytest

MODULE = "novelvideo.dual_reference_media_relay"
ORIGIN = "https://offline-reference-test.trycloudflare.com"
MP4 = b"\x00\x00\x00\x18ftypisom\x00\x00\x00\x00isommp42" + bytes(range(256)) * 4
READY_LOG = f"INF Quick tunnel created: {ORIGIN}\nINF Registered tunnel connection\n"


@pytest.fixture
def module():
    assert importlib.util.find_spec(MODULE) is not None, "MP4 relay is not implemented"
    return importlib.import_module(MODULE)


class FakeTunnel:
    def __init__(self, output=READY_LOG):
        self.stdout = io.StringIO(output)
        self.stdin = io.StringIO()
        self.returncode = None
        self.terminated = False
        self.killed = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = 0

    def kill(self):
        self.killed = True
        self.returncode = -9

    def wait(self, timeout=None):
        if self.returncode is None:
            raise subprocess.TimeoutExpired("offline", timeout)
        return self.returncode


@pytest.fixture
def relay(module, monkeypatch):
    process = FakeTunnel()
    monkeypatch.setattr(
        module.DualReferenceMediaRelay, "_spawn_tunnel", lambda _: process
    )
    instance = module.DualReferenceMediaRelay(port=0, start_timeout=0.15)
    instance.start()
    try:
        yield instance
    finally:
        instance.close()
    assert process.stdin.closed
    assert process.poll() is not None


@pytest.fixture
def video(tmp_path):
    path = tmp_path / "private-reference.mp4"
    path.write_bytes(MP4)
    return path


def request(relay, path, method="GET", **kwargs):
    with httpx.Client(trust_env=False, timeout=2) as client:
        return client.request(method, relay.local_origin + path, **kwargs)


def route(url):
    return urlsplit(url).path


def test_missing_implementation_contract():
    assert importlib.util.find_spec(MODULE) is not None, "MP4 relay is not implemented"
    module = importlib.import_module(MODULE)
    assert callable(module.ensure_public_video)
    assert callable(module.start)
    assert callable(module.status)
    assert callable(module.close)


@pytest.mark.parametrize(
    "code",
    [
        "RELAY_PORT_INVALID",
        "RELAY_CONFIG_INVALID",
        "RELAY_BIND_FAILED",
        "RELAY_TUNNEL_START_FAILED",
        "RELAY_TUNNEL_TIMEOUT",
        "RELAY_TUNNEL_EXITED",
        "RELAY_NOT_READY",
        "RELAY_VIDEO_INVALID",
    ],
)
def test_fixed_error_messages_are_chinese_and_keep_stable_codes(module, code):
    error = module.MediaRelayError(code)
    assert error.code == code
    message = str(error)
    assert message == module._ERROR_MESSAGES[code]
    assert any("\u4e00" <= char <= "\u9fff" for char in message)
    assert "\n" not in message
    assert "http://" not in message and "https://" not in message


def test_default_port_and_ttl(module):
    instance = module.DualReferenceMediaRelay()
    assert instance.port == 8794
    assert instance.ttl_seconds == 3600
    instance.close()


def test_register_returns_random_https_token_without_filename(relay, video):
    url = relay.register(video)
    assert url.startswith(ORIGIN + "/")
    assert len(route(url).removeprefix("/")) >= 43
    assert video.name not in url
    assert str(video) not in repr(relay)
    assert url not in repr(relay)
    assert request(relay, route(url)).content == MP4


def test_get_and_head_headers(relay, video):
    path = route(relay.register(video))
    get = request(relay, path)
    head = request(relay, path, "HEAD")
    assert get.status_code == head.status_code == 200
    assert head.content == b""
    for response in (get, head):
        assert response.headers["content-type"] == "video/mp4"
        assert response.headers["content-length"] == str(len(MP4))
        assert response.headers["accept-ranges"] == "bytes"
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["x-content-type-options"] == "nosniff"
        assert "content-disposition" not in response.headers


@pytest.mark.parametrize(
    "header,start,end",
    [
        ("bytes=0-0", 0, 0),
        ("bytes=12-47", 12, 47),
        ("bytes=10-", 10, len(MP4) - 1),
        ("bytes=-8", len(MP4) - 8, len(MP4) - 1),
        ("bytes=-999999", 0, len(MP4) - 1),
        ("bytes=0-999999", 0, len(MP4) - 1),
    ],
)
def test_ranges(relay, video, header, start, end):
    path = route(relay.register(video))
    response = request(relay, path, headers={"Range": header})
    assert response.status_code == 206
    assert response.content == MP4[start : end + 1]
    assert response.headers["content-range"] == f"bytes {start}-{end}/{len(MP4)}"
    assert int(response.headers["content-length"]) == end - start + 1


def test_head_ignores_range_as_required_for_non_get(relay, video):
    response = request(
        relay, route(relay.register(video)), "HEAD", headers={"Range": "bytes=3-7"}
    )
    assert response.status_code == 200
    assert int(response.headers["content-length"]) == len(MP4)
    assert response.content == b""


@pytest.mark.parametrize("header", ["bytes=999999-", "bytes=7-3", "bytes=-0"])
def test_unsatisfiable_range(relay, video, header):
    response = request(relay, route(relay.register(video)), headers={"Range": header})
    assert response.status_code == 416
    assert response.headers["content-range"] == f"bytes */{len(MP4)}"


@pytest.mark.parametrize(
    "header",
    [
        "bytes=0-1,3-5",
        "bytes=bad",
        "items=0-3",
        "bytes=" + "9" * 5000 + "-",
    ],
)
def test_unsupported_or_malformed_range_is_ignored(relay, video, header):
    response = request(relay, route(relay.register(video)), headers={"Range": header})
    assert response.status_code == 200
    assert response.content == MP4


def test_if_range_without_validator_returns_full_video(relay, video):
    response = request(
        relay,
        route(relay.register(video)),
        headers={
            "Range": "bytes=1-2",
            "If-Range": '"unknown-validator"',
        },
    )
    assert response.status_code == 200
    assert response.content == MP4


@pytest.mark.parametrize(
    "path",
    [
        "/",
        "/favicon.ico",
        "/private-reference.mp4",
        "/etc/passwd",
        "/..%2fprivate-reference.mp4",
        "/%2e%2e/%2e%2e/etc/passwd",
        "/api/v1/config",
        "/?path=/etc/passwd",
    ],
)
def test_no_directory_or_arbitrary_path_mapping(relay, video, path):
    relay.register(video)
    response = request(relay, path)
    assert response.status_code == 404
    assert video.name.encode() not in response.content
    assert str(video.parent).encode() not in response.content


def test_token_route_is_exact_and_other_methods_cannot_read(relay, video):
    path = route(relay.register(video))
    for invalid in [
        path + "/",
        path + "?path=/etc/passwd",
        path.replace("/", "/%2f", 1),
    ]:
        assert request(relay, invalid).status_code == 404
    response = request(relay, path, "POST")
    assert response.status_code == 405
    assert response.headers["allow"] == "GET, HEAD"
    assert MP4 not in response.content


def test_expiry_is_410_even_if_cloudflare_would_cache(relay, video, monkeypatch):
    now = [100.0]
    monkeypatch.setattr(relay, "_clock", lambda: now[0])
    url = relay.register(video)
    now[0] += 3599
    assert request(relay, route(url)).status_code == 200
    assert relay.register(video) == url
    now[0] += 1
    for method in ["GET", "HEAD"]:
        response = request(relay, route(url), method)
        assert response.status_code == 410
        assert response.headers["cache-control"] == "no-store"
        assert MP4 not in response.content
    fresh = relay.register(video)
    assert fresh != url
    assert request(relay, route(fresh)).status_code == 200
    assert request(relay, route(url)).status_code == 410


def test_hash_reuse_only_for_explicitly_selected_material(relay, video, tmp_path):
    copy = tmp_path / "selected-copy.mp4"
    copy.write_bytes(MP4)
    unrelated = tmp_path / "not-selected.mp4"
    unrelated.write_bytes(MP4 + b"other")
    first = relay.register(video)
    assert relay.register(copy) == first
    assert relay.status()["registeredCount"] == 1
    with ThreadPoolExecutor(max_workers=4) as executor:
        assert set(executor.map(relay.register, [copy] * 8)) == {first}
    assert request(relay, "/not-selected.mp4").status_code == 404
    assert relay.status()["registeredCount"] == 1


def test_content_change_does_not_serve_unregistered_replacement(relay, video):
    old = relay.register(video)
    video.write_bytes(MP4 + b"replacement")
    assert request(relay, route(old)).status_code == 410
    new = relay.register(video)
    assert old != new
    assert request(relay, route(new)).content == MP4 + b"replacement"


def test_symlink_replacement_does_not_expose_other_file(relay, video, tmp_path):
    url = relay.register(video)
    secret = tmp_path / "do-not-expose.txt"
    secret.write_text("private test contents")
    video.unlink()
    video.symlink_to(secret)
    response = request(relay, route(url))
    assert response.status_code == 410
    assert secret.read_bytes() not in response.content


def test_registration_rejects_non_mp4_directory_missing_and_url(
    relay, tmp_path, module
):
    bad = tmp_path / "bad.mp4"
    bad.write_bytes(b"this is not an mp4")
    text = tmp_path / "file.txt"
    text.write_bytes(MP4)
    for invalid in [
        bad,
        text,
        tmp_path,
        tmp_path / "missing.mp4",
        "https://host.test/a.mp4",
    ]:
        with pytest.raises(module.MediaRelayError) as exc:
            relay.register(invalid)
        assert exc.value.code == "RELAY_VIDEO_INVALID"
        assert str(invalid) not in str(exc.value)
    assert relay.status()["registeredCount"] == 0


def test_only_loopback_and_dedicated_os_assigned_port(relay):
    parsed = urlsplit(relay.local_origin)
    assert parsed.hostname == "127.0.0.1"
    assert parsed.port not in {8780, 5173, 8791}
    assert relay.status()["ready"]


@pytest.mark.parametrize("port", [8780, 5173, 8791, -1, 65536])
def test_forbidden_or_invalid_ports_fail_before_process_spawn(module, port):
    with pytest.raises(module.MediaRelayError) as exc:
        module.DualReferenceMediaRelay(port=port)
    assert exc.value.code == "RELAY_PORT_INVALID"


def test_occupied_port_is_never_forwarded(module, monkeypatch):
    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        instance = module.DualReferenceMediaRelay(port=occupied.getsockname()[1])
        monkeypatch.setattr(
            instance,
            "_spawn_tunnel",
            lambda: pytest.fail("must not proxy existing port"),
        )
        with pytest.raises(module.MediaRelayError) as exc:
            instance.start()
        assert exc.value.code == "RELAY_BIND_FAILED"
        assert not instance.status()["ready"]
        instance.close()


def test_safe_timeout_cleans_up_own_process(module, monkeypatch):
    process = FakeTunnel("INF private-untrusted-log-with-secret\n")
    monkeypatch.setattr(
        module.DualReferenceMediaRelay, "_spawn_tunnel", lambda _: process
    )
    instance = module.DualReferenceMediaRelay(port=0, start_timeout=0.05)
    with pytest.raises(module.MediaRelayError) as exc:
        instance.start()
    assert exc.value.code == "RELAY_TUNNEL_TIMEOUT"
    assert "private-untrusted" not in str(exc.value)
    assert process.terminated
    assert not instance.status()["ready"]
    assert instance.status()["port"] is None
    instance.close()


def test_process_exit_before_connection_is_safe(module, monkeypatch):
    process = FakeTunnel("private-path-and-token-in-raw-output\n")
    process.returncode = 7
    monkeypatch.setattr(
        module.DualReferenceMediaRelay, "_spawn_tunnel", lambda _: process
    )
    instance = module.DualReferenceMediaRelay(port=0, start_timeout=0.1)
    with pytest.raises(module.MediaRelayError) as exc:
        instance.start()
    assert exc.value.code == "RELAY_TUNNEL_EXITED"
    assert "private-path" not in str(exc.value)
    assert instance.status()["port"] is None
    assert instance.status()["exitCode"] == 7
    assert process.stdin.closed


def test_spawn_error_is_sanitized_and_listener_released(module, monkeypatch):
    def fail():
        raise OSError("/private/source.mp4?token=do-not-log")

    instance = module.DualReferenceMediaRelay(port=0)
    monkeypatch.setattr(instance, "_spawn_tunnel", fail)
    with pytest.raises(module.MediaRelayError) as exc:
        instance.start()
    assert exc.value.code == "RELAY_TUNNEL_START_FAILED"
    assert "do-not-log" not in str(exc.value)
    assert instance.status()["port"] is None


def test_timeout_escalates_only_owned_process(module, monkeypatch):
    class StubbornTunnel(FakeTunnel):
        def terminate(self):
            self.terminated = True

    process = StubbornTunnel("")
    monkeypatch.setattr(
        module.DualReferenceMediaRelay, "_spawn_tunnel", lambda _: process
    )
    instance = module.DualReferenceMediaRelay(port=0, start_timeout=0.03)
    with pytest.raises(module.MediaRelayError):
        instance.start()
    assert process.terminated and process.killed
    assert instance.status()["port"] is None


def test_failed_start_can_be_retried_without_old_links(module, monkeypatch, video):
    processes = [FakeTunnel("not-connected\n"), FakeTunnel()]
    monkeypatch.setattr(
        module.DualReferenceMediaRelay, "_spawn_tunnel", lambda _: processes.pop(0)
    )
    instance = module.DualReferenceMediaRelay(port=0, start_timeout=0.03)
    try:
        with pytest.raises(module.MediaRelayError):
            instance.start()
        assert instance.start() == ORIGIN
        assert request(instance, route(instance.register(video))).content == MP4
        assert instance.status()["error"] == ""
    finally:
        instance.close()


def test_midstream_change_revokes_registered_file(relay, video):
    path = route(relay.register(video))
    status, file, _ = relay._open_registered(path)
    assert status == 200
    with file:
        video.write_bytes(MP4 + b"unregistered-change")
        assert not relay._is_active(path, file)
        assert request(relay, path).status_code == 410


def test_hash_reuse_does_not_interrupt_existing_transfer(relay, video, tmp_path):
    path = route(relay.register(video))
    copy = tmp_path / "selected-again.mp4"
    copy.write_bytes(MP4)
    _, file, _ = relay._open_registered(path)
    with file:
        assert route(relay.register(copy)) == path
        assert relay._is_active(path, file)


def test_tunnel_origin_is_validated_after_complete_log_line(module, monkeypatch):
    class SplitOutput:
        def __init__(self):
            self.chunks = iter(
                [
                    "INF https://offline.trycloudflare.com",
                    ".attacker.test\nINF Registered tunnel connection\n",
                    "",
                ]
            )

        def read(self, size):
            return next(self.chunks, "")

        def close(self):
            pass

    process = FakeTunnel()
    process.stdout = SplitOutput()
    monkeypatch.setattr(
        module.DualReferenceMediaRelay, "_spawn_tunnel", lambda _: process
    )
    instance = module.DualReferenceMediaRelay(port=0, start_timeout=0.03)
    with pytest.raises(module.MediaRelayError):
        instance.start()
    assert process.terminated


@pytest.mark.parametrize(
    "origin",
    [
        "http://offline.trycloudflare.com",
        "https://trycloudflare.com",
        "https://offline.trycloudflare.com.attacker.test",
        "https://offline.trycloudflare.com/private",
        "https://offline.trycloudflare.com?token=secret",
    ],
)
def test_tunnel_does_not_trust_fake_or_non_https_origin(module, monkeypatch, origin):
    process = FakeTunnel(f"INF {origin}\nINF Registered tunnel connection\n")
    monkeypatch.setattr(
        module.DualReferenceMediaRelay, "_spawn_tunnel", lambda _: process
    )
    instance = module.DualReferenceMediaRelay(port=0, start_timeout=0.03)
    with pytest.raises(module.MediaRelayError):
        instance.start()
    assert not instance.status()["ready"]
    assert process.terminated


def test_url_announcement_alone_is_not_connected(module, monkeypatch):
    process = FakeTunnel(f"INF {ORIGIN}\n")
    monkeypatch.setattr(
        module.DualReferenceMediaRelay, "_spawn_tunnel", lambda _: process
    )
    instance = module.DualReferenceMediaRelay(port=0, start_timeout=0.03)
    with pytest.raises(module.MediaRelayError):
        instance.start()
    assert process.terminated


def test_process_failure_does_not_leave_ready_status_or_registration(
    relay, video, module
):
    relay._process.returncode = 1
    assert not relay.status()["ready"]
    with pytest.raises(module.MediaRelayError) as exc:
        relay.register(video)
    assert exc.value.code == "RELAY_NOT_READY"


def test_start_idempotent_and_close_releases_port(relay, video, module):
    origin = relay.start()
    assert origin == ORIGIN
    relay.register(video)
    local = relay.local_origin
    relay.close()
    relay.close()
    assert not relay.status()["ready"]
    assert relay.status()["registeredCount"] == 0
    with pytest.raises(httpx.ConnectError):
        with httpx.Client(trust_env=False) as client:
            client.get(local + "/", timeout=0.2)
    with pytest.raises(module.MediaRelayError):
        relay.register(video)


async def test_async_adapter_and_module_level_facade(module, relay, monkeypatch, video):
    monkeypatch.setattr(module, "_DEFAULT_RELAY", relay)
    assert module.status()["ready"]
    assert module.start() == ORIGIN
    urls = await asyncio.gather(*(module.ensure_public_video(video) for _ in range(4)))
    assert len(set(urls)) == 1
    assert request(relay, route(urls[0])).content == MP4
    module.close()


def test_no_token_path_or_tunnel_logs(relay, video, caplog, capsys):
    url = relay.register(video)
    request(relay, route(url))
    request(relay, "/not-found?token=private-query")
    outputs = capsys.readouterr()
    logs = caplog.text + outputs.out + outputs.err
    assert route(url) not in logs
    assert str(video) not in logs
    assert "private-query" not in logs
    assert url not in repr(relay.status())


def test_spawn_command_is_dedicated_no_config_no_debug_no_credentials(
    module, monkeypatch
):
    captured = {}
    process = FakeTunnel()

    def spawn(argv, **kwargs):
        captured.update(argv=argv, kwargs=kwargs)
        return process

    monkeypatch.setattr(module.subprocess, "Popen", spawn)
    monkeypatch.setenv("TUNNEL_URL", "http://127.0.0.1:8780")
    monkeypatch.setenv("TUNNEL_LOGLEVEL", "debug")
    monkeypatch.setenv("TUNNEL_TOKEN", "do-not-inherit")
    monkeypatch.setenv("TUNNEL_LOGFILE", "/private/do-not-write.log")
    instance = module.DualReferenceMediaRelay(port=0)
    try:
        instance.start()
        argv = captured["argv"]
        assert "--url" in argv
        assert argv[argv.index("--url") + 1] == instance.local_origin
        owned_config = Path(argv[argv.index("--config") + 1])
        assert str(owned_config) != os.devnull
        assert json.loads(owned_config.read_text()) == {}
        assert owned_config.stat().st_mode & 0o777 == 0o600
        assert owned_config.parent.stat().st_mode & 0o777 == 0o700
        assert argv[argv.index("--metrics") + 1] == "127.0.0.1:0"
        assert argv[argv.index("--loglevel") + 1] == "info"
        assert "--no-autoupdate" in argv
        assert "--token" not in argv
        assert not any(key.startswith("TUNNEL_") for key in captured["kwargs"]["env"])
        assert captured["kwargs"]["stdin"] == subprocess.PIPE
        assert captured["kwargs"]["stderr"] == subprocess.STDOUT
        assert "do-not-inherit" not in repr(captured)
    finally:
        instance.close()
    assert not owned_config.exists()


def test_real_guard_start_and_http_protocol_with_offline_tunnel_standin(
    module, tmp_path, video
):
    standin = tmp_path / "offline-cloudflared"
    pid_file = tmp_path / "standin.pid"
    standin.write_text(
        f"#!{Path(sys.executable).resolve()}\n"
        "import os,time\n"
        "from pathlib import Path\n"
        f"Path({str(pid_file)!r}).write_text(str(os.getpid()))\n"
        f"print({READY_LOG!r}, end='', flush=True)\n"
        "time.sleep(60)\n"
    )
    standin.chmod(0o700)
    instance = module.DualReferenceMediaRelay(
        port=0, start_timeout=5, cloudflared_path=standin
    )
    try:
        assert instance.start() == ORIGIN
        url = instance.register(video)
        response = request(instance, route(url), headers={"Range": "bytes=9-80"})
        assert response.status_code == 206
        assert response.content == MP4[9:81]
        owned_pid = int(pid_file.read_text())
    finally:
        instance.close()
    assert not instance.status()["ready"]
    with pytest.raises(ProcessLookupError):
        os.kill(owned_pid, 0)


def test_large_mp4_is_streamed_without_truncation(relay, video):
    content = MP4 + bytes(range(256)) * 4096
    video.write_bytes(content)
    path = route(relay.register(video))
    assert request(relay, path).content == content
    response = request(relay, path, headers={"Range": "bytes=60000-200000"})
    assert response.status_code == 206
    assert response.content == content[60000:200001]


def test_guard_preserves_child_exit_code_for_safe_diagnostics(module):
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            module._TUNNEL_GUARD_SCRIPT,
            sys.executable,
            "-c",
            "raise SystemExit(23)",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        assert process.wait(timeout=4) == 23
    finally:
        process.stdin.close()
        if process.poll() is None:
            process.kill()
        process.wait(timeout=4)


@pytest.mark.parametrize("parent_signal", [None, signal.SIGTERM, signal.SIGKILL])
def test_guard_reaps_only_owned_child_when_parent_exits(
    module, tmp_path, parent_signal
):
    # Run the production guardian with a harmless stand-in, never cloudflared.
    pid_file = tmp_path / "owned.pid"
    child_code = (
        "import os,pathlib,time;"
        f"pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid()));"
        "time.sleep(60)"
    )
    script = (
        "import subprocess,sys,time\n"
        "from pathlib import Path\n"
        f"script={module._TUNNEL_GUARD_SCRIPT!r}\n"
        f"argv=[sys.executable,'-c',script,sys.executable,'-c',{child_code!r}]\n"
        "p=subprocess.Popen(argv,stdin=subprocess.PIPE,stdout=subprocess.DEVNULL,"
        "stderr=subprocess.DEVNULL)\n"
        f"while not Path({str(pid_file)!r}).exists(): time.sleep(0.01)\n"
        + ("time.sleep(60)\n" if parent_signal else "time.sleep(0.05)\n")
    )
    parent = subprocess.Popen([sys.executable, "-c", script])
    try:
        deadline = time.monotonic() + 4
        while not pid_file.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert pid_file.exists()
        child_pid = int(pid_file.read_text())
        if parent_signal:
            parent.send_signal(parent_signal)
        parent.wait(timeout=4)
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            try:
                os.kill(child_pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.02)
        else:
            pytest.fail("owned tunnel stand-in survived parent exit")
    finally:
        if parent.poll() is None:
            parent.kill()
        parent.wait(timeout=4)
