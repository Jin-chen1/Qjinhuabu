import json
import os
import time

import pytest


class FakeBase:
    def __init__(self):
        self.token = None
        self.created = 0
        self.records = {}
        self.uploads = 0
        self.fail_lookup = False
        self.lose_create = False
        self.lose_record = False
        self.lose_upload = False

    def find_base(self):
        if self.fail_lookup:
            raise RuntimeError("lookup failed")
        return self.token

    def create_base(self):
        self.created += 1
        self.token = "base_test"
        if self.lose_create:
            raise RuntimeError("receipt lost")
        return self.token

    def binding(self, token):
        assert token == self.token
        return {"base_token": token, "table_id": "tbl_test", "attachment_field": "fld_file",
                "url": f"https://example.com/base/{token}"}

    def find_record(self, binding, key):
        return next((r for r in self.records.values() if r["key"] == key), None)

    def create_record(self, binding, fields):
        record_id = f"rec_{len(self.records)}"
        self.records[record_id] = {"id": record_id, "key": fields["素材标识"], "attachments": []}
        if self.lose_record:
            raise RuntimeError("receipt lost")
        return record_id

    def upload(self, binding, record_id, snapshot, name):
        self.uploads += 1
        self.records[record_id]["attachments"].append({"name": name, "size": snapshot.stat().st_size})
        if self.lose_upload:
            raise RuntimeError("receipt lost")


def library(tmp_path, client):
    from novelvideo.doubao_work.media_library import MediaLibrary
    output = tmp_path / "output"
    output.mkdir(exist_ok=True)
    return MediaLibrary(tmp_path / "library", output, client)


def test_single_base_survives_restart_and_binding_loss(tmp_path):
    client = FakeBase()
    first = library(tmp_path, client)
    first.ensure()
    binding = first.status()["url"]
    second = library(tmp_path, client)
    second.ensure()
    assert second.status()["url"] == binding
    (tmp_path / "library/binding.json").unlink()
    library(tmp_path, client).ensure()
    assert client.created == 1


def test_lookup_failure_never_creates_a_base(tmp_path):
    client = FakeBase()
    client.fail_lookup = True
    with pytest.raises(RuntimeError):
        library(tmp_path, client).ensure()
    assert client.created == 0


def test_lost_base_receipt_recovers_without_recreate(tmp_path):
    client = FakeBase()
    client.lose_create = True
    with pytest.raises(RuntimeError):
        library(tmp_path, client).ensure()
    library(tmp_path, client).ensure()
    assert client.created == 1


def test_unknown_base_with_no_match_never_recreates(tmp_path):
    client = FakeBase()
    client.lose_create = True
    with pytest.raises(RuntimeError):
        library(tmp_path, client).ensure()
    client.token = None
    with pytest.raises(RuntimeError):
        library(tmp_path, client).ensure()
    assert client.created == 1


@pytest.mark.parametrize("lost", ["lose_record", "lose_upload"])
def test_unknown_record_or_upload_recovers_once_after_restart(tmp_path, lost):
    client = FakeBase()
    lib = library(tmp_path, client)
    lib.ensure()
    source = tmp_path / "reference.png"
    source.write_bytes(b"reference")
    lib.enqueue(source, "reference.png", "project", "upload", "reference")
    source.unlink()
    setattr(client, lost, True)
    lib.sync()
    restored = library(tmp_path, client)
    restored.sync()
    assert len(client.records) == 1
    assert client.uploads == 1
    assert restored.status()["saved"] == 1
    assert restored.status()["pending"] == 0


def test_unknown_write_empty_lookup_never_reposts(tmp_path):
    client = FakeBase()
    lib = library(tmp_path, client)
    lib.ensure()
    source = tmp_path / "source.png"
    source.write_bytes(b"image")
    lib.enqueue(source, "source.png", "", "upload", "source")
    client.lose_record = True
    lib.sync()
    client.records.clear()
    lib.sync()
    assert not client.records
    assert lib.status()["pending"] == 1


def test_scanner_baseline_new_files_and_changed_content(tmp_path):
    client = FakeBase()
    lib = library(tmp_path, client)
    old = lib.output / "old.png"
    old.write_bytes(b"old")
    lib.scan()
    fresh = lib.output / "project/freezone/_uploads/fresh.png"
    fresh.parent.mkdir(parents=True)
    fresh.write_bytes(b"new")
    os.utime(fresh, (time.time() - 10, time.time() - 10))
    lib.scan()
    lib.scan()
    lib.sync()
    assert len(client.records) == 1
    assert lib.status()["saved"] == 1
    fresh.write_bytes(b"changed")
    os.utime(fresh, (time.time() - 10, time.time() - 10))
    lib.scan()
    lib.scan()
    lib.sync()
    assert len(client.records) == 2


def test_pending_snapshot_is_private_and_duplicate_enqueue_is_safe(tmp_path):
    client = FakeBase()
    lib = library(tmp_path, client)
    source = tmp_path / "source.png"
    source.write_bytes(b"original")
    lib.enqueue(source, "../../source.png", "", "upload", "key")
    lib.enqueue(source, "source.png", "", "upload", "key")
    assert lib.status()["pending"] == 1
    snapshots = list((tmp_path / "library/spool").glob("*"))
    assert len(snapshots) == 1
    assert snapshots[0].read_bytes() == b"original"
    assert snapshots[0].stat().st_mode & 0o777 == 0o600
    assert not (tmp_path / "library/binding.json").exists()


