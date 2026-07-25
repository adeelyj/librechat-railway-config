const test = require('node:test');
const assert = require('node:assert/strict');
const crypto = require('node:crypto');

const {
  canonicalJson,
  createV3AuthorizationContext,
} = require('../v3Authorization');

const SECRET = 'bauer-v3-signing-key-material-00000001';

test('V3 authorization token is canonical, scoped, short-lived, and correctly signed', () => {
  const token = createV3AuthorizationContext({
    userId: 'user-1',
    agentId: 'agent-v3',
    sourceIds: ['file-b', 'file-a', 'file-a'],
    tenantId: 'tenant-1',
    knowledgeBaseId: 'kb-1',
    audience: 'bauer-evidence-v3',
    keyId: 'current',
    signingKey: SECRET,
    nowSeconds: 1000,
    ttlSeconds: 120,
  });
  const [headerSegment, claimsSegment, signatureSegment] = token.split('.');
  const header = JSON.parse(Buffer.from(headerSegment, 'base64url').toString('utf8'));
  const claims = JSON.parse(Buffer.from(claimsSegment, 'base64url').toString('utf8'));
  assert.equal(header.typ, 'BAUER-AUTH');
  assert.deepEqual(claims.authorized_source_ids, ['file-a', 'file-b']);
  assert.equal(claims.issued_at, 1000);
  assert.equal(claims.expires_at, 1120);
  assert.equal(
    Buffer.from(headerSegment, 'base64url').toString('utf8'),
    canonicalJson(header),
  );
  assert.equal(
    Buffer.from(claimsSegment, 'base64url').toString('utf8'),
    canonicalJson(claims),
  );
  const expected = crypto
    .createHmac('sha256', Buffer.from(SECRET, 'utf8'))
    .update(`${headerSegment}.${claimsSegment}`)
    .digest('base64url');
  assert.equal(signatureSegment, expected);
});

test('V3 authorization fails closed on weak keys, invalid TTL, and oversized scope', () => {
  const base = {
    userId: 'user',
    agentId: 'agent',
    sourceIds: ['file'],
    tenantId: 'tenant',
    knowledgeBaseId: 'kb',
    keyId: 'current',
    signingKey: SECRET,
    nowSeconds: 1000,
  };
  assert.throws(
    () => createV3AuthorizationContext({ ...base, signingKey: 'short' }),
    /at least 32 bytes/,
  );
  assert.throws(
    () => createV3AuthorizationContext({ ...base, ttlSeconds: 301 }),
    /between 1 and 300/,
  );
  assert.throws(
    () =>
      createV3AuthorizationContext({
        ...base,
        sourceIds: Array.from({ length: 1001 }, (_, index) => `file-${index}`),
      }),
    /at most 1000/,
  );
});
