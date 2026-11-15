import { createServer } from 'node:http';
import { equal } from './crypto.mjs';
import { Agent } from './agent.mjs';
import { ApiError } from './errors.mjs';

const MAX_BODY = 29 * 1024 * 1024;

function json(res, status, value) {
  res.writeHead(status, { 'Content-Type': 'application/json; charset=utf-8' });
  res.end(JSON.stringify(value));
}

async function body(req) {
  if (!/^application\/json(?:\s*;|$)/i.test(req.headers['content-type'] ?? '')) {
    throw new ApiError('JSON_CONTENT_TYPE_REQUIRED', 415);
  }
  const parts = [];
  let length = 0;
  for await (const chunk of req) {
    length += chunk.length;
    if (length > MAX_BODY) throw new ApiError('REQUEST_TOO_LARGE', 413);
    parts.push(chunk);
  }
  let parsed;
  try { parsed = JSON.parse(Buffer.concat(parts).toString('utf8')); }
  catch { throw new ApiError('INVALID_JSON'); }
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) throw new ApiError('INVALID_JSON');
  return parsed;
}

const cookieName = 'doubao_oauth_browser';
function oauthCookie(req) {
  const cookies = (req.headers.cookie ?? '').split(';').map(v => v.trim());
  return cookies.find(v => v.startsWith(cookieName + '='))?.slice(cookieName.length + 1) ?? '';
}

export function createApp({ store, oauth, key, fetcher = fetch, openapi = {}, reload }) {
  const agent = new Agent(store, oauth, fetcher);
  const app = createServer(async (req, res) => {
    res.setHeader('Cache-Control', 'no-store');
    res.setHeader('X-Content-Type-Options', 'nosniff');
    res.setHeader('Referrer-Policy', 'no-referrer');
    res.setHeader('Content-Security-Policy', "default-src 'none'; frame-ancestors 'none'");
    try {
      const host = req.headers.host;
      if (!host || ![`127.0.0.1:${req.socket.localPort}`, `localhost:${req.socket.localPort}`].includes(host)) {
        throw new ApiError('INVALID_HOST', 403);
      }
      const origin = `http://${host}`;
      if (req.headers.origin && req.headers.origin !== origin) throw new ApiError('ORIGIN_NOT_ALLOWED', 403);
      if (req.headers['sec-fetch-site'] === 'cross-site' && req.url?.startsWith('/v1/')) {
        throw new ApiError('ORIGIN_NOT_ALLOWED', 403);
      }
      const url = new URL(req.url, origin);
      if (req.method === 'GET' && url.pathname === '/healthz') {
        json(res, 200, { status: 'ok', service: 'doubao-work-local-api', ...oauth.status() });
        return;
      }
      if (req.method === 'GET' && url.pathname === '/') {
        res.writeHead(200, { 'Content-Type': 'text/plain; charset=utf-8' });
        res.end('豆包工作本地 API 服务\n\n健康检查：/healthz\n接口合同：/openapi.json\n\n在项目目录执行 npm run configure，再执行 npm run authorize。\n调用 Key：在自己的终端执行 npm run key。\n本服务使用豆包工作会话协议，尚非 OpenAI 模型网关。\n');
        return;
      }
      if (req.method === 'GET' && url.pathname === '/openapi.json') {
        json(res, 200, openapi);
        return;
      }
      if (req.method === 'GET' && url.pathname === '/oauth/start') {
        const ticket = url.searchParams.get('ticket');
        if (!ticket || ticket.length > 128) throw new ApiError('OAUTH_TICKET_INVALID', 403);
        const started = store.consumeTicket(ticket);
        res.setHeader('Set-Cookie', `${cookieName}=${started.cookie}; HttpOnly; SameSite=Lax; Path=/oauth; Max-Age=600`);
        res.writeHead(302, { Location: started.authorizationUrl });
        res.end();
        return;
      }
      if (req.method === 'GET' && url.pathname === '/oauth/callback') {
        await oauth.callback(url.searchParams.get('state'), url.searchParams.get('code'), oauthCookie(req));
        res.setHeader('Set-Cookie', `${cookieName}=; HttpOnly; SameSite=Lax; Path=/oauth; Max-Age=0`);
        res.writeHead(200, { 'Content-Type': 'text/plain; charset=utf-8' });
        res.end('豆包工作授权已保存到本机。可以关闭此页面。\n实际 API 权限及生成能力还需通过真实请求验证。');
        return;
      }
      if (!equal(req.headers.authorization, `Bearer ${key}`)) throw new ApiError('API_KEY_REQUIRED', 401);
      if (req.method === 'GET' && url.pathname === '/v1/auth/status') {
        json(res, 200, oauth.status());
        return;
      }
      if (req.method === 'POST' && url.pathname === '/v1/oauth/start') {
        await body(req);
        const started = oauth.begin();
        json(res, 200, { path: started.path, expiresIn: started.expiresIn });
        return;
      }
      if (req.method === 'POST' && url.pathname === '/v1/config/reload' && reload) {
        await body(req);
        await reload();
        json(res, 200, { reloaded: true, ...oauth.status() });
        return;
      }
      if (req.method === 'DELETE' && url.pathname === '/v1/auth') {
        store.clearGrant();
        json(res, 200, { disconnected: true, platformTokenRevoked: false });
        return;
      }
      if (['/v1/models', '/v1/chat/completions', '/v1/videos/generations'].includes(url.pathname)) {
        throw new ApiError('OPENAI_PROTOCOL_NOT_IMPLEMENTED', 501);
      }
      let result;
      if (req.method === 'POST' && url.pathname === '/v1/sessions') result = await agent.createSession(await body(req));
      else if (req.method === 'POST' && url.pathname === '/v1/files') result = await agent.upload(await body(req));
      else {
        const match = /^\/v1\/sessions\/([a-zA-Z0-9_-]+)\/(events|messages)$/.exec(url.pathname);
        if (!match) throw new ApiError('ROUTE_NOT_FOUND', 404);
        if (req.method === 'POST' && match[2] === 'events') result = await agent.events(match[1], await body(req));
        else if (req.method === 'GET' && match[2] === 'messages') result = await agent.messages(match[1], url.searchParams);
        else throw new ApiError('METHOD_NOT_ALLOWED', 405);
      }
      json(res, 200, { data: result });
    } catch (error) {
      const safe = error instanceof ApiError ? error : new ApiError('INTERNAL_ERROR', 500);
      if (!res.headersSent) json(res, safe.status, { error: {
        code: safe.code, outcomeUnknown: safe.outcomeUnknown,
        ...(safe.outcomeUnknown ? { guidance: '结果未知，保留原 UUID；查询现有消息或人工核验，禁止自动重发生成请求。' } : {}),
      } });
      else res.end();
    }
  });
  app.requestTimeout = 60_000;
  app.headersTimeout = 10_000;
  return app;
}
