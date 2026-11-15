import { DatabaseSync } from 'node:sqlite';
import { chmodSync } from 'node:fs';
import { digest, secret, seal, unseal } from './crypto.mjs';
import { ApiError } from './errors.mjs';

export class Store {
  constructor(path, key) {
    this.key = key;
    this.db = new DatabaseSync(path);
    chmodSync(path, 0o600);
    this.db.exec(`
      PRAGMA journal_mode=WAL;
      PRAGMA synchronous=FULL;
      PRAGMA busy_timeout=5000;
      CREATE TABLE IF NOT EXISTS grants (
        id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL, refreshing INTEGER NOT NULL DEFAULT 0
      );
      CREATE TABLE IF NOT EXISTS oauth_states (
        hash TEXT PRIMARY KEY, ticket_hash TEXT UNIQUE, cookie_hash TEXT, payload TEXT NOT NULL,
        expires_at INTEGER NOT NULL, visited INTEGER NOT NULL DEFAULT 0, consumed INTEGER NOT NULL DEFAULT 0
      );
      CREATE TABLE IF NOT EXISTS requests (
        scope TEXT NOT NULL, route TEXT NOT NULL, uuid TEXT NOT NULL, body_hash TEXT NOT NULL,
        status TEXT NOT NULL, result TEXT, PRIMARY KEY(scope, route, uuid)
      );
      CREATE TABLE IF NOT EXISTS sessions (id TEXT NOT NULL, scope TEXT NOT NULL, PRIMARY KEY(id, scope));
      CREATE TABLE IF NOT EXISTS files (uri TEXT NOT NULL, scope TEXT NOT NULL, PRIMARY KEY(uri, scope));
    `);
  }

  close() { this.db.close(); }
  transaction(fn) {
    this.db.exec('BEGIN IMMEDIATE');
    try { const result = fn(); this.db.exec('COMMIT'); return result; }
    catch (error) { this.db.exec('ROLLBACK'); throw error; }
  }

  addState(state, payload) {
    const ticket = secret();
    this.db.prepare('DELETE FROM oauth_states WHERE expires_at < ?').run(Date.now());
    this.db.prepare('INSERT INTO oauth_states(hash,ticket_hash,payload,expires_at) VALUES(?,?,?,?)')
      .run(digest(state), digest(ticket), seal({ ...payload, state }, this.key, `state:${digest(state)}`),
        Date.now() + 10 * 60_000);
    return ticket;
  }

  consumeTicket(ticket) {
    return this.transaction(() => {
      const row = this.db.prepare('SELECT * FROM oauth_states WHERE ticket_hash=? AND visited=0 AND expires_at>?')
        .get(digest(ticket), Date.now());
      if (!row) throw new ApiError('OAUTH_TICKET_INVALID', 403);
      const cookie = secret();
      this.db.prepare('UPDATE oauth_states SET visited=1,cookie_hash=? WHERE hash=?').run(digest(cookie), row.hash);
      const payload = unseal(row.payload, this.key, `state:${row.hash}`);
      return { ...payload, cookie };
    });
  }

  consumeState(state, cookie) {
    return this.transaction(() => {
      const hash = digest(state);
      const row = this.db.prepare(
        'SELECT * FROM oauth_states WHERE hash=? AND cookie_hash=? AND visited=1 AND consumed=0 AND expires_at>?')
        .get(hash, digest(cookie), Date.now());
      if (!row) throw new ApiError('OAUTH_STATE_INVALID', 403);
      this.db.prepare('UPDATE oauth_states SET consumed=1 WHERE hash=?').run(hash);
      return unseal(row.payload, this.key, `state:${hash}`);
    });
  }

  saveGrant(grant) {
    this.db.prepare(`INSERT INTO grants(id,payload,refreshing) VALUES(1,?,0)
      ON CONFLICT(id) DO UPDATE SET payload=excluded.payload,refreshing=0`)
      .run(seal(grant, this.key, 'grant:local'));
  }

  finishAuthorization(state, grant) {
    return this.transaction(() => {
      const hash = digest(state);
      const row = this.db.prepare('SELECT 1 FROM oauth_states WHERE hash=? AND consumed=1 AND expires_at>?')
        .get(hash, Date.now());
      if (!row) return false;
      this.saveGrant(grant);
      this.db.prepare('DELETE FROM oauth_states WHERE hash=?').run(hash);
      return true;
    });
  }

  grant() {
    const row = this.db.prepare('SELECT payload,refreshing FROM grants WHERE id=1').get();
    if (!row) return null;
    try { return { ...unseal(row.payload, this.key, 'grant:local'), refreshing: Boolean(row.refreshing) }; }
    catch { throw new ApiError('OAUTH_STORAGE_INVALID', 503); }
  }

  claimRefresh() {
    return this.db.prepare('UPDATE grants SET refreshing=1 WHERE id=1 AND refreshing=0').run().changes === 1;
  }

  finishRefresh(connectionId, next) {
    return this.transaction(() => {
      const current = this.grant();
      if (!current || !current.refreshing || current.connectionId !== connectionId) return false;
      this.saveGrant({ ...next, connectionId });
      return true;
    });
  }

  clearGrant() {
    this.transaction(() => {
      this.db.exec('DELETE FROM grants; DELETE FROM oauth_states;');
    });
  }

  scope() { return this.grant()?.connectionId ?? 'not-connected'; }

  reserve(route, uuid, body) {
    return this.transaction(() => {
      const scope = this.scope();
      const hash = digest(JSON.stringify(body));
      const row = this.db.prepare('SELECT * FROM requests WHERE scope=? AND route=? AND uuid=?').get(scope, route, uuid);
      if (row) {
        if (row.body_hash !== hash) throw new ApiError('REQUEST_UUID_CONFLICT', 409);
        if (row.status === 'completed') {
          return { cached: true, result: unseal(row.result, this.key, `result:${scope}:${route}:${uuid}`) };
        }
        throw new ApiError(row.status === 'rejected' ? 'REQUEST_REJECTED_USE_NEW_UUID' : 'REQUEST_OUTCOME_UNKNOWN',
          409, row.status !== 'rejected');
      }
      this.db.prepare('INSERT INTO requests(scope,route,uuid,body_hash,status) VALUES(?,?,?,?,?)')
        .run(scope, route, uuid, hash, 'pending');
      return { cached: false, scope };
    });
  }

  complete(route, uuid, scope, result, owned) {
    this.transaction(() => {
      this.db.prepare('UPDATE requests SET status=?,result=? WHERE scope=? AND route=? AND uuid=?')
        .run('completed', seal(result, this.key, `result:${scope}:${route}:${uuid}`), scope, route, uuid);
      if (owned?.sessionId) this.db.prepare('INSERT OR IGNORE INTO sessions(id,scope) VALUES(?,?)').run(owned.sessionId, scope);
      if (owned?.fileUri) this.db.prepare('INSERT OR IGNORE INTO files(uri,scope) VALUES(?,?)').run(owned.fileUri, scope);
    });
  }

  fail(route, uuid, scope, unknown) {
    this.db.prepare('UPDATE requests SET status=? WHERE scope=? AND route=? AND uuid=?')
      .run(unknown ? 'unknown' : 'rejected', scope, route, uuid);
  }

  ownsSession(id) {
    return Boolean(this.db.prepare('SELECT 1 FROM sessions WHERE id=? AND scope=?').get(id, this.scope()));
  }

  ownsFile(uri) {
    return Boolean(this.db.prepare('SELECT 1 FROM files WHERE uri=? AND scope=?').get(uri, this.scope()));
  }
}
