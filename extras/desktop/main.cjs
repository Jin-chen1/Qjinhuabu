const { app, BrowserWindow, Menu, dialog, shell } = require("electron");
const fs = require("node:fs");
const path = require("node:path");
const os = require("node:os");
const { spawn } = require("node:child_process");
const { backendEnv, freePort, stopChild, isBackendReady } = require("./runtime.cjs");

const root = path.join(os.homedir(), "Library/Application Support/QJin/desktop");
const payload = path.join(process.resourcesPath, "payload");
const data = process.env.QJIN_DESKTOP_TEST_DATA || path.join(root, "data");
app.setPath("userData", path.join(root, "browser"));
app.setName("Q 金");
let backend, relay, window, quitting = false, origin, relayProject;

function startProcess(binary, args, env, name, cwd) {
  const log = fs.openSync(path.join(root, "logs", `${name}.log`), "a", 0o600);
  const child = spawn(binary, args, { cwd, env, stdio: ["ignore", log, log] });
  fs.closeSync(log);
  child.on("error", (error) => {
    fs.appendFileSync(path.join(root, "logs/desktop.log"), `${name}: ${error.message}\n`);
  });
  return child;
}

async function health(url) {
  try {
    const result = await fetch(url, { signal: AbortSignal.timeout(1500) });
    return result.ok ? await result.json() : null;
  } catch { return null; }
}

async function prepareRelay() {
  let settings = {};
  const settingsFile = path.join(root, "settings.json");
  if (fs.existsSync(settingsFile)) settings = JSON.parse(fs.readFileSync(settingsFile, "utf8"));
  relayProject = settings.relayProject || path.join(root, "doubao-work-api");
  if (!fs.existsSync(path.join(relayProject, "server.mjs"))) {
    fs.cpSync(path.join(payload, "source/doubao-work-api"), relayProject, { recursive: true });
  }
  fs.writeFileSync(settingsFile, JSON.stringify({ ...settings, relayProject }, null, 2), { mode: 0o600 });
  const existing = await health("http://127.0.0.1:8791/healthz");
  if (existing) {
    if (existing.service !== "doubao-work-local-api") {
      throw new Error("8791 已由其他服务占用，请检查本机豆包工作服务。");
    }
    return;
  }
  const env = {};
  for (const key of ["HOME", "USER", "LOGNAME", "TMPDIR", "LANG", "PATH"]) {
    if (process.env[key]) env[key] = process.env[key];
  }
  relay = startProcess(path.join(payload, "bin/node"), [path.join(relayProject, "server.mjs")], env, "doubao-work", relayProject);
}

function terminalCommand(command) {
  const quote = (text) => "'" + text.replaceAll("'", "'\\''") + "'";
  const filename = path.join(root, command === "configure" ? "配置豆包工作.command" : "授权豆包工作.command");
  const script = `#!/bin/bash\nset -e\n${quote(path.join(payload, "bin/node"))} ${quote(path.join(relayProject, "cli.mjs"))} ${command}\n`;
  fs.writeFileSync(filename, script, { mode: 0o700 });
  spawn("/usr/bin/open", ["-a", "Terminal", filename], { stdio: "ignore" });
}

function createWindow() {
  window = new BrowserWindow({
    width: 1440, height: 960, minWidth: 1024, minHeight: 680,
    title: "Q 金", backgroundColor: "#ffffff", show: false,
    webPreferences: { nodeIntegration: false, contextIsolation: true, sandbox: true },
  });
  window.webContents.setWindowOpenHandler(({ url }) => {
    if (/^https?:\/\//.test(url)) shell.openExternal(url);
    return { action: "deny" };
  });
  window.webContents.on("will-navigate", (event, url) => {
    if (new URL(url).origin !== origin) {
      event.preventDefault();
      if (/^https?:\/\//.test(url)) shell.openExternal(url);
    }
  });
  window.once("ready-to-show", () => window.show());
  window.webContents.session.setPermissionRequestHandler((_, permission, callback) => {
    callback(permission === "clipboard-sanitized-write" || permission === "fullscreen");
  });
  window.loadURL(origin);
  Menu.setApplicationMenu(Menu.buildFromTemplate([
    { label: "Q 金", submenu: [{ role: "about" }, { type: "separator" }, { role: "hide" }, { role: "hideOthers" }, { role: "unhide" }, { type: "separator" }, { role: "quit" }] },
    { label: "编辑", submenu: [{ role: "undo" }, { role: "redo" }, { type: "separator" }, { role: "cut" }, { role: "copy" }, { role: "paste" }, { role: "selectAll" }] },
    { label: "视图", submenu: [{ role: "reload" }, { role: "resetZoom" }, { role: "zoomIn" }, { role: "zoomOut" }, { role: "togglefullscreen" }] },
    { label: "窗口", submenu: [{ role: "minimize" }, { role: "zoom" }] },
    { label: "帮助", submenu: [
      { label: "使用说明", click: () => shell.openPath(path.join(payload, "source/README-交付说明.md")) },
      { label: "配置豆包工作…", click: () => terminalCommand("configure") },
      { label: "授权豆包工作…", click: () => terminalCommand("authorize") },
      { label: "打开项目数据目录", click: () => shell.openPath(data) },
      { label: "打开运行日志", click: () => shell.openPath(path.join(root, "logs")) },
    ] },
  ]));
}

if (!app.requestSingleInstanceLock()) app.quit();
else {
  app.on("second-instance", () => { if (window) { window.restore(); window.focus(); } });
  app.whenReady().then(async () => {
    fs.mkdirSync(path.join(root, "logs"), { recursive: true, mode: 0o700 });
    fs.mkdirSync(data, { recursive: true, mode: 0o700 });
    await prepareRelay();
    const port = await freePort();
    origin = `http://127.0.0.1:${port}`;
    backend = startProcess(
      path.join(payload, "python/bin/python3.12"),
      [path.join(payload, "source/desktop/backend-entry.py")],
      backendEnv(process.env, { payload, data, port, relayProject }), "backend", data,
    );
    const deadline = Date.now() + 90000;
    while (Date.now() < deadline) {
      const config = await health(`${origin}/api/v1/config`);
      if (isBackendReady(config)) {
        fs.writeFileSync(path.join(root, "running.json"), JSON.stringify({ origin, pid: backend.pid, data, appVersion: app.getVersion() }), { mode: 0o600 });
        createWindow();
        return;
      }
      if (backend.exitCode !== null || backend.signalCode !== null) break;
      await new Promise((resolve) => setTimeout(resolve, 300));
    }
    throw new Error("内置后端未能启动。日志位于 ~/Library/Application Support/QJin/desktop/logs/backend.log");
  }).catch((error) => {
    dialog.showErrorBox("Q 金启动失败", error.message);
    app.quit();
  });
  app.on("window-all-closed", () => app.quit());
  app.on("before-quit", (event) => {
    if (quitting) return;
    event.preventDefault();
    quitting = true;
    Promise.all([stopChild(backend), stopChild(relay)]).finally(() => {
      fs.rmSync(path.join(root, "running.json"), { force: true });
      app.quit();
    });
  });
}
