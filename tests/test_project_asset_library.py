import pytest
import os
import time

from novelvideo.doubao_work.media_library import MediaLibrary
from novelvideo.freezone.video_node import (
    delete_video_character_library_item,
    load_video_character_library,
    rename_video_character_library_item,
    add_video_character_library_item,
    save_video_character_library,
)


def setup_project(tmp_path):
    output = tmp_path / "output"
    project = output / "local" / "demo"
    project.mkdir(parents=True)
    library = MediaLibrary(tmp_path / "archive", output, None)
    return project, library


def reconcile(project, library):
    from novelvideo.freezone.project_asset_library import reconcile_project_assets
    return reconcile_project_assets(project, "project_id", library)


def put(project, rel, data=b"image"):
    path = project / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    os.utime(path, (time.time() - 10, time.time() - 10))
    return path


def test_backfill_is_project_scoped_deduplicated_and_preserves_user_edits(tmp_path):
    project, lib = setup_project(tmp_path)
    put(project, "frames/beat.png")
    put(project, "frames/copy.png")
    put(project, "_thumbs/cache.webp", b"thumb")
    put(project, "freezone/_outputs/freezone_video_breakdown/job/frames/native_01.jpg", b"cache")
    put(project.parent / "other", "other.png", b"other")
    items = reconcile(project, lib)
    assert len(items) == 1
    assert items[0]["archive_status"] == "pending"
    rename_video_character_library_item(project, items[0]["id"], name="我的封面")
    assert reconcile(project, lib)[0]["name"] == "我的封面"
    assert lib.status()["pending"] == 1
    delete_video_character_library_item(project, items[0]["id"])
    assert reconcile(project, lib) == []


def test_saved_history_reuses_original_archive_key(tmp_path):
    project, lib = setup_project(tmp_path)
    path = put(project, "freezone/_outputs/video.mp4", b"video")
    key = lib.enqueue(path, path.name, "local", "生成/导出", str(path.resolve()))
    lib.update(key, stage="saved", record_id="existing")
    items = reconcile(project, lib)
    assert items[0]["archive_status"] == "saved"
    assert items[0]["archive_keys"] == [key]
    assert lib.status()["saved"] == 1
    assert lib.status()["pending"] == 0


def test_upload_appears_while_archive_snapshot_fails_and_retries(tmp_path, monkeypatch):
    project, lib = setup_project(tmp_path)
    put(project, "freezone/_uploads/upload.wav", b"audio")
    original = lib.enqueue
    monkeypatch.setattr(lib, "enqueue", lambda *args: (_ for _ in ()).throw(OSError("disk")))
    items = reconcile(project, lib)
    assert len(load_video_character_library(project)) == 1
    assert items[0]["media"] == "audio"
    assert items[0]["archive_status"] == "failed"
    monkeypatch.setattr(lib, "enqueue", original)
    items = reconcile(project, lib)
    assert len(items) == 1
    assert items[0]["archive_status"] == "pending"


@pytest.mark.parametrize("stage,error,status", [
    ("queued", None, "pending"),
    ("record_ready", "SYNC_PENDING", "failed"),
    ("upload_pending", "SYNC_PENDING", "pending"),
    ("saved", None, "saved"),
])
def test_per_file_status_does_not_label_unknown_write_as_failure(tmp_path, stage, error, status):
    project, lib = setup_project(tmp_path)
    put(project, "frames/one.png")
    items = reconcile(project, lib)
    lib.update(items[0]["archive_keys"][0], stage=stage, error=error)
    assert reconcile(project, lib)[0]["archive_status"] == status


def test_symlink_and_outside_project_are_never_indexed(tmp_path):
    project, lib = setup_project(tmp_path)
    outside = put(tmp_path, "secret.png")
    (project / "linked.png").symlink_to(outside)
    assert reconcile(project, lib) == []
    with pytest.raises(ValueError):
        reconcile(tmp_path, lib)


