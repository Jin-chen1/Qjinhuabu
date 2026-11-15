const net = require("node:net");
const path = require("node:path");

function backendEnv(parent, { payload, data, port, relayProject }) {
  const env = {};
  for (const key of ["HOME", "USER", "LOGNAME", "TMPDIR", "LANG", "LC_ALL"]) {
    if (parent[key]) env[key] = parent[key];
  }
  return {
    ...env,
    PATH: `${path.join(payload, "bin")}:${path.join(env.HOME || "", ".local/bin")}:/usr/bin:/bin:/usr/sbin:/sbin`,
    PYTHONHOME: path.join(payload, "python"),
    PYTHONPATH: path.join(payload, "source/dramaclaw/src"),
    PYTHONNOUSERSITE: "1",
    PYTHONDONTWRITEBYTECODE: "1",
    PYTHONUNBUFFERED: "1",
    PYTHON_DOTENV_DISABLED: "1",
    ST_EDITION: "ce",
    ST_CONTROL_PLANE_DSN: "",
    ST_REDIS_URL: "",
    ST_CELERY_BROKER_URL: "",
    ST_CELERY_RESULT_BACKEND: "",
    SUPERTALE_ENV: "development",
    DRAMACLAW_CHAT_BACKEND: "codex",
    CODEX_BIN: path.join(payload, "bin/codex"),
    NOVELVIDEO_DATA_ROOT: data,
    NOVELVIDEO_STATE_DIR: path.join(data, "state"),
    NOVELVIDEO_OUTPUT_DIR: path.join(data, "output"),
    NOVELVIDEO_RUNTIME_DIR: path.join(data, "runtime"),
    SYSTEM_ROOT_DIRECTORY: path.join(data, "runtime/cognee/system"),
    DATA_ROOT_DIRECTORY: path.join(data, "runtime/cognee/data"),
    CACHE_ROOT_DIRECTORY: path.join(data, "runtime/cognee/cache"),
    LOGS_ROOT_DIRECTORY: path.join(data, "runtime/cognee/logs"),
    COGNEE_LOGS_DIR: path.join(data, "runtime/cognee/logs"),
    NOVELVIDEO_API_HOST: "127.0.0.1",
    NOVELVIDEO_API_PORT: String(port),
    NOVELVIDEO_API_URL: `http://127.0.0.1:${port}`,
    DRAMACLAW_API_URL: `http://127.0.0.1:${port}`,
    SUPERTALE_API_URL: `http://127.0.0.1:${port}`,
    DRAMACLAW_FRONTEND_DIST: path.join(payload, "frontend"),
    QJIN_DOUBAO_PROJECT: relayProject,
    QJIN_MEDIA_LIBRARY_ENABLED: "1",
  };
}

async function freePort(start = 18780) {
  for (let port = start; port < start + 100; port++) {
    const available = await new Promise((resolve) => {
      const server = net.createServer();
      server.once("error", () => resolve(false));
      server.listen(port, "127.0.0.1", () => server.close(() => resolve(true)));
    });
    if (available) return port;
  }
  throw new Error("未找到可用的本机端口");
}

async function stopChild(child) {
  if (!child || child.exitCode !== null || child.signalCode !== null) return;
  await new Promise((resolve) => {
    const timer = setTimeout(() => child.kill("SIGKILL"), 5000);
    child.once("exit", () => {
      clearTimeout(timer);
      resolve();
    });
    child.kill("SIGTERM");
  });
}

function isBackendReady(config) {
  return config?.ok === true && config.data?.edition === "ce";
}

module.exports = { backendEnv, freePort, stopChild, isBackendReady };
