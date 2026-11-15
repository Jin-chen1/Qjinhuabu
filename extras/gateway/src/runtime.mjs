import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { homedir } from 'node:os';
import { join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { mkdirSync, readFileSync, writeFileSync, renameSync, chmodSync } from 'node:fs';
import { digest, secret } from './crypto.mjs';
import { SCOPES } from './oauth.mjs';
import { ApiError } from './errors.mjs';

const run = promisify(execFile);
export const project = resolve(fileURLToPath(new URL('..', import.meta.url)));
const instance = digest(project).slice(0, 16);
export const dataDir = join(homedir(), '.local', 'share', 'doubao-work-api', instance);
export const keychainService = `doubao-work-local-api.${instance}`;
export const configPath = join(dataDir, 'config.json');

export class Keychain {
  async get(name) {
    try {
      const { stdout } = await run('/usr/bin/security', ['find-generic-password',
        '-a', name, '-s', keychainService, '-w'], { maxBuffer: 16 * 1024, timeout: 30_000 });
      return stdout.replace(/\r?\n$/, '');
    } catch (error) {
      if (error.code === 44) return null;
      throw new ApiError('KEYCHAIN_UNAVAILABLE', 503);
    }
  }

  async set(name, value) {
    try {
      // Secrets are generated/read in-process and are never interpolated into shell text.
      await run('/usr/bin/security', ['add-generic-password', '-U',
        '-a', name, '-s', keychainService, '-w', value], { timeout: 30_000 });
    } catch { throw new ApiError('KEYCHAIN_WRITE_FAILED', 503); }
  }
}

export function readConfig() {
  try {
    const c = JSON.parse(readFileSync(configPath, 'utf8'));
    if (!c || c.port !== 8791 || typeof c.clientId !== 'string' ||
        c.redirectUri !== 'http://127.0.0.1:8791/oauth/callback') throw new Error('invalid');
    return { ...c, scopes: SCOPES };
  } catch (error) {
    if (error.code === 'ENOENT') return { port: 8791, clientId: '',
      redirectUri: 'http://127.0.0.1:8791/oauth/callback', scopes: SCOPES };
    throw new ApiError('LOCAL_CONFIG_INVALID', 503);
  }
}

export function writeConfig(config) {
  mkdirSync(dataDir, { recursive: true, mode: 0o700 });
  chmodSync(dataDir, 0o700);
  const temporary = configPath + '.tmp';
  writeFileSync(temporary, JSON.stringify({ port: 8791, clientId: config.clientId,
    redirectUri: 'http://127.0.0.1:8791/oauth/callback', scopes: SCOPES }, null, 2) + '\n', { mode: 0o600 });
  chmodSync(temporary, 0o600);
  renameSync(temporary, configPath);
}

export async function prepare(vault = new Keychain()) {
  if (process.platform !== 'darwin') throw new ApiError('MACOS_KEYCHAIN_REQUIRED', 503);
  mkdirSync(dataDir, { recursive: true, mode: 0o700 });
  chmodSync(dataDir, 0o700);
  if (!await vault.get('master-key')) await vault.set('master-key', secret());
  if (!await vault.get('gateway-key')) await vault.set('gateway-key', 'dw-local-' + secret());
  const config = readConfig();
  writeConfig(config);
  return { prepared: true, configured: Boolean(config.clientId && await vault.get('app-secret')),
    keychainService, configPath, dataDir, url: 'http://127.0.0.1:8791' };
}

export async function loadRuntime(vault = new Keychain()) {
  const config = readConfig();
  const [master, key, appSecret] = await Promise.all([
    vault.get('master-key'), vault.get('gateway-key'), vault.get('app-secret'),
  ]);
  if (!master || !key) throw new ApiError('RUN_PREPARE_FIRST', 503);
  const encryptionKey = Buffer.from(master, 'base64url');
  if (encryptionKey.length !== 32 || !key.startsWith('dw-local-')) throw new ApiError('LOCAL_KEY_INVALID', 503);
  return { config: { ...config, clientSecret: appSecret ?? '' }, key, encryptionKey,
    databasePath: join(dataDir, 'state.db') };
}