def test_manual_registration_after_discovery_updates_instead_of_duplicates(tmp_path):
    project, lib = setup_project(tmp_path)
    put(project, "freezone/_uploads/one.png")
    item = reconcile(project, lib)[0]
    registered = add_video_character_library_item(
        project, name="上传封面", image_urls=item["image_urls"], category="scene",
    )
    assert registered["id"] == item["id"]
    assert len(reconcile(project, lib)) == 1
    assert reconcile(project, lib)[0]["category"] == "scene"


def test_background_index_remembers_project_across_restart(tmp_path):
    project, lib = setup_project(tmp_path)
    reconcile(project, lib)
    put(project, "freezone/_outputs/new.mp4", b"new video")
    restarted = MediaLibrary(lib.root, lib.output, None)
    from novelvideo.doubao_work.media_library_runtime import _index_projects
    _index_projects(restarted)
    items = load_video_character_library(project)
    assert len(items) == 1
    assert items[0]["media"] == "video"


def test_recent_generation_is_not_published_while_encoder_may_be_writing(tmp_path):
    project, lib = setup_project(tmp_path)
    path = put(project, "freezone/_outputs/encoding.mp4", b"partial")
    os.utime(path, None)
    assert reconcile(project, lib) == []
    os.utime(path, (time.time() - 10, time.time() - 10))
    assert len(reconcile(project, lib)) == 1


@pytest.mark.parametrize("suffix,media,folder", [
    ("png", "image", "image"), ("mp4", "video", "video"), ("wav", "audio", "audio"),
])
def test_new_materials_default_to_separate_media_folders(tmp_path, suffix, media, folder):
    project, lib = setup_project(tmp_path)
    put(project, f"freezone/_outputs/new.{suffix}")
    item = reconcile(project, lib)[0]
    assert (item["media"], item["folder"]) == (media, folder)
    registered = add_video_character_library_item(
        project, name="manual", media=media, image_urls=["/image.png"],
    )
    assert registered["folder"] == folder


def test_media_folder_migration_runs_once_and_preserves_custom_moves(tmp_path):
    project, lib = setup_project(tmp_path)
    put(project, "freezone/_outputs/one.png")
    put(project, "freezone/_outputs/two.mp4", b"video")
    items = reconcile(project, lib)
    image = next(item for item in items if item["media"] == "image")
    video = next(item for item in items if item["media"] == "video")
    image["folder"] = "other"
    video["folder"] = "my-folder"
    save_video_character_library(project, items)
    manifest = project / "freezone" / "asset_media_index.json"
    import json
    data = json.loads(manifest.read_text())
    data.pop("media_folders_v1", None)
    manifest.write_text(json.dumps(data))
    migrated = {item["id"]: item for item in reconcile(project, lib)}
    assert migrated[image["id"]]["folder"] == "image"
    assert migrated[video["id"]]["folder"] == "my-folder"
    assert migrated[image["id"]]["archive_keys"] == image["archive_keys"]
    image["folder"] = "other"  # A deliberate move after migration must survive.
    save_video_character_library(project, [image, video])
    assert reconcile(project, lib)[0]["folder"] == "other"


@pytest.mark.asyncio
async def test_actual_upload_endpoint_indexes_valid_image_before_return(tmp_path, monkeypatch):
    import io
    from PIL import Image
    from starlette.datastructures import UploadFile
    from novelvideo.api.routes import freezone as routes
    from novelvideo.doubao_work import media_library_runtime
    from novelvideo.utils.static_urls import project_static_url

    project, lib = setup_project(tmp_path)

    async def resolve(*args, **kwargs):
        return None, "local", "demo", project, lib.output

    monkeypatch.setattr(routes, "_resolve_freezone_project", resolve)
    monkeypatch.setattr(media_library_runtime, "get_library", lambda: lib)
    monkeypatch.setattr(routes, "make_static_url_for_context",
                        lambda ctx, rel, **kwargs: project_static_url("project_id", rel))
    data = io.BytesIO()
    Image.new("RGB", (4, 4), "blue").save(data, format="PNG")
    data.seek(0)
    response = await routes._save_freezone_upload(
        "project_id", UploadFile(filename="reference.png", file=data), {},
    )
    assert response["ok"]
    items = load_video_character_library(project)
    assert len(items) == 1
    assert items[0]["media"] == "image"
    assert lib.status()["pending"] == 1
