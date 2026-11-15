const { test } = require("node:test");
const assert = require("node:assert/strict");
const net = require("node:net");
const { spawn } = require("node:child_process");
const { once } = require("node:events");
const runtime = require("./runtime.cjs");

test("desktop backend starts without inheriting model keys or development paths", () => {
  assert.equal(typeof runtime.backendEnv, "function");
  const env = runtime.backendEnv(
    { HOME: "/home/test", PATH: "/external/bin", MODEL_API_KEY: "private", PYTHONPATH: "/external", ST_EDITION: "ee" },
    { payload: "/bundle", data: "/data", port: 18780, relayProject: "/relay" },
  );
  assert.equal(env.MODEL_API_KEY, undefined);
  assert.equal(env.ST_EDITION, "ce");
  assert.equal(env.PYTHONPATH, "/bundle/source/dramaclaw/src");
  assert.equal(env.NOVELVIDEO_DATA_ROOT, "/data");
  assert.equal(env.PYTHON_DOTENV_DISABLED, "1");
  assert.equal(env.QJIN_DOUBAO_PROJECT, "/relay");
});

test("API readiness follows the actual CE response envelope", () => {
  assert.equal(typeof runtime.isBackendReady, "function");
  assert.equal(runtime.isBackendReady({ ok: true, data: { edition: "ce", auth_required: false } }), true);
  assert.equal(runtime.isBackendReady({ ok: false, data: { edition: "ce" } }), false);
  assert.equal(runtime.isBackendReady({ ok: true, data: { edition: "ee" } }), false);
});

test("dependency databases and caches stay outside the signed app bundle", () => {
  const env = runtime.backendEnv({ HOME: "/home/test" }, { payload: "/bundle", data: "/data", port: 18780, relayProject: "/relay" });
  assert.equal(env.SYSTEM_ROOT_DIRECTORY, "/data/runtime/cognee/system");
  assert.equal(env.DATA_ROOT_DIRECTORY, "/data/runtime/cognee/data");
  assert.equal(env.CACHE_ROOT_DIRECTORY, "/data/runtime/cognee/cache");
  assert.equal(env.LOGS_ROOT_DIRECTORY, "/data/runtime/cognee/logs");
});

test("occupied ports are skipped without taking over the existing service", async () => {
  assert.equal(typeof runtime.freePort, "function");
  const server = net.createServer();
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  const occupied = server.address().port;
  try {
    const selected = await runtime.freePort(occupied);
    assert.notEqual(selected, occupied);
    assert.equal(server.listening, true);
  } finally {
    server.close();
    await once(server, "close");
  }
});

test("cleanup terminates only its own live child", async () => {
  assert.equal(typeof runtime.stopChild, "function");
  const owned = spawn(process.execPath, ["-e", "setInterval(() => {}, 1000)"]);
  const other = spawn(process.execPath, ["-e", "setInterval(() => {}, 1000)"]);
  await Promise.all([once(owned, "spawn"), once(other, "spawn")]);
  try {
    await runtime.stopChild(owned);
    assert.notEqual(owned.signalCode, null);
    assert.equal(other.exitCode, null);
    assert.equal(other.signalCode, null);
  } finally {
    other.kill("SIGTERM");
    await once(other, "exit");
  }
});
