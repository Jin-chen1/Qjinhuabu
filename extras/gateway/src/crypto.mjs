import {
  randomBytes, randomUUID, createHash, createCipheriv, createDecipheriv,
  generateKeyPairSync, createPrivateKey, sign, timingSafeEqual,
} from 'node:crypto';

export const digest = (value, encoding = 'hex') =>
  createHash('sha256').update(value).digest(encoding);
export const secret = () => randomBytes(32).toString('base64url');

export function equal(a, b) {
  return typeof a === 'string' && typeof b === 'string' &&
    timingSafeEqual(Buffer.from(digest(a)), Buffer.from(digest(b)));
}

export function seal(value, key, context) {
  const iv = randomBytes(12);
  const cipher = createCipheriv('aes-256-gcm', key, iv);
  cipher.setAAD(Buffer.from(context));
  const body = Buffer.concat([cipher.update(JSON.stringify(value), 'utf8'), cipher.final()]);
  return Buffer.concat([iv, cipher.getAuthTag(), body]).toString('base64url');
}

export function unseal(value, key, context) {
  const bytes = Buffer.from(value, 'base64url');
  const decipher = createDecipheriv('aes-256-gcm', key, bytes.subarray(0, 12));
  decipher.setAAD(Buffer.from(context));
  decipher.setAuthTag(bytes.subarray(12, 28));
  return JSON.parse(Buffer.concat([decipher.update(bytes.subarray(28)), decipher.final()]).toString('utf8'));
}

export function newDpopKey() {
  return generateKeyPairSync('ec', { namedCurve: 'P-256' }).privateKey.export({ format: 'jwk' });
}

export function dpopProof(jwk, method, target, accessToken, nonce) {
  const url = new URL(target);
  url.search = '';
  url.hash = '';
  const header = { typ: 'dpop+jwt', alg: 'ES256',
    jwk: { kty: jwk.kty, crv: jwk.crv, x: jwk.x, y: jwk.y } };
  const payload = { jti: randomUUID(), htm: method.toUpperCase(), htu: url.toString(),
    iat: Math.floor(Date.now() / 1000),
    ...(accessToken ? { ath: digest(accessToken, 'base64url') } : {}),
    ...(nonce ? { nonce } : {}) };
  const input = [header, payload].map(v => Buffer.from(JSON.stringify(v)).toString('base64url')).join('.');
  const signature = sign('sha256', Buffer.from(input), {
    key: createPrivateKey({ key: jwk, format: 'jwk' }), dsaEncoding: 'ieee-p1363',
  }).toString('base64url');
  return `${input}.${signature}`;
}
