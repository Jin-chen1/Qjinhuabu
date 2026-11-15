import test from 'node:test';
import assert from 'node:assert/strict';
import { randomBytes, createPublicKey, verify } from 'node:crypto';
import { mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { seal, unseal, newDpopKey, dpopProof, digest } from '../src/crypto.mjs';
import { Store } from '../src/store.mjs';
import { OAuth } from '../src/oauth.mjs';
import { createApp } from '../src/app.mjs';

function fixture(t, fetcher, overrides = {}) {
  const dir = mkdtempSync(join(tmpdir(), 'doubao-api-test-'));
  const store = new Store(join(dir, 'state.db'), randomBytes(32));
  const config = {
    clientId: 'cli_test', clientSecret: 'test-secret-not-live',
    redirectUri: 'http://127.0.0.1:8791/oauth/callback',
    scopes: ['doubao:session:write', 'offline_access'], ...overrides,
  };
  const oauth = new OAuth(store, () => config, fetcher);
  t.after(() => { store.close(); rmSync(dir, { recursive: true, force: true }); });
  return { store, oauth, config, dir };
}

const answer = () => new Response(JSON.stringify({
  code: 0, data: { access_token: 'access-test', refresh_token: 'refresh-test',
    token_type: 'DPoP', expires_in: 7200, refresh_token_expires_in: 86400 },
}), { status: 200 });

async function authorize(oauth, store) {
  const start = oauth.begin();
  const browser = store.consumeTicket(start.ticket);
  await oauth.callback(browser.state, 'code-test', browser.cookie);
  return browser;
}

test('AES-GCM rejects another connection context and never stores plaintext', () => {
  const key = randomBytes(32);
  const value = seal({ accessToken: 'secret-sample' }, key, 'owner-A');
  assert.equal(unseal(value, key, 'owner-A').accessToken, 'secret-sample');
  assert.throws(() => unseal(value, key, 'owner-B'));
  assert.ok(!value.includes('secret-sample'));
});

test('DPoP proof verifies and binds method, URL, nonce and access token hash', () => {
  const jwk = newDpopKey();
  const token = dpopProof(jwk, 'GET', 'https://example.test/messages?order=desc', 'token-test', 'nonce-test');
  const [header, payload, signature] = token.split('.');
  const h = JSON.parse(Buffer.from(header, 'base64url'));
  const p = JSON.parse(Buffer.from(payload, 'base64url'));
  assert.equal(h.typ, 'dpop+jwt');
  assert.equal(h.jwk.d, undefined);
  assert.equal(p.htm, 'GET');
  assert.equal(p.htu, 'https://example.test/messages');
  assert.equal(p.nonce, 'nonce-test');
  assert.equal(p.ath, digest('token-test', 'base64url'));
  assert.ok(verify('sha256', Buffer.from(`${header}.${payload}`),
    { key: createPublicKey({ key: h.jwk, format: 'jwk' }), dsaEncoding: 'ieee-p1363' },
    Buffer.from(signature, 'base64url')));
});

test('OAuth binds one-use ticket, browser cookie and PKCE verifier', async t => {
  let tokenBody;
  const { store, oauth, dir } = fixture(t, async (url, init) => {
    tokenBody = JSON.parse(init.body);
    return answer();
  });
  const start = oauth.begin();
  const browser = store.consumeTicket(start.ticket);
  assert.throws(() => store.consumeTicket(start.ticket), /OAUTH_TICKET_INVALID/);
  await assert.rejects(oauth.callback(browser.state, 'code-test', 'wrong-cookie'), /OAUTH_STATE_INVALID/);
  const url = new URL(browser.authorizationUrl);
  assert.equal(url.searchParams.get('code_challenge_method'), 'S256');
  await oauth.callback(browser.state, 'code-test', browser.cookie);
  assert.equal(digest(tokenBody.code_verifier, 'base64url'), url.searchParams.get('code_challenge'));
  await assert.rejects(oauth.callback(browser.state, 'code-test', browser.cookie), /OAUTH_STATE_INVALID/);
  assert.ok(!readFileSync(join(dir, 'state.db')).includes(Buffer.from('access-test')));
  assert.equal(oauth.status().connected, true);
});

test('expired state never exchanges an authorization code', async t => {
  let calls = 0;
  const { store, oauth } = fixture(t, async () => { calls++; return answer(); });
  const start = oauth.begin();
  const browser = store.consumeTicket(start.ticket);
  store.db.prepare('UPDATE oauth_states SET expires_at = 0').run();
  await assert.rejects(oauth.callback(browser.state, 'code-test', browser.cookie), /OAUTH_STATE_INVALID/);
  assert.equal(calls, 0);
});

test('concurrent expired-token calls perform only one refresh', async t => {
  let calls = 0;
  const { store, oauth } = fixture(t, async () => { calls++; return answer(); });
  await authorize(oauth, store);
  const grant = store.grant();
  store.saveGrant({ ...grant, accessExpiresAt: 0 });
  const responses = await Promise.all(Array.from({ length: 8 }, () =>
    oauth.authorization('GET', 'https://open.doubao.com/open-apis/agent/v1/sessions')));
  assert.equal(calls, 2);
  assert.ok(responses.every(r => r.Authorization === 'DPoP access-test' && r.DPoP));
});

test('refresh timeout requires new authorization and never retries rotating token', async t => {
  let calls = 0;
  const { store, oauth } = fixture(t, async () => {
    calls++;
    if (calls === 1) return answer();
    throw new Error('private upstream content');
  });
  await authorize(oauth, store);
  store.saveGrant({ ...store.grant(), accessExpiresAt: 0 });
  await assert.rejects(oauth.authorization('GET', 'https://example.test'), /OAUTH_REAUTHORIZE_REQUIRED/);
  await assert.rejects(oauth.authorization('GET', 'https://example.test'), /OAUTH_REAUTHORIZE_REQUIRED/);
  assert.equal(calls, 2);
});

test('disconnect during token refresh cannot resurrect the local grant', async t => {
  let release;
  let began;
  const started = new Promise(resolve => { began = resolve; });
  let calls = 0;
  const { store, oauth } = fixture(t, async () => {
    calls++;
    if (calls === 1) return answer();
    began();
    await new Promise(resolve => { release = resolve; });
    return answer();
  });
  await authorize(oauth, store);
  store.saveGrant({ ...store.grant(), accessExpiresAt: 0 });
  const pending = oauth.authorization('GET', 'https://example.test');
  await started;
  store.clearGrant();
  release();
  await assert.rejects(pending, /OAUTH_REAUTHORIZE_REQUIRED/);
  assert.equal(store.grant(), null);
});

test('disconnect during authorization exchange invalidates the pending callback', async t => {
  let release;
  let began;
  const started = new Promise(resolve => { began = resolve; });
  const { store, oauth } = fixture(t, async () => {
    began();
    await new Promise(resolve => { release = resolve; });
    return answer();
  });
  const first = oauth.begin();
  const browser = store.consumeTicket(first.ticket);
  const pending = oauth.callback(browser.state, 'code-test', browser.cookie);
  await started;
  store.clearGrant();
  release();
  await assert.rejects(pending, /OAUTH_STATE_INVALID/);
  assert.equal(store.grant(), null);
});

test('token exchange retries only an explicit DPoP nonce rejection', async t => {
  let calls = 0;
  const { store, oauth } = fixture(t, async (url, init) => {
    calls++;
    const proof = JSON.parse(Buffer.from(init.headers.DPoP.split('.')[1], 'base64url'));
    if (calls === 1) {
      assert.equal(proof.nonce, undefined);
      return new Response('{}', { status: 401, headers: { 'dpop-nonce': 'expected-nonce' } });
    }
    assert.equal(proof.nonce, 'expected-nonce');
    return answer();
  });
  await authorize(oauth, store);
  assert.equal(calls, 2);
});

async function serve(t, fetcher, overrides = {}) {
  const f = fixture(t, fetcher, overrides);
  const key = 'local-test-key-' + 'x'.repeat(32);
  const app = createApp({ ...f, key, fetcher });
  await new Promise(resolve => app.listen(0, '127.0.0.1', resolve));
  const base = `http://127.0.0.1:${app.address().port}`;
  t.after(() => new Promise(resolve => { app.close(resolve); app.closeAllConnections(); }));
  const call = async (path, body, extra = {}) => {
    const res = await fetch(base + path, {
      method: body === undefined ? 'GET' : 'POST',
      headers: { Authorization: `Bearer ${key}`, ...(body === undefined ? {} : { 'Content-Type': 'application/json' }),
        ...extra },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    });
    return { status: res.status, body: await res.json(), headers: res.headers };
  };
  return { ...f, app, key, base, call };
}

test('HTTP rejects missing key and hostile browser origins before any upstream call', async t => {
  let calls = 0;
  const f = await serve(t, async () => { calls++; return answer(); });
  const noKey = await fetch(f.base + '/v1/auth/status');
  assert.equal(noKey.status, 401);
  const attack = await f.call('/v1/oauth/start', {}, { Origin: 'https://attacker.test' });
  assert.equal(attack.status, 403);
  assert.equal(calls, 0);
  assert.equal((await f.call('/v1/auth/status')).body.connected, false);
});

test('HTTP reports missing app configuration without exposing credentials', async t => {
  const f = await serve(t, async () => answer(), { clientId: '', clientSecret: '' });
  const result = await f.call('/v1/oauth/start', {});
  assert.equal(result.status, 503);
  assert.equal(result.body.error.code, 'OAUTH_NOT_CONFIGURED');
});

test('callback requires original browser cookie and consumes state once', async t => {
  let calls = 0;
  const f = await serve(t, async () => { calls++; return answer(); });
  const start = await f.call('/v1/oauth/start', {});
  const visit = await fetch(f.base + start.body.path, { redirect: 'manual' });
  assert.equal(visit.status, 302);
  const authUrl = new URL(visit.headers.get('location'));
  const cookie = visit.headers.get('set-cookie').split(';')[0];
  const callback = '/oauth/callback?state=' + authUrl.searchParams.get('state') + '&code=code-test';
  const missing = await fetch(f.base + callback);
  assert.equal(missing.status, 403);
  const ok = await fetch(f.base + callback, { headers: { Cookie: cookie } });
  assert.equal(ok.status, 200);
  const replay = await fetch(f.base + callback, { headers: { Cookie: cookie } });
  assert.equal(replay.status, 403);
  assert.equal(calls, 1);
});

test('sessions, events and messages preserve owner isolation and durable idempotency', async t => {
  let createCalls = 0;
  let eventCalls = 0;
  const f = await serve(t, async (url, init) => {
    if (url.includes('/oauth/token')) return answer();
    if (url.endsWith('/sessions')) {
      createCalls++;
      return new Response(JSON.stringify({ code: 0, data: { id: 'session-test' } }));
    }
    if (url.endsWith('/events')) {
      eventCalls++;
      return new Response(JSON.stringify({ code: 0, data: {
        messages: [{ id: 'message-test', role: 'user', session_id: 'session-test' }],
      } }));
    }
    return new Response(JSON.stringify({ code: 0, data: { messages: [], has_more: false } }));
  });
  const body = { uuid: 'session-request-1', name: '测试会话' };
  assert.equal((await f.call('/v1/sessions', body)).status, 409);
  await authorize(f.oauth, f.store);
  assert.equal((await f.call('/v1/sessions', body)).status, 200);
  assert.equal((await f.call('/v1/sessions', body)).status, 200);
  assert.equal(createCalls, 1);
  assert.equal((await f.call('/v1/sessions', { ...body, name: 'changed' })).status, 409);
  const event = { uuid: 'event-request-1', text: '你好' };
  assert.equal((await f.call('/v1/sessions/session-test/events', event)).status, 200);
  assert.equal((await f.call('/v1/sessions/session-test/events', event)).status, 200);
  assert.equal(eventCalls, 1);
  assert.equal((await f.call('/v1/sessions/another/events', event)).status, 404);
  assert.equal((await f.call('/v1/sessions/another/messages')).status, 404);
  assert.equal((await f.call('/v1/sessions/session-test/messages')).status, 200);
});

test('unknown write result cannot be resubmitted even after a database reopen', async t => {
  let calls = 0;
  const f = await serve(t, async url => {
    if (url.includes('/oauth/token')) return answer();
    calls++;
    throw new Error('secret timeout response');
  });
  await authorize(f.oauth, f.store);
  const body = { uuid: 'request-unknown', name: 'Unknown' };
  const first = await f.call('/v1/sessions', body);
  assert.equal(first.status, 502);
  assert.equal(first.body.error.outcomeUnknown, true);
  const again = await f.call('/v1/sessions', body);
  assert.equal(again.status, 409);
  assert.equal(again.body.error.code, 'REQUEST_OUTCOME_UNKNOWN');
  assert.equal(calls, 1);
  assert.ok(!JSON.stringify(first.body).includes('secret timeout'));
  const second = new Store(join(f.dir, 'state.db'), f.store.key);
  t.after(() => second.close());
  assert.throws(() => second.reserve('POST /sessions', body.uuid, body), /REQUEST_OUTCOME_UNKNOWN/);
});

test('file uploads reject untrusted URIs, fake image signatures and invalid base64', async t => {
  let uploads = 0;
  const f = await serve(t, async url => {
    if (url.includes('/oauth/token')) return answer();
    if (url.endsWith('/files')) {
      uploads++;
      return new Response(JSON.stringify({ code: 0, data: { uri: 'file-owned' } }));
    }
    return new Response(JSON.stringify({ code: 0, data: { id: 's' } }));
  });
  await authorize(f.oauth, f.store);
  await f.call('/v1/sessions', { uuid: 'file-session', name: 'Files' });
  const invalid = await f.call('/v1/files', { uuid: 'bad-file', filename: 'a.png', contentBase64: 'aGVsbG8=' });
  assert.equal(invalid.status, 400);
  const badBase64 = await f.call('/v1/files', { uuid: 'bad-base64', filename: 'a.png', contentBase64: '!!!' });
  assert.equal(badBase64.status, 400);
  const valid = await f.call('/v1/files', { uuid: 'image-file', filename: 'a.png',
    contentBase64: 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aXZkAAAAASUVORK5CYII=' });
  assert.equal(valid.status, 200);
  assert.equal(uploads, 1);
  const foreign = await f.call('/v1/sessions/s/events', { uuid: 'foreign-file', text: 'look',
    files: [{ uri: 'file-not-owned', name: 'a.png' }] });
  assert.equal(foreign.status, 403);
});
