const test = require('node:test');
const assert = require('node:assert/strict');

const { createV4AuthorizationContext } = require('../v4Authorization');

test('V4 authorization uses its distinct audience and fixed source scope', () => {
  const token = createV4AuthorizationContext({
    userId: 'user-1',
    agentId: 'agent-v4',
    sourceIds: ['file-b', 'file-a', 'file-a'],
    tenantId: 'tenant-v4',
    knowledgeBaseId: 'kb-v4',
    audience: 'bauer-evidence-v4',
    keyId: 'current',
    signingKey: 'bauer-v4-signing-key-material-00000001',
    nowSeconds: 2000,
    ttlSeconds: 120,
  });
  const claims = JSON.parse(
    Buffer.from(token.split('.')[1], 'base64url').toString('utf8'),
  );
  assert.equal(claims.audience, 'bauer-evidence-v4');
  assert.equal(claims.agent_id, 'agent-v4');
  assert.equal(claims.tenant_id, 'tenant-v4');
  assert.equal(claims.knowledge_base_id, 'kb-v4');
  assert.deepEqual(claims.authorized_source_ids, ['file-a', 'file-b']);
  assert.equal(claims.expires_at, 2120);
});

test('V4 authorization fails closed when V4 signing material is absent', () => {
  assert.throws(
    () =>
      createV4AuthorizationContext({
        userId: 'user-1',
        agentId: 'agent-v4',
        sourceIds: ['file-a'],
        tenantId: 'tenant-v4',
        knowledgeBaseId: 'kb-v4',
        audience: 'bauer-evidence-v4',
        keyId: 'current',
        signingKey: '',
      }),
    /at least 32 bytes/,
  );
});

test('V4 authorization may reuse the sealed V3 key with a distinct audience', () => {
  const previousKeyId = process.env.BAUER_V3_AUTH_KEY_ID;
  const previousSigningKey = process.env.BAUER_V3_AUTH_SIGNING_KEY;
  process.env.BAUER_V3_AUTH_KEY_ID = 'shared-current';
  process.env.BAUER_V3_AUTH_SIGNING_KEY =
    'shared-bauer-signing-key-material-000001';
  try {
    const token = createV4AuthorizationContext({
      userId: 'user-1',
      agentId: 'agent-v4',
      sourceIds: ['file-a'],
      tenantId: 'tenant-v4',
      knowledgeBaseId: 'kb-v4',
      audience: 'bauer-evidence-v4',
      nowSeconds: 2000,
      ttlSeconds: 120,
    });
    const claims = JSON.parse(
      Buffer.from(token.split('.')[1], 'base64url').toString('utf8'),
    );
    const header = JSON.parse(
      Buffer.from(token.split('.')[0], 'base64url').toString('utf8'),
    );
    assert.equal(header.kid, 'shared-current');
    assert.equal(claims.audience, 'bauer-evidence-v4');
  } finally {
    if (previousKeyId === undefined) {
      delete process.env.BAUER_V3_AUTH_KEY_ID;
    } else {
      process.env.BAUER_V3_AUTH_KEY_ID = previousKeyId;
    }
    if (previousSigningKey === undefined) {
      delete process.env.BAUER_V3_AUTH_SIGNING_KEY;
    } else {
      process.env.BAUER_V3_AUTH_SIGNING_KEY = previousSigningKey;
    }
  }
});
