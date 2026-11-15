"""Seal an app and archive exactly the source snapshot it contains."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import zipfile

parser = argparse.ArgumentParser()
parser.add_argument("--source", required=True, type=Path)
parser.add_argument("--app", required=True, type=Path)
parser.add_argument("--zip", required=True, type=Path)
parser.add_argument("--staging", required=True, type=Path)
parser.add_argument("--frontend", type=Path)
parser.add_argument("--normalize-python-from", type=Path)
args = parser.parse_args()
excluded = {
    "node_modules", ".venv", ".git", "dist", ".build", "state", "output",
    "runtime", "__pycache__", ".pytest_cache", ".ruff_cache", ".tanstack",
    ".DS_Store", ".env", ".env.local", "SOURCE-MANIFEST.json",
}


def ignore(directory, names):
    return [name for name in names if name in excluded or name.endswith(".pyc")]


shutil.copytree(args.source, args.staging, ignore=ignore, dirs_exist_ok=True)
files = []
for filename in sorted(args.staging.rglob("*")):
    if filename.is_file() and not filename.is_symlink() and filename.name != "SOURCE-MANIFEST.json":
        files.append({
            "path": filename.relative_to(args.staging).as_posix(),
            "bytes": filename.stat().st_size,
            "sha256": hashlib.sha256(filename.read_bytes()).hexdigest(),
        })
manifest = {"app": "Q 金", "version": "2.1.1", "date": "2026-11-10", "files": files}
(args.staging / "SOURCE-MANIFEST.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
resources = args.app / "Contents/Resources"
if args.frontend:
    shutil.rmtree(resources / "payload/frontend")
    shutil.copytree(args.frontend, resources / "payload/frontend")
shutil.copytree(args.staging, resources / "payload/source", dirs_exist_ok=True)
shutil.copytree(args.staging / "desktop", resources / "app", dirs_exist_ok=True)
shutil.copy2(args.staging / "desktop/QJin.icns", resources / "QJin.icns")

if args.normalize_python_from:
    python_root = resources / "payload/python"
    for filename in python_root.rglob("*"):
        if filename.is_symlink():
            target = Path(os.readlink(filename))
            if target.is_absolute():
                relative = target.relative_to(args.normalize_python_from)
                filename.unlink()
                filename.symlink_to(os.path.relpath(python_root / relative, filename.parent))

for filename in (resources / "payload").rglob("*"):
    if filename.is_symlink():
        assert not Path(os.readlink(filename)).is_absolute(), str(filename)
        assert filename.exists(), str(filename)

info_path = args.app / "Contents/Info.plist"
with info_path.open("rb") as stream:
    info = plistlib.load(stream)
info["CFBundleDisplayName"] = "Q 金"
info["CFBundleIconFile"] = "QJin.icns"
info["LSApplicationCategoryType"] = "public.app-category.video"
with info_path.open("wb") as stream:
    plistlib.dump(info, stream)
subprocess.run(["/usr/bin/codesign", "--force", "--deep", "--sign", "-", str(args.app)], check=True)
subprocess.run(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(args.app)], check=True)

with zipfile.ZipFile(args.zip, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
    for filename in sorted(args.staging.rglob("*")):
        if filename.is_file() and not filename.is_symlink():
            archive.write(filename, "Q金源码/" + filename.relative_to(args.staging).as_posix())

with zipfile.ZipFile(args.zip) as archive:
    assert archive.testzip() is None
    for item in files:
        zip_bytes = archive.read("Q金源码/" + item["path"])
        app_bytes = (resources / "payload/source" / item["path"]).read_bytes()
        assert hashlib.sha256(zip_bytes).hexdigest() == item["sha256"]
        assert zip_bytes == app_bytes, item["path"]
    assert len(archive.namelist()) == len(files) + 1
print(json.dumps({
    "app": str(args.app), "source_zip": str(args.zip), "source_files": len(files),
    "source_matches_app": True, "zip_bytes": args.zip.stat().st_size,
    "zip_sha256": hashlib.sha256(args.zip.read_bytes()).hexdigest(),
}, ensure_ascii=False, indent=2))