def test_symlinks_and_temporary_files_are_not_archived(tmp_path):
    client = FakeBase()
    lib = library(tmp_path, client)
    lib.scan()
    outside = tmp_path / "private.png"
    outside.write_bytes(b"private")
    (lib.output / "link.png").symlink_to(outside)
    (lib.output / "pending.part").write_bytes(b"partial")
    lib.scan()
    lib.scan()
    assert lib.status()["pending"] == 0


def test_existing_binding_failure_keeps_original_id(tmp_path):
    client = FakeBase()
    lib = library(tmp_path, client)
    lib.ensure()
    client.token = "other_base"
    with pytest.raises(AssertionError):
        library(tmp_path, client).ensure()
    assert client.created == 1
    assert json.loads((tmp_path / "library/binding.json").read_text())["base_token"] == "base_test"


def test_cli_missing_base_is_distinct_from_permission_failure(monkeypatch):
    import subprocess
    from novelvideo.doubao_work.media_library_client import BaseClient, LibraryError

    client = BaseClient("/fake/lark-cli")
    def result(message):
        return subprocess.CompletedProcess([], 2, "", json.dumps({
            "ok": False, "error": {"type": "validation", "message": message},
        }))
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: result("No Base matched this title or keyword."))
    assert client.find_base() is None
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: result("Permission denied: PRIVATE"))
    with pytest.raises(LibraryError) as error:
        client.find_base()
    assert "PRIVATE" not in str(error.value)


def test_cli_ndjson_manifest_without_ok_is_valid(monkeypatch):
    import subprocess
    from novelvideo.doubao_work.media_library_client import BaseClient

    manifest = {"manifest_version": "v1", "format": "ndjson", "has_more": False,
                "records_count": 0, "record_file": "records.ndjson"}
    monkeypatch.setattr(subprocess, "run", lambda *a, **k:
                        subprocess.CompletedProcess([], 0, json.dumps(manifest), ""))
    assert BaseClient("/fake/lark-cli").run("+record-list") == manifest


def test_cli_progress_before_rejection_preserves_known_failure(monkeypatch):
    import subprocess
    from novelvideo.doubao_work.media_library_client import BaseClient, LibraryError

    error = {"ok": False, "error": {"type": "validation", "message": "unsafe file path"}}
    monkeypatch.setattr(subprocess, "run", lambda *a, **k:
                        subprocess.CompletedProcess([], 2, "", "Uploading attachment\n" + json.dumps(error)))
    with pytest.raises(LibraryError) as result:
        BaseClient("/fake/lark-cli").run("+record-upload-attachment", write=True)
    assert result.value.unknown is False


def test_cli_upload_uses_a_private_copy_without_hard_links(tmp_path, monkeypatch):
    import subprocess
    from pathlib import Path
    from novelvideo.doubao_work.media_library_client import BaseClient
    source = tmp_path / "source.jpg"
    source.write_bytes(b"actual bytes")
    def run(argv, **kwargs):
        uploaded = Path(kwargs["cwd"]) / argv[argv.index("--file") + 1]
        assert uploaded.stat().st_nlink == 1
        assert uploaded.stat().st_mode & 0o777 == 0o600
        assert uploaded.read_bytes() == source.read_bytes()
        return subprocess.CompletedProcess([], 0, '{"ok":true,"data":{}}', "")
    monkeypatch.setattr(subprocess, "run", run)
    BaseClient("/fake/lark-cli").upload(
        {"base_token": "b", "table_id": "t", "attachment_field": "f"}, "r", source, "原图.jpg",
    )


def test_cli_missing_scope_is_a_known_rejection(monkeypatch):
    import subprocess
    from novelvideo.doubao_work.media_library_client import BaseClient, LibraryError
    error = {"ok": False, "error": {"type": "authorization", "subtype": "missing_scope"}}
    monkeypatch.setattr(subprocess, "run", lambda *a, **k:
                        subprocess.CompletedProcess([], 3, "", json.dumps(error)))
    with pytest.raises(LibraryError) as result:
        BaseClient("/fake/lark-cli").run("+record-upload-attachment", write=True)
    assert result.value.unknown is False


def test_failed_known_upload_can_retry_after_authorization(tmp_path):
    from novelvideo.doubao_work.media_library_client import LibraryError
    client = FakeBase()
    lib = library(tmp_path, client)
    source = tmp_path / "source.png"
    source.write_bytes(b"image")
    lib.enqueue(source, "source.png", "", "upload", "auth-retry")
    upload = client.upload
    def rejected(*args):
        raise LibraryError("LARK_REQUEST_REJECTED", unknown=False)
    client.upload = rejected
    lib.sync()
    assert lib.status()["state"] == "attention"
    client.upload = upload
    lib.sync()
    assert lib.status()["saved"] == 1
    assert len(client.records) == client.uploads == 1


def test_reference_snapshot_is_enqueued_before_temporary_cleanup(tmp_path, monkeypatch):
    import base64
    import io
    from uuid import uuid4
    from PIL import Image
    from novelvideo.doubao_work import media_library_runtime
    from novelvideo.doubao_work.jobs import JobStore

    lib = library(tmp_path, FakeBase())
    monkeypatch.setattr(media_library_runtime, "get_library", lambda: lib)
    data = io.BytesIO()
    Image.new("RGB", (8, 8)).save(data, format="PNG")
    store = JobStore(tmp_path / "jobs", tmp_path / "results", object())
    job_id = str(uuid4())
    store.create(job_id, "image", "test", [
        {"name": "ref.png", "contentBase64": base64.b64encode(data.getvalue()).decode()},
    ])
    assert lib.status()["pending"] == 1
    store.create(job_id, "image", "test", [
        {"name": "ref.png", "contentBase64": base64.b64encode(data.getvalue()).decode()},
    ])
    assert lib.status()["pending"] == 1
