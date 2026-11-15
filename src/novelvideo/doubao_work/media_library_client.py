"""User-identity lark-cli adapter; no credentials copied into application state."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile


BASE_NAME = "Q金素材库"
FIELDS = [
    {"name": "素材名称", "type": "text"},
    {"name": "素材文件", "type": "attachment"},
    {"name": "素材类型", "type": "text"},
    {"name": "所属项目", "type": "text"},
    {"name": "来源", "type": "text"},
    {"name": "保存时间", "type": "datetime", "style": {"format": "yyyy-MM-dd HH:mm"}},
    {"name": "素材标识", "type": "text"},
    {"name": "文件大小", "type": "number"},
    {"name": "SHA256", "type": "text"},
]


class LibraryError(RuntimeError):
    def __init__(self, code: str, unknown: bool = False):
        super().__init__(code)
        self.unknown = unknown


def objects(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from objects(child)
    elif isinstance(value, list):
        for child in value:
            yield from objects(child)


def executable():
    configured = os.environ.get("QJIN_LARK_CLI") or shutil.which("lark-cli")
    if configured:
        return configured
    versions = Path.home() / ".trae-cn/plugins/trae-remote-official/lark"
    candidates = list(versions.glob("*/bin/lark-cli"))
    if candidates:
        return str(max(candidates, key=lambda p: tuple(
            int(n) if n.isdigit() else 0 for n in p.parent.parent.name.split(".")
        )))
    raise LibraryError("LARK_CLI_REQUIRED")


class BaseClient:
    def __init__(self, binary=None):
        self.binary = binary

    def run(self, command, *arguments, cwd=None, write=False, timeout=60):
        try:
            result = subprocess.run(
                [self.binary or executable(), "base", command, *map(str, arguments), "--as", "user"],
                cwd=cwd, capture_output=True, text=True, timeout=timeout,
                env={key: os.environ[key] for key in ("HOME", "PATH", "TMPDIR") if key in os.environ},
            )
        except subprocess.TimeoutExpired:
            raise LibraryError("LARK_REQUEST_TIMEOUT", unknown=write) from None
        except OSError:
            raise LibraryError("LARK_CLI_REQUIRED") from None
        try:
            raw = result.stdout or result.stderr
            # Upload progress may precede the JSON error envelope on stderr.
            envelope = json.loads(raw[raw.index("{"):])
        except ValueError:
            raise LibraryError("LARK_RECEIPT_UNAVAILABLE", unknown=write) from None
        manifest = (command == "+record-list" and envelope.get("manifest_version") == "v1"
                    and envelope.get("format") == "ndjson" and "records_count" in envelope)
        if result.returncode or (envelope.get("ok") is not True and not manifest):
            error = envelope.get("error", {})
            if command == "+title-resolve" and error.get("message") == "No Base matched this title or keyword.":
                return None
            rejected = error.get("type") in {
                "validation", "auth", "permission", "authentication", "authorization",
            }
            raise LibraryError("LARK_REQUEST_REJECTED" if rejected else "LARK_REQUEST_FAILED",
                               unknown=write and not rejected)
        return envelope.get("data", envelope)

    @staticmethod
    def token(data):
        tokens = {o.get("base_token") or o.get("app_token") for o in objects(data)
                  if o.get("base_token") or o.get("app_token")}
        if len(tokens) != 1:
            raise LibraryError("BASE_AMBIGUOUS")
        return tokens.pop()

    def find_base(self):
        data = self.run("+title-resolve", "--title", BASE_NAME)
        if data is None:
            return None
        # A keyword search must never silently bind a similarly named Base.
        names = [o.get("name") or o.get("title") for o in objects(data)
                 if o.get("base_token") or o.get("app_token")]
        if any(name and name != BASE_NAME for name in names):
            raise LibraryError("BASE_AMBIGUOUS")
        return self.token(data)

    def create_base(self):
        data = self.run("+base-create", "--name", BASE_NAME, "--table-name", "素材",
                        "--time-zone", "Asia/Shanghai", "--fields",
                        json.dumps(FIELDS, ensure_ascii=False), write=True)
        return self.token(data)

    def binding(self, token):
        self.run("+base-get", "--base-token", token)
        tables = self.run("+table-list", "--base-token", token)
        ids = {o.get("table_id") or o.get("id") for o in objects(tables)
               if o.get("name") == "素材" and (o.get("table_id") or o.get("id"))}
        if len(ids) != 1:
            raise LibraryError("LIBRARY_TABLE_REQUIRED")
        table = ids.pop()
        fields = self.run("+field-list", "--base-token", token, "--table-id", table)
        by_name = {o.get("name"): o for o in objects(fields)
                   if o.get("field_id") or (o.get("id", "").startswith("fld"))}
        if not all(field["name"] in by_name for field in FIELDS):
            raise LibraryError("LIBRARY_SCHEMA_REQUIRED")
        attachment = by_name["素材文件"]
        if attachment.get("type") not in {"attachment", 17}:
            raise LibraryError("LIBRARY_ATTACHMENT_REQUIRED")
        return {"base_token": token, "table_id": table,
                "attachment_field": attachment.get("field_id") or attachment["id"],
                "url": f"https://bytedance.larkoffice.com/base/{token}?table={table}"}

    def find_record(self, binding, key):
        with tempfile.TemporaryDirectory(prefix="qjin-record-") as directory:
            data = self.run(
                "+record-list", "--base-token", binding["base_token"], "--table-id", binding["table_id"],
                "--field-id", "素材标识", "--field-id", "素材文件",
                "--filter-json", json.dumps({"logic": "and", "conditions": [["素材标识", "==", key]]}),
                "--format", "ndjson", "--output", "records.ndjson", "--overwrite", "--limit", "10",
                cwd=directory,
            )
            if any(o.get("has_more") is True for o in objects(data)):
                raise LibraryError("RECORD_AMBIGUOUS")
            rows = [json.loads(line) for line in (Path(directory) / "records.ndjson").read_text().splitlines()
                    if line.strip()]
        if len(rows) > 1:
            raise LibraryError("RECORD_AMBIGUOUS")
        if not rows:
            return None
        row = rows[0]
        fields = row.get("fields", row)
        record_id = row.get("record_id") or row.get("id")
        if not record_id:
            raise LibraryError("RECORD_RECEIPT_UNAVAILABLE")
        return {"id": record_id, "key": key, "attachments": fields.get("素材文件") or []}

    def create_record(self, binding, fields):
        data = self.run("+record-batch-create", "--base-token", binding["base_token"],
                        "--table-id", binding["table_id"], "--json",
                        json.dumps({"create_records": [fields]}, ensure_ascii=False), write=True)
        lists = [o["record_id_list"] for o in objects(data) if "record_id_list" in o]
        if len(lists) != 1 or len(lists[0]) != 1:
            raise LibraryError("RECORD_RECEIPT_UNAVAILABLE", unknown=True)
        return lists[0][0]

    def upload(self, binding, record_id, snapshot, name):
        with tempfile.TemporaryDirectory(prefix="qjin-upload-") as directory:
            target = Path(directory) / Path(name).name
            # CLI rejects hard links as unsafe; upload a private regular file.
            shutil.copyfile(snapshot, target)
            os.chmod(target, 0o600)
            self.run("+record-upload-attachment", "--base-token", binding["base_token"],
                     "--table-id", binding["table_id"], "--record-id", record_id,
                     "--field-id", binding["attachment_field"], "--file", target.name,
                     cwd=directory, write=True, timeout=180)
