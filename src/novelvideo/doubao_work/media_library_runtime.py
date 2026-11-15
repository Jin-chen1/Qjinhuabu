"""Local CE media archiving lifecycle, enabled by the macOS launcher."""

import asyncio
from functools import lru_cache
import logging
import json
import os
from pathlib import Path
import sys
import threading

from novelvideo.shared.runtime_env import is_ce_effective

from .media_library import MediaLibrary
from .media_library_client import BaseClient

logger = logging.getLogger(__name__)
_stop = threading.Event()
_wake = threading.Event()


def enabled():
    return (sys.platform == "darwin" and is_ce_effective()
            and os.environ.get("SUPERTALE_ENV") != "production"
            and os.environ.get("QJIN_MEDIA_LIBRARY_ENABLED") == "1")


@lru_cache(maxsize=1)
def _library():
    from novelvideo.config import OUTPUT_DIR
    return MediaLibrary(
        Path.home() / "Library/Application Support/QJin/media-library",
        Path(OUTPUT_DIR), BaseClient(),
    )


def get_library():
    return _library() if enabled() else None


def capture_reference(path, name, job_id, index):
    lib = get_library()
    if lib:
        # Fail before accepting the job if its only durable reference snapshot
        # cannot be saved. A retry uses the same key and cannot duplicate it.
        lib.enqueue(path, name, "豆包工作", "参考图上传", f"native:{job_id}:ref:{index}")
        _wake.set()


def status():
    lib = get_library()
    return lib.status() if lib else {"enabled": False, "url": None, "saved": 0,
                                    "pending": 0, "unknown": 0, "state": "disabled"}


def request_sync():
    _wake.set()


def _index_projects(lib):
    from novelvideo.freezone.project_asset_library import reconcile_project_assets
    prefix = "project-index:" + lib.source_root + ":"
    with lib.db() as db:
        projects = [row["value"] for row in db.execute(
            "SELECT key,value FROM meta"
        ) if row["key"].startswith(prefix)]
    for value in projects:
        try:
            project = json.loads(value)
            directory = lib.output / project["relative"]
            if directory.is_dir():
                reconcile_project_assets(directory, project["project_id"], lib)
        except Exception:
            logger.warning("Q Jin project material index will retry")


def _worker():
    lib = get_library()
    if not lib:
        return
    next_sync = 0
    while not _stop.is_set():
        try:
            _index_projects(lib)
            lib.scan()
            if _wake.is_set() or asyncio_time() >= next_sync:
                _wake.clear()
                lib.sync()
                next_sync = asyncio_time() + 30
        except Exception:
            # Never expose upstream error bodies, signed URLs or credentials.
            logger.warning("Q Jin material library is waiting for connection or write verification")
            next_sync = asyncio_time() + 30
        _stop.wait(5)


def asyncio_time():
    import time
    return time.monotonic()


async def start():
    if not enabled():
        return None
    _stop.clear()
    # Establish the old-file baseline before network provisioning begins.
    await asyncio.to_thread(get_library().scan)
    task = asyncio.create_task(asyncio.to_thread(_worker), name="qjin-media-library")
    return task


async def stop(task):
    if task:
        _stop.set()
        await task
