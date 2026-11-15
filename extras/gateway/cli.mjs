import { createInterface } from 'node:readline/promises';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { randomUUID } from 'node:crypto';
import { Keychain, prepare, loadRuntime, writeConfig, readConfig } from './src/runtime.mjs';
import { Store } from './src/store.mjs';
import { SCOPES } from './src/oauth.mjs';
import { ApiError } from './src/errors.mjs';

const run = promisify(execFile);
const base = 'http://127.0.0.1:8791';

async function hidden(prompt) {
  if (!process.stdin.isTTY) throw new ApiError('CONFIGURE_REQUIRES_LOCAL_TERMINAL');
  process.stdout.write(prompt);
  process.stdin.setRawMode(true);
  process.stdin.setEncoding('utf8');
  process.stdin.resume();
  return new Promise((resolve, reject) => {
    let result = '';
    const finish = () => {
      process.stdin.off('data', onData);
      process.stdin.setRawMode(false);
      process.stdin.pause();
      process.stdout.write('\n');
    };
    const onData = chunk => {
      for (const char of chunk) {
        if (char === '\u0003') { finish(); reject(new ApiError('CONFIGURATION_CANCELLED')); return; }
        if (char === '\r' || char === '\n') { finish(); resolve(result); return; }
        if (char === '\u007f' || char === '\b') { result = result.slice(0, -1); continue; }
        if (char >= ' ') result += char;
      }
    };
    process.stdin.on('data', onData);
  });
}

async function request(path, body, method) {
  const runtime = await loadRuntime();
  let res;
  try {
    res = await fetch(base + path, { method: method ?? (body ? 'POST' : 'GET'),
      redirect: 'error', signal: AbortSignal.timeout(15_000),
      headers: { Authorization: `Bearer ${runtime.key}`, ...(body ? { 'Content-Type': 'application/json' } : {}) },
      ...(body ? { body: JSON.stringify(body) } : {}) });
  } catch { throw new ApiError('LOCAL_SERVER_UNAVAILABLE'); }
  const value = await res.json();
  if (!res.ok) throw new ApiError(value.error?.code ?? 'LOCAL_REQUEST_FAILED');
  return value;
}

try {
  const command = process.argv[2];
  const vault = new Keychain();
  if (command === 'prepare') console.log(JSON.stringify(await prepare(vault), null, 2));
  else if (command === 'configure') {
    await prepare(vault);
    if (!process.stdin.isTTY) throw new ApiError('CONFIGURE_REQUIRES_LOCAL_TERMINAL');
    const input = createInterface({ input: process.stdin, output: process.stdout });
    const clientId = (await input.question('开放平台 App ID（cli_ 开头）：')).trim();
    input.close();
    if (!/^cli_[a-zA-Z0-9]+$/.test(clientId)) throw new ApiError('INVALID_APP_ID');
    const appSecret = (await hidden('App Secret（隐藏输入，粘贴后按回车）：')).trim();
    if (appSecret.length < 8 || appSecret.length > 4096) throw new ApiError('INVALID_APP_SECRET');
    const old = await loadRuntime(vault);
    const store = new Store(old.databasePath, old.encryptionKey);
    try { store.clearGrant(); } finally { store.close(); }
    await vault.set('app-secret', appSecret);
    writeConfig({ ...readConfig(), clientId });
    console.log('配置已保存。请在开放平台登记回调：http://127.0.0.1:8791/oauth/callback');
    console.log('所需权限：' + SCOPES.join(', '));
    try {
      await request('/v1/config/reload', {});
      console.log('运行中的 API 服务已重新加载配置，现在可执行 npm run authorize。');
    } catch {
      console.log('请启动 API 服务，再执行 npm run authorize。');
    }
  } else if (command === 'key') {
    const key = await vault.get('gateway-key');
    if (!key) throw new ApiError('RUN_PREPARE_FIRST');
    console.log(key);
  } else if (command === 'authorize') {
    const result = await request('/v1/oauth/start', {});
    await run('/usr/bin/open', [base + result.path]);
    console.log('已打开用户授权页面，请本人完成授权。回调后执行 npm run status。');
  } else if (command === 'status') console.log(JSON.stringify(await request('/v1/auth/status'), null, 2));
  else if (command === 'disconnect') {
    console.log(JSON.stringify(await request('/v1/auth', undefined, 'DELETE'), null, 2));
  } else if (command === 'create-session') {
    console.log(JSON.stringify(await request('/v1/sessions', { uuid: randomUUID(), name: process.argv[3] || '本地 API 会话' }), null, 2));
  } else {
    console.log('用法：node cli.mjs prepare | configure | key | authorize | status | disconnect | create-session [名称]');
  }
} catch (error) {
  console.error(error.code ?? 'CLI_FAILED');
  process.exitCode = 1;
}
