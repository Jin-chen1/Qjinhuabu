"""Explicit, one-hour MP4 links for the authorized local dual-reference adapter.

No directory, application port, user key, or remote source is accepted. Call
``start()`` during service preflight, ``status()`` for a secret-free result, and
``await ensure_public_video(path)`` only for the reference selected at submission.
The UI integration must explain that only that reference gets a temporary link.
This module does not submit generation requests or upload a media library.
"""

from __future__ import annotations

import asyncio
import atexit
import hashlib
import math
import os
import re
import secrets
import stat
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, BinaryIO

DEFAULT_PORT = 8794
TTL_SECONDS = 3600
_FORBIDDEN_PORTS = frozenset({8780, 5173, 8791})
_ORIGIN_PATTERN = re.compile(
    r"https://[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.trycloudflare\.com(?=[\s|]|$)"
)
_RANGE_PATTERN = re.compile(r"bytes=(\d*)-(\d*)")

_ERROR_MESSAGES = {
    "RELAY_PORT_INVALID": "参考视频临时链接服务需要独立的本机端口",
    "RELAY_CONFIG_INVALID": "参考视频临时链接服务配置无效",
    "RELAY_BIND_FAILED": "无法绑定参考视频临时链接服务的独立端口，请检查端口占用",
    "RELAY_TUNNEL_START_FAILED": "无法启动参考视频临时访问通道，请检查本机 cloudflared",
    "RELAY_TUNNEL_TIMEOUT": "参考视频临时访问通道连接超时，请检查网络",
    "RELAY_TUNNEL_EXITED": "参考视频临时访问通道在连接成功前已退出",
    "RELAY_NOT_READY": "参考视频临时链接服务尚未就绪，请先完成服务预检",
    "RELAY_VIDEO_INVALID": "请选择已存在的本机 MP4 视频文件作为本次参考",
}


class MediaRelayError(RuntimeError):
    """A fixed, secret-free failure code suitable for adapter/preflight results."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(_ERROR_MESSAGES[code])


# stdin is a parent-liveness pipe, not request input. EOF also covers SIGKILL
# of the application. The guard owns exactly one child and never scans processes.
_TUNNEL_GUARD_SCRIPT = """
import os
import select
import signal
import subprocess
import sys

child = None
def stop(signum, frame):
    raise SystemExit(0)

signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)
try:
    child = subprocess.Popen(sys.argv[1:], stdin=subprocess.DEVNULL, close_fds=True)
    while child.poll() is None:
        readable, _, _ = select.select([sys.stdin.fileno()], [], [], 0.2)
        if readable and not os.read(sys.stdin.fileno(), 1):
            break
finally:
    if child is not None:
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=2)
            except subprocess.TimeoutExpired:
                child.kill()
        child.wait()
if child is not None:
    code = child.returncode
    sys.exit(code if code >= 0 else 128 - code)
