import { readFileSync } from 'node:fs';
import { Store } from './src/store.mjs';
import { OAuth } from './src/oauth.mjs';
import { createApp } from './src/app.mjs';
import { loadRuntime } from './src/runtime.mjs';

try {
  process.umask(0o077);
  let runtime = await loadRuntime();
  const store = new Store(runtime.databasePath, runtime.encryptionKey);
  const oauth = new OAuth(store, () => runtime.config);
  const openapi = JSON.parse(readFileSync(new URL('./docs/openapi.json', import.meta.url), 'utf8'));
  const app = createApp({ store, oauth, key: runtime.key, openapi, reload: async () => {
    runtime = await loadRuntime();
  } });
  app.on('error', error => {
    console.error(error.code === 'EADDRINUSE' ? 'PORT_8791_IN_USE' : 'SERVER_START_FAILED');
    store.close();
    process.exitCode = 1;
  });
  app.listen(runtime.config.port, '127.0.0.1', () => {
    console.log(`豆包工作本地 API 已启动：http://127.0.0.1:${runtime.config.port}`);
    console.log(`开放平台应用配置：${runtime.config.clientId && runtime.config.clientSecret ? '已配置，等待用户授权' : '待配置'}`);
    console.log('本地 Key 与 App Secret 从 macOS 钥匙串读取；日志不显示秘密。');
  });
  let closing = false;
  const close = () => {
    if (closing) return;
    closing = true;
    app.close(() => { store.close(); process.exit(0); });
    app.closeAllConnections();
    setTimeout(() => process.exit(0), 5000).unref();
  };
  process.on('SIGINT', close);
  process.on('SIGTERM', close);
} catch (error) {
  console.error(error.code ?? 'SERVER_INITIALIZATION_FAILED');
  process.exitCode = 1;
}
