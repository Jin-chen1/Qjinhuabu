"""One durable Base binding and a private, restart-safe media outbox."""

import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
from contextlib import contextmanager

import portalocker

from .jobs import private_write


MEDIA_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".webp", ".gif", ".avif", ".heic", ".bmp", ".tiff",
    ".mp4", ".mov", ".webm", ".mkv", ".m4v",
    ".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac",
}
VIDEO_SUFFIXES = {".mp4", ".mov", ".webm", ".mkv", ".m4v"}
AUDIO_SUFFIXES = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"}


class MediaLibrary:
    def __init__(self, root: Path, output: Path, client):
        self.root, self.output, self.client = root, output.resolve(), client
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(root, 0o700)
        self.spool = root / "spool"
        self.spool.mkdir(exist_ok=True, mode=0o700)
        self.db_path = root / "outbox.sqlite3"
        self.lock = threading.RLock()
        self.candidates = {}
        self.binding_path = root / "binding.json"
        self.source_root = str(self.output)
        self.db_init()

    @contextmanager
    def db(self):
        connection = sqlite3.connect(self.db_path, timeout=30)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def db_init(self):
        with self.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS assets (
                    key TEXT PRIMARY KEY, name TEXT, project TEXT, source TEXT,
                    sha TEXT, size INTEGER, snapshot TEXT, created REAL,
                    stage TEXT DEFAULT 'queued', record_id TEXT, error TEXT
                );
                CREATE TABLE IF NOT EXISTS seen (
                    root TEXT, path TEXT, fingerprint TEXT,
                    PRIMARY KEY(root,path)
                );
                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
            """)
        os.chmod(self.db_path, 0o600)

    @contextmanager
    def writer(self):
        # Only one process may create a Base or write records at a time. Enqueue
        # is independent so cloud latency never holds up local media delivery.
        with self.lock, portalocker.Lock(str(self.root / "sync.lock"), timeout=1):
            yield

    def read_binding(self):
        if self.binding_path.exists():
            return json.loads(self.binding_path.read_text())
        return {}

    def write_binding(self, value):
        private_write(self.binding_path, json.dumps(value, ensure_ascii=False).encode())

    def _ensure(self):
        binding = self.read_binding()
        token = binding.get("base_token")
        if not token:
            token = self.client.find_base()
            if not token:
                if binding.get("creation_pending"):
                    raise RuntimeError("BASE_CREATION_UNKNOWN")
                self.write_binding({"creation_pending": True})
                token = self.client.create_base()
            # Persist the remote ID before schema inspection can fail.
            self.write_binding({"base_token": token})
        result = self.client.binding(token)
        self.write_binding(result)
        return result

    def ensure(self):
        with self.writer():
            return self._ensure()

    def enqueue(self, path: Path, name: str, project: str, source: str, identity: str):
        """Snapshot bytes before callers can delete temporary reference files."""
        name = Path(name).name
        suffix = Path(name).suffix.lower()
        if suffix not in MEDIA_SUFFIXES:
            raise ValueError("UNSUPPORTED_MEDIA")
        descriptor, temporary = tempfile.mkstemp(dir=self.spool, suffix=".part")
        temporary = Path(temporary)
        try:
            digest = hashlib.sha256()
            with os.fdopen(descriptor, "wb") as target:
                fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
                with os.fdopen(fd, "rb") as source_file:
                    before = os.fstat(source_file.fileno())
                    if before.st_size == 0 or before.st_size > 2 * 1024**3:
                        raise ValueError("MEDIA_SIZE_LIMIT")
                    while chunk := source_file.read(1024 * 1024):
                        digest.update(chunk)
                        target.write(chunk)
                    after = os.fstat(source_file.fileno())
                if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                    raise ValueError("MEDIA_STILL_WRITING")
                target.flush()
                os.fsync(target.fileno())
            sha = digest.hexdigest()
            key = hashlib.sha256((identity + "\0" + sha).encode()).hexdigest()
            snapshot = self.spool / (key + suffix)
            with self.db() as db:
                if db.execute("SELECT 1 FROM assets WHERE key=?", (key,)).fetchone():
                    return key
                os.replace(temporary, snapshot)
                db.execute(
                    "INSERT OR IGNORE INTO assets "
                    "(key,name,project,source,sha,size,snapshot,created) VALUES (?,?,?,?,?,?,?,?)",
                    (key, name, project, source, sha, before.st_size, snapshot.name, time.time()),
                )
            return key
        finally:
            temporary.unlink(missing_ok=True)

    def files(self):
        for directory, dirs, filenames in os.walk(self.output, followlinks=False):
            dirs[:] = [d for d in dirs if not d.startswith(".")
                       and not (Path(directory) / d).is_symlink()]
            for filename in filenames:
                path = Path(directory) / filename
                if filename.startswith(".") or path.suffix.lower() not in MEDIA_SUFFIXES:
                    continue
                try:
                    if path.is_symlink() or not path.is_file():
                        continue
                    stat = path.stat()
                    yield path, stat, f"{stat.st_size}:{stat.st_mtime_ns}"
                except FileNotFoundError:
                    continue

    def scan(self):
        baseline_key = "baseline:" + self.source_root
        entries = list(self.files())
        with self.db() as db:
            initialized = db.execute("SELECT 1 FROM meta WHERE key=?", (baseline_key,)).fetchone()
            known = {r["path"]: r["fingerprint"] for r in db.execute(
                "SELECT path,fingerprint FROM seen WHERE root=?", (self.source_root,),
            )}
            if not initialized:
                db.executemany("INSERT OR REPLACE INTO seen VALUES (?,?,?)", [
                    (self.source_root, str(path.relative_to(self.output)), fingerprint)
                    for path, _, fingerprint in entries
                ])
                db.execute("INSERT INTO meta VALUES (?,?)", (baseline_key, "1"))
                return
        for path, stat, fingerprint in entries:
            relative = str(path.relative_to(self.output))
            if known.get(relative) == fingerprint:
                continue
            if self.candidates.get(relative) != fingerprint or time.time() - stat.st_mtime < 3:
                self.candidates[relative] = fingerprint
                continue
            parts = path.relative_to(self.output).parts
            source = "上传" if "_uploads" in parts else "生成/导出"
            try:
                self.enqueue(path, path.name, parts[0] if len(parts) > 1 else "", source,
                             self.source_root + "/" + relative)
            except (OSError, ValueError):
                continue
            with self.db() as db:
                db.execute("INSERT OR REPLACE INTO seen VALUES (?,?,?)",
                           (self.source_root, relative, fingerprint))
            self.candidates.pop(relative, None)

    def update(self, key, **changes):
        with self.db() as db:
            db.execute("UPDATE assets SET " + ",".join(f"{name}=?" for name in changes) +
                       " WHERE key=?", (*changes.values(), key))

    def sync_asset(self, binding, asset):
        key = asset["key"]
        stage = asset["stage"]
        record_id = asset["record_id"]
        record = self.client.find_record(binding, key)
        if record:
            record_id = record["id"]
            self.update(key, record_id=record_id)
        elif stage in {"record_pending", "upload_pending"}:
            raise RuntimeError("WRITE_OUTCOME_UNKNOWN")
        elif not record_id:
            suffix = Path(asset["name"]).suffix.lower()
            fields = {
                "素材名称": asset["name"], "素材类型": "视频" if suffix in VIDEO_SUFFIXES
                else "音频" if suffix in AUDIO_SUFFIXES else "图片",
                "所属项目": asset["project"], "来源": asset["source"],
                "保存时间": int(asset["created"] * 1000), "素材标识": key,
                "文件大小": asset["size"], "SHA256": asset["sha"],
            }
            self.update(key, stage="record_pending")
            try:
                record_id = self.client.create_record(binding, fields)
            except Exception as error:
                if getattr(error, "unknown", True) is False:
                    self.update(key, stage="queued")
                raise
            self.update(key, record_id=record_id, stage="record_ready")
        attachments = record["attachments"] if record else []
        matching = [a for a in attachments if a.get("name") == asset["name"]
                    and a.get("size") == asset["size"]]
        if not matching:
            if stage == "upload_pending":
                raise RuntimeError("WRITE_OUTCOME_UNKNOWN")
            self.update(key, stage="upload_pending")
            try:
                self.client.upload(binding, record_id, self.spool / asset["snapshot"], asset["name"])
            except Exception as error:
                if getattr(error, "unknown", True) is False:
                    self.update(key, stage="record_ready")
                raise
        self.update(key, stage="saved", error=None)
        (self.spool / asset["snapshot"]).unlink(missing_ok=True)

    def sync(self):
        with self.writer():
            try:
                binding = self._ensure()
            except Exception:
                with self.db() as db:
                    db.execute("INSERT OR REPLACE INTO meta VALUES ('error','CONNECTION_REQUIRED')")
                raise
            with self.db() as db:
                db.execute("DELETE FROM meta WHERE key='error'")
                assets = db.execute("SELECT * FROM assets WHERE stage!='saved' ORDER BY created LIMIT 20").fetchall()
            for asset in assets:
                try:
                    self.sync_asset(binding, asset)
                except Exception:
                    self.update(asset["key"], error="SYNC_PENDING")

    def status(self):
        binding = self.read_binding()
        with self.db() as db:
            saved = db.execute("SELECT count(*) FROM assets WHERE stage='saved'").fetchone()[0]
            pending = db.execute("SELECT count(*) FROM assets WHERE stage!='saved'").fetchone()[0]
            unknown = db.execute(
                "SELECT count(*) FROM assets WHERE stage IN ('record_pending','upload_pending')"
            ).fetchone()[0]
            error = db.execute("SELECT value FROM meta WHERE key='error'").fetchone()
            failed = db.execute("SELECT count(*) FROM assets WHERE stage!='saved' AND error IS NOT NULL").fetchone()[0]
        return {"enabled": True, "url": binding.get("url"), "saved": saved, "pending": pending,
                "unknown": unknown, "state": "attention" if error or unknown or failed else
                "ready" if binding.get("url") else "initializing"}
