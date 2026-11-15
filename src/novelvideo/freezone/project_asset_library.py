"""Project-local material indexing; cloud writes remain in the existing outbox."""

import hashlib
import json
import os
import time
from pathlib import Path

from novelvideo.doubao_work.media_library import (
    AUDIO_SUFFIXES, MEDIA_SUFFIXES, VIDEO_SUFFIXES,
)
from novelvideo.utils.static_urls import project_static_url

from .paths import resolve_static_url_to_path
from .video_node import (
    _library_lock, _upsert_library_item, _write_json_atomic,
    load_video_character_library, save_video_character_library,
)


def _files(project_dir):
    for directory, dirs, names in os.walk(project_dir, followlinks=False):
        dirs[:] = [name for name in dirs if not name.startswith(".")
                   and name not in {"_thumbs", "_history", "_generation_history"}
                   and not (Path(directory) / name).is_symlink()]
        for name in sorted(names):
            path = Path(directory) / name
            relative = path.relative_to(project_dir)
            # These are input samples for the analysis model, not user outputs.
            if "freezone_video_breakdown" in relative.parts and "frames" in relative.parts:
                continue
            if name.startswith(".") or path.suffix.lower() not in MEDIA_SUFFIXES:
                continue
            if not path.is_symlink() and path.is_file():
                yield path


def _fingerprint(path):
    stat = path.stat()
    return f"{stat.st_size}:{stat.st_mtime_ns}"


def _digest(path):
    # Refuse a symlink swapped in after inventory. Hash only a stable file.
    digest = hashlib.sha256()
    with os.fdopen(os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)), "rb") as stream:
        before = os.fstat(stream.fileno())
        if not 0 < before.st_size <= 2 * 1024**3:
            raise ValueError("MEDIA_SIZE_LIMIT")
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
        after = os.fstat(stream.fileno())
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise ValueError("MEDIA_STILL_WRITING")
    return digest.hexdigest()


def _item_urls(item):
    if item.get("media") == "video":
        return [item.get("video_url")]
    if item.get("media") == "audio":
        return [item.get("audio_url")]
    return item.get("image_urls") or []


def reconcile_project_assets(project_dir: Path, project_id: str, library) -> list[dict]:
    """Index only this authorized project; reuse archive path/content identities.

    The manifest remembers indexed content after a user deletes its library
    entry, so periodic discovery cannot undo deletion. Renames/folders are never
    updated by discovery, except the one-time migration of unsorted materials
    into image/video folders. No network call is made on this path.
    """
    project_dir = project_dir.resolve()
    relative = project_dir.relative_to(library.output)
    if len(relative.parts) != 2:
        raise ValueError("expected an output/user/project directory")
    with library.db() as db:
        db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (
            "project-index:" + library.source_root + ":" + relative.as_posix(),
            json.dumps({"relative": relative.as_posix(), "project_id": project_id}),
        ))
    manifest_path = project_dir / "freezone" / "asset_media_index.json"
    with _library_lock(project_dir):
        manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
        cached = manifest.setdefault("files", {})
        indexed = manifest.setdefault("indexed", {})
        items = load_video_character_library(project_dir)
        changed = False
        if not manifest.get("media_folders_v1"):
            for item in items:
                if (item.get("source") == "upload"
                        and item.get("category", "other") == "other"
                        and item.get("folder", "other") == "other"
                        and item.get("media", "image") in ("image", "video")):
                    item["folder"] = item.get("media", "image")
                    changed = True
            manifest["media_folders_v1"] = True
        by_sha = {}
        for path in _files(project_dir):
            rel = path.relative_to(project_dir).as_posix()
            try:
                # Uploads are atomically published by the upload endpoint.
                # Encoders may still be writing directly to generated paths.
                if "_uploads" not in path.parts and time.time() - path.stat().st_mtime < 3:
                    continue
                fingerprint = _fingerprint(path)
                previous = cached.get(rel, {})
                sha = previous.get("sha") if previous.get("fingerprint") == fingerprint else _digest(path)
                if _fingerprint(path) != fingerprint:
                    continue
            except (OSError, ValueError):
                continue
            cached[rel] = {"fingerprint": fingerprint, "sha": sha}
            by_sha.setdefault(sha, []).append(path)

        # Match preexisting manually registered/mainline items before appending.
        item_shas = {}
        for item in items:
            shas = []
            for url in _item_urls(item):
                try:
                    path = resolve_static_url_to_path(url, project_dir)
                    rel = path.relative_to(project_dir).as_posix()
                    entry = cached.get(rel)
                    if entry and _fingerprint(path) == entry["fingerprint"]:
                        shas.append(entry["sha"])
                except (ValueError, OSError, TypeError):
                    continue
            item_shas[item["id"]] = shas
            for sha in shas:
                indexed.setdefault(sha, item["id"])

        for sha, paths in by_sha.items():
            if sha in indexed:
                continue
            path = paths[0]
            suffix = path.suffix.lower()
            media = "video" if suffix in VIDEO_SUFFIXES else "audio" if suffix in AUDIO_SUFFIXES else "image"
            url = project_static_url(project_id, path.relative_to(project_dir).as_posix(), path)
            item = _upsert_library_item(
                items, name=path.name[:60], media=media, source="upload",
                image_urls=[url] if media == "image" else [],
                video_url=url if media == "video" else None,
                audio_url=url if media == "audio" else None,
                item_id="local:" + sha, category=None, folder=None,
            )
            indexed[sha] = item["id"]
            item_shas[item["id"]] = [sha]
            changed = True
        # Publish local items before snapshot failures can occur.
        if changed:
            save_video_character_library(project_dir, items)
        _write_json_atomic(manifest_path, manifest)

        with library.db() as db:
            records = {row["key"]: dict(row) for row in db.execute("SELECT key,stage,error FROM assets")}
            connection_error = db.execute("SELECT 1 FROM meta WHERE key='error'").fetchone()
        result = []
        for item in items:
            keys, states = [], []
            for sha in set(item_shas.get(item["id"], [])):
                paths = by_sha.get(sha, [])
                candidates = [
                    (hashlib.sha256((str(path) + "\0" + sha).encode()).hexdigest(), path)
                    for path in paths
                ]
                # Any saved project copy is sufficient; never upload a duplicate
                # just because a different path is first in the inventory.
                candidates.sort(key=lambda pair: records.get(pair[0], {}).get("stage") != "saved")
                if not candidates:
                    states.append("failed")
                    continue
                key, path = candidates[0]
                record = records.get(key)
                if record is None:
                    try:
                        key = library.enqueue(path, path.name, project_dir.name,
                                              "上传" if "_uploads" in path.parts else "生成/导出",
                                              str(path))
                    except (OSError, ValueError):
                        states.append("failed")
                        continue
                    record = {"stage": "queued", "error": None}
                keys.append(key)
                stage = record["stage"]
                states.append("saved" if stage == "saved" else
                              "pending" if stage in {"record_pending", "upload_pending"} else
                              "failed" if record["error"] or connection_error else "pending")
            state = ("failed" if "failed" in states else
                     "pending" if not states or "pending" in states else "saved")
            result.append({**item, "archive_status": state, "archive_keys": keys})
        return result