"""


def _identity(info: os.stat_result) -> tuple[int, ...]:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _open_regular(path: Path) -> BinaryIO:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("not a regular file")
        return os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)
        raise


@dataclass(slots=True)
class _VideoEntry:
    path: Path = field(repr=False)
    identity: tuple[int, ...] = field(repr=False)
    digest: str = field(repr=False)
    expires_at: float
    revoked: bool = False


class _VideoHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False

    def __init__(self, port: int, relay: DualReferenceMediaRelay) -> None:
        self.relay = relay
        super().__init__(("127.0.0.1", port), _VideoHandler)

    def handle_error(self, request: Any, client_address: Any) -> None:
        # The default traceback can include request paths or local file names.
        pass


class _VideoHandler(BaseHTTPRequestHandler):
    server: _VideoHTTPServer
    server_version = "MediaRelay"
    sys_version = ""

    def setup(self) -> None:
        self.request.settimeout(10)
        super().setup()

    def log_message(self, format: str, *args: Any) -> None:
        pass

    def send_error(
        self, code: int, message: str | None = None, explain: str | None = None
    ) -> None:
        self._empty(code)

    def _headers(
        self, code: int, length: int, content_range: str | None = None
    ) -> None:
        self.send_response(code)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if content_range:
            self.send_header("Content-Range", content_range)
        if code == 405:
            self.send_header("Allow", "GET, HEAD")
        self.end_headers()

    def _empty(self, code: int, content_range: str | None = None) -> None:
        self._headers(code, 0, content_range)

    def do_GET(self) -> None:
        self._serve()

    def do_HEAD(self) -> None:
        self._serve(head=True)

    def do_POST(self) -> None:
        self._empty(405)

    do_PUT = do_POST
    do_DELETE = do_POST
    do_PATCH = do_POST
    do_OPTIONS = do_POST
    do_TRACE = do_POST
    do_CONNECT = do_POST

    def _serve(self, *, head: bool = False) -> None:
        code, file, size = self.server.relay._open_registered(self.path)
        if file is None:
            self._empty(code)
            return
        with file:
            start, end, partial = 0, size - 1, False
            header = self.headers.get("Range", "")
            # RFC 9110: Range applies to GET only. No validators are advertised,
            # so If-Range cannot match and is answered with the full representation.
            match = _RANGE_PATTERN.fullmatch(header) if len(header) < 128 else None
            if not head and not self.headers.get("If-Range") and match:
                first, last = match.groups()
                if first or last:
                    partial = True
                    if first:
                        start = int(first)
                        end = min(int(last), end) if last else end
                    else:
                        suffix = int(last)
                        if suffix == 0:
                            self._empty(416, f"bytes */{size}")
                            return
                        start = max(0, size - suffix)
                    if start >= size or start > end:
                        self._empty(416, f"bytes */{size}")
                        return
            length = end - start + 1
            content_range = f"bytes {start}-{end}/{size}" if partial else None
            self._headers(206 if partial else 200, length, content_range)
            if head:
                return
            try:
                file.seek(start)
                remaining = length
                while remaining:
                    # Expiry also bounds an already-open slow download.
                    if not self.server.relay._is_active(self.path, file):
                        return
                    chunk = file.read(min(64 * 1024, remaining))
                    if not chunk or not self.server.relay._is_active(self.path, file):
                        return
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
            except (OSError, ValueError):
                return


class DualReferenceMediaRelay:
    """One process-owned HTTP server and quick tunnel, with explicit MP4 grants.

    ``register`` returns a bearer HTTPS link, reusing identical selected content
    until its original expiry (reuse never extends TTL). ``port=0`` lets the OS
    select a dedicated port. A failed start does not forward an existing listener.
    """

    def __init__(
        self,
        *,
        port: int = DEFAULT_PORT,
        start_timeout: float = 45,
        cloudflared_path: str | Path | None = None,
    ) -> None:
        if type(port) is not int or not 0 <= port <= 65535 or port in _FORBIDDEN_PORTS:
            raise MediaRelayError("RELAY_PORT_INVALID")
        if (
            not isinstance(start_timeout, (int, float))
            or not math.isfinite(start_timeout)
            or start_timeout <= 0
        ):
            raise MediaRelayError("RELAY_CONFIG_INVALID")
        self.port = port
        self.ttl_seconds = TTL_SECONDS
        self._start_timeout = start_timeout
        self._cloudflared_path = Path(
            cloudflared_path or Path.home() / ".local" / "bin" / "cloudflared"
        )
        self._lock = threading.RLock()
        self._clock = time.monotonic
        self._server: _VideoHTTPServer | None = None
        self._server_thread: threading.Thread | None = None
        self._reader_thread: threading.Thread | None = None
        self._process: subprocess.Popen | None = None
        self._config_directory: tempfile.TemporaryDirectory | None = None
        self._public_origin: str | None = None
        self._tunnel_connected = threading.Event()
        self._entries: dict[str, _VideoEntry] = {}
        self._digests: dict[str, str] = {}
        self._error = ""
        self._exit_code: int | None = None
        self._exit_registered = False

    @property
    def local_origin(self) -> str:
        with self._lock:
            if self._server is None:
                raise MediaRelayError("RELAY_NOT_READY")
            return f"http://127.0.0.1:{self._server.server_port}"

    @property
    def public_origin(self) -> str | None:
        with self._lock:
            return self._public_origin if self._ready() else None

    def _ready(self) -> bool:
        return bool(
            self._server is not None
            and self._server_thread is not None
            and self._server_thread.is_alive()
            and self._process is not None
            and self._process.poll() is None
            and self._public_origin
            and self._tunnel_connected.is_set()
        )

    def status(self) -> dict[str, Any]:
        """Report preflight state, never registered paths, tokens, or raw logs."""
        with self._lock:
            return {
                "ready": self._ready(),
                "port": self._server.server_port if self._server else None,
                "ttlSeconds": self.ttl_seconds,
                "registeredCount": sum(
                    not entry.revoked and entry.expires_at > self._clock()
                    for entry in self._entries.values()
                ),
                "error": self._error,
                "exitCode": self._exit_code,
            }

    def _spawn_tunnel(self) -> subprocess.Popen:
        # Ignore named-tunnel credentials, inherited forwarding/logging settings,
        # and the user's existing cloudflared configuration.
        environment = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("TUNNEL_", "CLOUDFLARED_"))
        }
        self._config_directory = tempfile.TemporaryDirectory(
            prefix="qjin-reference-relay-"
        )
        config_file = Path(self._config_directory.name) / "config.json"
        config_file.write_text("{}\n", encoding="ascii")
        config_file.chmod(0o600)
        argv = [
            sys.executable,
            "-c",
            _TUNNEL_GUARD_SCRIPT,
            str(self._cloudflared_path),
            "tunnel",
            "--config",
            str(config_file),
            "--no-autoupdate",
            "--url",
            self.local_origin,
            "--metrics",
            "127.0.0.1:0",
            "--loglevel",
            "info",
            "--protocol",
            "http2",
            "--grace-period",
            "1s",
        ]
        return subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=0,
            close_fds=True,
            start_new_session=True,
            env=environment,
        )

    def _read_tunnel_output(self, process: subprocess.Popen) -> None:
        pending = ""
        try:
            while process.stdout is not None:
                chunk = process.stdout.read(1024)
                if not chunk:
                    return
                pending += (
                    chunk.decode("utf-8", errors="replace")
                    if isinstance(chunk, bytes)
                    else chunk
                )
                while "\n" in pending:
                    line, pending = pending.split("\n", 1)
                    found = _ORIGIN_PATTERN.search(line)
                    if found:
                        self._public_origin = found.group(0)
                    if "Registered tunnel connection" in line:
                        self._tunnel_connected.set()
                pending = pending[-8192:]
        except (OSError, ValueError):
            return

    def start(self) -> str:
        """Bind our own listener, await real HTTPS origin and edge connection.

        This blocking preflight never registers or sends any reference. Async
        callers should use ``await asyncio.to_thread(start)``.
        """
        with self._lock:
            if self._ready():
                return self._public_origin  # type: ignore[return-value]
            self._close_locked()
            self._error = ""
            self._exit_code = None
            try:
                try:
                    self._server = _VideoHTTPServer(self.port, self)
                    # Even an OS-selected listener must not use an app port.
                    while self._server.server_port in _FORBIDDEN_PORTS:
                        self._server.server_close()
                        self._server = _VideoHTTPServer(0, self)
                except OSError:
                    raise MediaRelayError("RELAY_BIND_FAILED") from None
                self._server_thread = threading.Thread(
                    target=self._server.serve_forever,
                    kwargs={"poll_interval": 0.05},
                    name="reference-video-http",
                    daemon=True,
                )
                self._server_thread.start()
                try:
                    self._process = self._spawn_tunnel()
                except (OSError, ValueError):
                    raise MediaRelayError("RELAY_TUNNEL_START_FAILED") from None
                self._reader_thread = threading.Thread(
                    target=self._read_tunnel_output,
                    args=(self._process,),
                    name="reference-video-tunnel",
                    daemon=True,
                )
                self._reader_thread.start()
                deadline = time.monotonic() + self._start_timeout
                while not self._ready():
                    if self._process.poll() is not None:
                        self._exit_code = self._process.returncode
                        raise MediaRelayError("RELAY_TUNNEL_EXITED")
                    if time.monotonic() >= deadline:
                        raise MediaRelayError("RELAY_TUNNEL_TIMEOUT")
                    time.sleep(0.01)
                if not self._exit_registered:
                    atexit.register(self.close)
                    self._exit_registered = True
                return self._public_origin  # type: ignore[return-value]
            except BaseException as exc:
                self._error = (
                    exc.code
                    if isinstance(exc, MediaRelayError)
                    else "RELAY_TUNNEL_START_FAILED"
                )
                self._close_locked()
                raise

    def register(self, path: str | Path) -> str:
        """Grant only this selected file; hash it locally, never upload a folder."""
        with self._lock:
            if not self._ready():
                raise MediaRelayError("RELAY_NOT_READY")
            try:
                source = Path(path)
                if source.suffix.lower() != ".mp4" or source.is_symlink():
                    raise ValueError("not an mp4")
                source = source.resolve(strict=True)
                with _open_regular(source) as file:
                    before = _identity(os.fstat(file.fileno()))
                    header = file.read(12)
                    if len(header) != 12 or header[4:8] != b"ftyp":
                        raise ValueError("invalid mp4 header")
                    file.seek(0)
                    digest = hashlib.file_digest(file, "sha256").hexdigest()
                    if _identity(os.fstat(file.fileno())) != before:
                        raise ValueError("reference changed")
            except (OSError, ValueError, TypeError, RuntimeError):
                raise MediaRelayError("RELAY_VIDEO_INVALID") from None
            now = self._clock()
            existing_route = self._digests.get(digest)
            if existing_route:
                existing = self._entries[existing_route]
                if not existing.revoked and now < existing.expires_at:
                    try:
                        unchanged = (
                            _identity(existing.path.lstat()) == existing.identity
                        )
                    except OSError:
                        unchanged = False
                    if not unchanged:
                        existing.path = source
                        existing.identity = before
                    return self._public_origin + existing_route  # type: ignore[operator]
            token_route = "/" + secrets.token_urlsafe(32)
            while token_route in self._entries:
                token_route = "/" + secrets.token_urlsafe(32)
            self._entries[token_route] = _VideoEntry(
                source,
                before,
                digest,
                now + self.ttl_seconds,
            )
            self._digests[digest] = token_route
            return self._public_origin + token_route  # type: ignore[operator]

    async def ensure_public_video(self, path: str | Path) -> str:
        """Start lazily if needed, then grant only the selected MP4 in a worker."""

        def ensure() -> str:
            self.start()
            return self.register(path)

        return await asyncio.to_thread(ensure)

    def _is_active(self, route: str, file: BinaryIO | None = None) -> bool:
        with self._lock:
            entry = self._entries.get(route)
            if not entry or entry.revoked or self._clock() >= entry.expires_at:
                return False
            if file is not None:
                try:
                    unchanged = _identity(os.fstat(file.fileno())) == entry.identity
                except (OSError, ValueError):
                    unchanged = False
                if not unchanged:
                    entry.revoked = True
                    return False
            return True

    def _open_registered(self, route: str) -> tuple[int, BinaryIO | None, int]:
        with self._lock:
            entry = self._entries.get(route)
            if entry is None:
                return 404, None, 0
            if not self._is_active(route):
                return 410, None, 0
            try:
                file = _open_regular(entry.path)
            except OSError:
                entry.revoked = True
                return 410, None, 0
            if _identity(os.fstat(file.fileno())) != entry.identity:
                file.close()
                entry.revoked = True
                return 410, None, 0
            return 200, file, entry.identity[2]

    def _close_locked(self) -> None:
        process, self._process = self._process, None
        if process is not None:
            if process.stdin is not None:
                process.stdin.close()
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=4)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)
            else:
                process.wait(timeout=2)
        # server.shutdown does not wait for request threads (which use _lock).
        server, self._server = self._server, None
        if server is not None:
            if self._server_thread and self._server_thread.is_alive():
                server.shutdown()
            server.server_close()
        if self._server_thread:
            self._server_thread.join(timeout=1)
        if self._reader_thread:
            self._reader_thread.join(timeout=1)
        if process is not None and process.stdout is not None:
            process.stdout.close()
        self._server_thread = self._reader_thread = None
        self._public_origin = None
        self._tunnel_connected.clear()
        self._entries.clear()
        self._digests.clear()
        if self._config_directory is not None:
            self._config_directory.cleanup()
            self._config_directory = None

    def close(self) -> None:
        """Revoke links and stop only the listener and child owned by this object."""
        with self._lock:
            self._close_locked()
            if self._exit_registered:
                atexit.unregister(self.close)
                self._exit_registered = False


_DEFAULT_RELAY: DualReferenceMediaRelay | None = None
_DEFAULT_LOCK = threading.Lock()


def _default() -> DualReferenceMediaRelay:
    global _DEFAULT_RELAY
    with _DEFAULT_LOCK:
        if _DEFAULT_RELAY is None:
            _DEFAULT_RELAY = DualReferenceMediaRelay()
        return _DEFAULT_RELAY


def start() -> str:
    """Blocking preflight; import alone never starts a tunnel."""
    return _default().start()


def status() -> dict[str, Any]:
    """Secret-free local status; does not start a tunnel or register media."""
    return _default().status()


async def ensure_public_video(path: str | Path) -> str:
    """Adapter facade: return a one-hour HTTPS link for this selected MP4 only."""
    return await _default().ensure_public_video(path)


def close() -> None:
    """Application shutdown hook."""
    with _DEFAULT_LOCK:
        if _DEFAULT_RELAY is not None:
            _DEFAULT_RELAY.close()
