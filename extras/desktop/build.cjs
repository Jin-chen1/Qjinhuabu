const fs = require("node:fs");
const path = require("node:path");
const { execFileSync } = require("node:child_process");
const crypto = require("node:crypto");
const { pathToFileURL } = require("node:url");

const flags = {};
for (let i = 2; i < process.argv.length; i += 2) flags[process.argv[i].replace(/^--/, "")] = process.argv[i + 1];
const source = path.resolve(__dirname, "..");
const repo = path.join(source, "dramaclaw");
const out = path.resolve(flags.out || path.join(source, "dist"));
const backendEnv = path.resolve(flags["backend-env"] || path.join(repo, ".venv"));
const python = flags.python || execFileSync(path.join(backendEnv, "bin/python"), ["-c", "import sys;print(sys.base_prefix)"], { encoding: "utf8" }).trim();
const tools = flags.tools ? path.resolve(flags.tools, "node_modules") : null;
const work = path.join(out, ".build");
const payload = path.join(work, "payload");
fs.mkdirSync(payload, { recursive: true });
const skip = new Set(["node_modules", ".venv", ".git", "dist", ".build", "state", "output", "runtime", "__pycache__", ".pytest_cache", ".ruff_cache", ".tanstack", ".DS_Store", ".env", ".env.local", ".cognee_system", ".cognee_cache", ".data_storage", "SOURCE-MANIFEST.json"]);
function allowed(filename) {
  return !skip.has(path.basename(filename)) && !filename.endsWith(".pyc") && !path.basename(filename).startsWith("_editable");
}
function copy(from, to) { fs.cpSync(from, to, { recursive: true, verbatimSymlinks: true, filter: allowed }); }
function checksum(filename) { return crypto.createHash("sha256").update(fs.readFileSync(filename)).digest("hex"); }
function manifest(root, prefix = "") {
  const entries = [];
  for (const item of fs.readdirSync(root, { withFileTypes: true }).sort((a, b) => a.name.localeCompare(b.name))) {
    const filename = path.join(root, item.name);
    if (!allowed(filename) || item.isSymbolicLink()) continue;
    const relative = prefix + item.name;
    if (item.isDirectory()) entries.push(...manifest(filename, relative + "/"));
    else if (item.isFile()) entries.push({ path: relative, bytes: fs.statSync(filename).size, sha256: checksum(filename) });
  }
  return entries;
}

(async () => {
  if (process.platform !== "darwin" || process.arch !== "arm64") throw new Error("请在 Apple Silicon macOS 上打包。");
  const { packager } = await import(tools ? pathToFileURL(path.join(tools, "@electron/packager/dist/index.js")).href : "@electron/packager");
  const sourceManifest = { app: "Q 金", version: "2.1.1", date: "2026-11-10", files: manifest(source) };
  fs.writeFileSync(path.join(source, "SOURCE-MANIFEST.json"), JSON.stringify(sourceManifest, null, 2) + "\n");
  copy(source, path.join(payload, "source"));
  fs.copyFileSync(path.join(source, "SOURCE-MANIFEST.json"), path.join(payload, "source/SOURCE-MANIFEST.json"));
  copy(python, path.join(payload, "python"));
  copy(path.join(backendEnv, "lib/python3.12/site-packages"), path.join(payload, "python/lib/python3.12/site-packages"));
  fs.cpSync(path.join(repo, "frontend/dist"), path.join(payload, "frontend"), { recursive: true });
  fs.mkdirSync(path.join(payload, "bin"), { recursive: true });
  const which = (name) => execFileSync("/usr/bin/which", [name], { encoding: "utf8" }).trim();
  const codex = flags.codex || execFileSync("/bin/bash", [path.join(repo, "scripts/setup-codex.sh")], { encoding: "utf8" }).trim();
  if (execFileSync(codex, ["--version"], { encoding: "utf8" }).trim() !== "codex-cli 0.164.0") throw new Error("Codex 必须为项目专用 0.164.0。");
  for (const [name, filename] of Object.entries({ node: flags.node || process.execPath, ffmpeg: flags.ffmpeg || which("ffmpeg"), ffprobe: flags.ffprobe || which("ffprobe"), codex })) {
    fs.copyFileSync(filename, path.join(payload, "bin", name));
    fs.chmodSync(path.join(payload, "bin", name), 0o755);
  }
  if (!fs.existsSync(path.join(__dirname, "QJin.icns"))) {
    execFileSync(path.join(payload, "python/bin/python3.12"), [path.join(__dirname, "make-icon.py")]);
    execFileSync("/usr/bin/iconutil", ["-c", "icns", path.join(__dirname, "QJin.iconset"), "-o", path.join(__dirname, "QJin.icns")]);
  }
  const apps = await packager({
    dir: __dirname, out, name: "Q金", appBundleId: "local.qjin.desktop",
    appVersion: "2.1.1", buildVersion: "20261110", electronVersion: "45.2.3",
    platform: "darwin", arch: "arm64", asar: false, overwrite: false,
    icon: path.join(__dirname, "QJin.icns"), osxSign: false,
    ...(flags["electron-zip-dir"] ? { electronZipDir: flags["electron-zip-dir"] } : {}),
    extendInfo: { LSMinimumSystemVersion: "13.0", CFBundleDisplayName: "Q 金", NSHighResolutionCapable: true },
    ignore: [/node_modules/, /QJin\.iconset/], prune: false,
  });
  const appPath = path.join(apps[0], "Q金.app");
  fs.cpSync(payload, path.join(appPath, "Contents/Resources/payload"), { recursive: true, verbatimSymlinks: true });
  fs.copyFileSync(path.join(__dirname, "QJin.icns"), path.join(appPath, "Contents/Resources/QJin.icns"));
  const plist = path.join(appPath, "Contents/Info.plist");
  execFileSync("/usr/bin/plutil", ["-replace", "CFBundleIconFile", "-string", "QJin.icns", plist]);
  execFileSync("/usr/bin/plutil", ["-replace", "CFBundleDisplayName", "-string", "Q 金", plist]);
  execFileSync("/usr/bin/codesign", ["--force", "--deep", "--sign", "-", appPath], { stdio: "inherit" });
  console.log(appPath);
})().catch((error) => { console.error(error); process.exitCode = 1; });
