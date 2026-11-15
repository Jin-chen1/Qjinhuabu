import { randomUUID } from 'node:crypto';
import { digest, secret, dpopProof, newDpopKey } from './crypto.mjs';
import { ApiError } from './errors.mjs';

export const AUTHORIZE_URL = 'https://accounts.feishu.cn/open-apis/authen/v1/authorize';
export const TOKEN_URL = 'https://open.feishu.cn/open-apis/authen/v2/oauth/token';
export const SCOPES = [
  'doubao:file:write', 'doubao:session.event:write', 'doubao:session.message:read',
  'doubao:session:read', 'doubao:session:write', 'offline_access',
];

export class OAuth {
  constructor(store, configuration, fetcher = fetch) {
    this.store = store;
    this.configuration = configuration;
    this.fetcher = fetcher;
    this.refreshPromise = null;
  }

  config() {
    const c = this.configuration();
    if (!c.clientId || !c.clientSecret || !c.redirectUri) throw new ApiError('OAUTH_NOT_CONFIGURED', 503);
    return c;
  }

  begin() {
    const config = this.config();
    const state = secret();
    const verifier = secret();
    const url = new URL(AUTHORIZE_URL);
    url.searchParams.set('app_id', config.clientId);
    url.searchParams.set('redirect_uri', config.redirectUri);
    url.searchParams.set('response_type', 'code');
    url.searchParams.set('scope', (config.scopes ?? SCOPES).join(' '));
    url.searchParams.set('state', state);
    url.searchParams.set('code_challenge', digest(verifier, 'base64url'));
    url.searchParams.set('code_challenge_method', 'S256');
    const ticket = this.store.addState(state, {
      authorizationUrl: url.toString(), verifier, clientId: config.clientId, redirectUri: config.redirectUri,
    });
    return { ticket, path: `/oauth/start?ticket=${encodeURIComponent(ticket)}`, expiresIn: 600 };
  }

  async callback(state, code, cookie) {
    if (!state || state.length > 128 || !code || code.length > 2048 || !cookie) {
      throw new ApiError('OAUTH_STATE_INVALID', 403);
    }
    const config = this.config();
    const record = this.store.consumeState(state, cookie);
    if (record.clientId !== config.clientId || record.redirectUri !== config.redirectUri) {
      throw new ApiError('OAUTH_CONFIG_CHANGED', 409);
    }
    const grant = await this.exchange({ grant_type: 'authorization_code', code,
      redirect_uri: config.redirectUri, code_verifier: record.verifier }, newDpopKey());
    if (!this.store.finishAuthorization(state, { ...grant, connectionId: randomUUID() })) {
      throw new ApiError('OAUTH_STATE_INVALID', 403);
    }
  }

  status() {
    const c = this.configuration();
    const grant = this.store.grant();
    return {
      configured: Boolean(c.clientId && c.clientSecret),
      connected: Boolean(grant && !grant.refreshing && grant.refreshExpiresAt > Date.now()),
      accessExpiresAt: grant ? new Date(grant.accessExpiresAt).toISOString() : null,
      refreshExpiresAt: grant ? new Date(grant.refreshExpiresAt).toISOString() : null,
      tokenType: grant?.tokenType ?? null,
      reauthorizationRequired: Boolean(grant?.refreshing || (grant && grant.refreshExpiresAt <= Date.now())),
    };
  }

  async usableGrant() {
    let grant = this.store.grant();
    if (!grant) throw new ApiError('OAUTH_AUTHORIZATION_REQUIRED', 409);
    if (this.refreshPromise) return this.refreshPromise;
    if (grant.refreshing || grant.refreshExpiresAt <= Date.now()) {
      throw new ApiError('OAUTH_REAUTHORIZE_REQUIRED', 409);
    }
    if (grant.accessExpiresAt > Date.now() + 60_000) return grant;
    if (!this.store.claimRefresh()) throw new ApiError('OAUTH_REAUTHORIZE_REQUIRED', 409);
    this.refreshPromise = (async () => {
      try {
        const next = await this.exchange({ grant_type: 'refresh_token', refresh_token: grant.refreshToken },
          grant.privateJwk);
        if (!this.store.finishRefresh(grant.connectionId, next)) {
          throw new ApiError('OAUTH_REAUTHORIZE_REQUIRED', 409);
        }
        return this.store.grant();
      } catch {
        // Keep refreshing=1 after an unknown rotating-token result. A fresh browser grant is required.
        throw new ApiError('OAUTH_REAUTHORIZE_REQUIRED', 409);
      } finally { this.refreshPromise = null; }
    })();
    return this.refreshPromise;
  }

  async authorization(method, url, nonce) {
    const grant = await this.usableGrant();
    return { Authorization: `${grant.tokenType} ${grant.accessToken}`,
      ...(grant.tokenType === 'DPoP' ? { DPoP: dpopProof(grant.privateJwk, method, url, grant.accessToken, nonce) } : {}) };
  }

  async exchange(fields, jwk) {
    const config = this.config();
    let nonce;
    for (let attempt = 0; attempt < 2; attempt++) {
      let response;
      try {
        response = await this.fetcher(TOKEN_URL, {
          method: 'POST', redirect: 'error', signal: AbortSignal.timeout(20_000),
          headers: { 'Content-Type': 'application/json', DPoP: dpopProof(jwk, 'POST', TOKEN_URL, undefined, nonce) },
          body: JSON.stringify({ client_id: config.clientId, client_secret: config.clientSecret, ...fields }),
        });
      } catch { throw new ApiError('OAUTH_EXCHANGE_UNAVAILABLE', 502); }
      const headerNonce = response.headers.get('dpop-nonce');
      if (attempt === 0 && response.status === 401 && headerNonce) {
        nonce = headerNonce;
        continue;
      }
      if (!response.ok) throw new ApiError('OAUTH_EXCHANGE_REJECTED', 502);
      let envelope;
      try { envelope = await response.json(); } catch { throw new ApiError('OAUTH_RESPONSE_INVALID', 502); }
      if (envelope.code !== undefined && envelope.code !== 0) throw new ApiError('OAUTH_EXCHANGE_REJECTED', 502);
      const result = envelope.data ?? envelope;
      const type = result.token_type?.toLowerCase();
      if (typeof result.access_token !== 'string' || !result.access_token ||
          typeof result.refresh_token !== 'string' || !result.refresh_token ||
          !['bearer', 'dpop'].includes(type) ||
          !Number.isFinite(result.expires_in) || result.expires_in <= 60 ||
          !Number.isFinite(result.refresh_token_expires_in) || result.refresh_token_expires_in <= 60) {
        throw new ApiError('OAUTH_RESPONSE_INVALID', 502);
      }
      return { accessToken: result.access_token, refreshToken: result.refresh_token,
        tokenType: type === 'dpop' ? 'DPoP' : 'Bearer', privateJwk: jwk,
        accessExpiresAt: Date.now() + result.expires_in * 1000,
        refreshExpiresAt: Date.now() + result.refresh_token_expires_in * 1000 };
    }
    throw new ApiError('OAUTH_DPOP_NONCE_REJECTED', 502);
  }
}
