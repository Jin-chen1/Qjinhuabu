import { digest, dpopProof } from './crypto.mjs';
import { ApiError } from './errors.mjs';

export const API_ROOT = 'https://open.doubao.com/open-apis/agent/v1';
const MAX_IMAGE_BYTES = 20 * 1024 * 1024;
const MAX_RESPONSE_BYTES = 10 * 1024 * 1024;

export function identifier(value) {
  if (typeof value !== 'string' || !/^[a-zA-Z0-9_-]{1,256}$/.test(value)) {
    throw new ApiError('INVALID_IDENTIFIER');
  }
  return value;
}

async function responseJson(response) {
  if (Number(response.headers.get('content-length')) > MAX_RESPONSE_BYTES) {
    await response.body?.cancel();
    throw new ApiError('UPSTREAM_RESPONSE_TOO_LARGE', 502);
  }
  const parts = [];
  let size = 0;
  for await (const chunk of response.body) {
    size += chunk.length;
    if (size > MAX_RESPONSE_BYTES) throw new ApiError('UPSTREAM_RESPONSE_TOO_LARGE', 502);
    parts.push(chunk);
  }
  return JSON.parse(Buffer.concat(parts).toString('utf8'));
}

export class Agent {
  constructor(store, oauth, fetcher = fetch) {
    this.store = store;
    this.oauth = oauth;
    this.fetcher = fetcher;
  }

  async request(method, path, body, grant) {
    const url = API_ROOT + path;
    let nonce;
    for (let attempt = 0; attempt < 2; attempt++) {
      const multipart = body instanceof FormData;
      let response;
      try {
        response = await this.fetcher(url, {
          method, redirect: 'error', signal: AbortSignal.timeout(30_000),
          headers: { Authorization: `${grant.tokenType} ${grant.accessToken}`,
            ...(grant.tokenType === 'DPoP' ? { DPoP: dpopProof(grant.privateJwk, method, url, grant.accessToken, nonce) } : {}),
            ...(multipart || body === undefined ? {} : { 'Content-Type': 'application/json' }) },
          ...(body === undefined ? {} : { body: multipart ? body : JSON.stringify(body) }),
        });
      } catch { throw new ApiError('UPSTREAM_TRANSPORT_ERROR', 502, method === 'POST'); }
      const headerNonce = response.headers.get('dpop-nonce');
      if (attempt === 0 && response.status === 401 && grant.tokenType === 'DPoP' && headerNonce) {
        await response.body?.cancel();
        nonce = headerNonce;
        continue;
      }
      let envelope;
      try { envelope = await responseJson(response); }
      catch { throw new ApiError('UPSTREAM_RESPONSE_INVALID', 502, method === 'POST' && response.ok); }
      if (!response.ok || envelope.code !== 0) {
        // No upstream error message is relayed: it may include credentials or user content.
        const code = Number.isInteger(envelope.code) ? envelope.code : response.status;
        throw new ApiError(`UPSTREAM_${code}`, response.status === 429 ? 429 : 502,
          method === 'POST' && (response.status >= 500 || response.status === 409 || response.ok));
      }
      if (!envelope.data || typeof envelope.data !== 'object' || Array.isArray(envelope.data)) {
        throw new ApiError('UPSTREAM_RESPONSE_INVALID', 502, method === 'POST');
      }
      return envelope.data;
    }
    throw new ApiError('UPSTREAM_DPOP_NONCE_REJECTED', 502, method === 'POST');
  }

  async write(path, uuid, ledgerBody, requestBody, validate) {
    identifier(uuid);
    const grant = await this.oauth.usableGrant();
    const route = `POST ${path}`;
    const reservation = this.store.reserve(route, uuid, ledgerBody);
    if (reservation.cached) return reservation.result;
    try {
      const result = await this.request('POST', path, requestBody, grant);
      const owned = validate(result);
      this.store.complete(route, uuid, reservation.scope, result, owned);
      return result;
    } catch (error) {
      const known = error instanceof ApiError;
      const unknown = !known || error.outcomeUnknown;
      this.store.fail(route, uuid, reservation.scope, unknown);
      throw known ? error : new ApiError('UPSTREAM_RESULT_INVALID', 502, true);
    }
  }

  async createSession(body) {
    identifier(body.uuid);
    if (typeof body.name !== 'string' || !body.name.trim() || body.name.length > 256) {
      throw new ApiError('INVALID_SESSION_NAME');
    }
    const payload = { uuid: body.uuid, name: body.name };
    return this.write('/sessions', body.uuid, payload, payload, result => {
      try { return { sessionId: identifier(result.id) }; }
      catch { throw new ApiError('UPSTREAM_SESSION_ID_MISSING', 502, true); }
    });
  }

  assertSession(sessionId) {
    identifier(sessionId);
    if (!this.store.ownsSession(sessionId)) throw new ApiError('SESSION_NOT_FOUND', 404);
  }

  async events(sessionId, body) {
    this.assertSession(sessionId);
    identifier(body.uuid);
    if (typeof body.text !== 'string' || !body.text.trim() || body.text.length > 65535) {
      throw new ApiError('INVALID_MESSAGE_TEXT');
    }
    const files = body.files ?? [];
    if (!Array.isArray(files) || files.length > 9) throw new ApiError('INVALID_FILES');
    const contents = [{ type: 'text', text: body.text }];
    for (const item of files) {
      if (!item || typeof item.uri !== 'string' || !this.store.ownsFile(item.uri)) {
        throw new ApiError('FILE_NOT_OWNED', 403);
      }
      if (typeof item.name !== 'string' || !item.name || item.name.length > 256) throw new ApiError('INVALID_FILENAME');
      contents.push({ type: 'file', uri: item.uri, name: item.name });
    }
    const payload = { uuid: body.uuid, events: [{ type: 'user.message', contents }] };
    return this.write(`/sessions/${encodeURIComponent(sessionId)}/events`, body.uuid, payload, payload, result => {
      if (!Array.isArray(result.messages) || result.messages.length !== 1 ||
          result.messages[0]?.role !== 'user' || result.messages[0]?.session_id !== sessionId) {
        throw new ApiError('UPSTREAM_MESSAGE_ID_MISSING', 502, true);
      }
      try { identifier(result.messages[0].id); }
      catch { throw new ApiError('UPSTREAM_MESSAGE_ID_MISSING', 502, true); }
    });
  }

  async messages(sessionId, query) {
    this.assertSession(sessionId);
    const pageSize = query.get('page_size') ?? '100';
    const order = query.get('order') ?? 'desc';
    if (!/^\d+$/.test(pageSize) || Number(pageSize) < 1 || Number(pageSize) > 100 ||
        !['asc', 'desc'].includes(order)) throw new ApiError('INVALID_PAGINATION');
    const params = new URLSearchParams({ page_size: pageSize, order });
    if (query.has('page_token')) {
      const token = query.get('page_token');
      if (!token || token.length > 2048) throw new ApiError('INVALID_PAGE_TOKEN');
      params.set('page_token', token);
    }
    const grant = await this.oauth.usableGrant();
    const result = await this.request('GET', `/sessions/${encodeURIComponent(sessionId)}/messages?${params}`, undefined, grant);
    if (!Array.isArray(result.messages) || typeof result.has_more !== 'boolean') {
      throw new ApiError('UPSTREAM_MESSAGES_INVALID', 502);
    }
    return result;
  }

  async upload(body) {
    identifier(body.uuid);
    if (typeof body.filename !== 'string' || !/^[^/\\\x00-\x1f]{1,256}$/.test(body.filename) ||
        typeof body.contentBase64 !== 'string' || !body.contentBase64 ||
        body.contentBase64.length > Math.ceil(MAX_IMAGE_BYTES / 3) * 4 ||
        !/^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/.test(body.contentBase64)) {
      throw new ApiError('INVALID_FILE_INPUT');
    }
    const bytes = Buffer.from(body.contentBase64, 'base64');
    if (!bytes.length || bytes.length > MAX_IMAGE_BYTES || bytes.toString('base64') !== body.contentBase64) {
      throw new ApiError('INVALID_FILE_INPUT');
    }
    let mime;
    if (bytes.length > 32 && bytes.subarray(0, 8).equals(Buffer.from('89504e470d0a1a0a', 'hex')) &&
        bytes.toString('ascii', 12, 16) === 'IHDR' &&
        bytes.toString('ascii', bytes.length - 8, bytes.length - 4) === 'IEND') mime = 'image/png';
    else if (bytes.length > 4 && bytes[0] === 0xff && bytes[1] === 0xd8 && bytes[2] === 0xff &&
        bytes[bytes.length - 2] === 0xff && bytes[bytes.length - 1] === 0xd9) mime = 'image/jpeg';
    else if (bytes.length > 16 && bytes.toString('ascii', 0, 4) === 'RIFF' &&
        bytes.toString('ascii', 8, 12) === 'WEBP' && bytes.readUInt32LE(4) + 8 === bytes.length) mime = 'image/webp';
    else throw new ApiError('UNSUPPORTED_IMAGE_FORMAT');
    const form = new FormData();
    form.set('uuid', body.uuid);
    form.set('filename', body.filename);
    form.set('file', new Blob([bytes], { type: mime }), body.filename);
    const ledger = { uuid: body.uuid, filename: body.filename, sha256: digest(bytes) };
    return this.write('/files', body.uuid, ledger, form, result => {
      if (typeof result.uri !== 'string' || !result.uri.trim() || /^https?:/i.test(result.uri)) {
        throw new ApiError('UPSTREAM_FILE_URI_MISSING', 502, true);
      }
      return { fileUri: result.uri };
    });
  }
}
