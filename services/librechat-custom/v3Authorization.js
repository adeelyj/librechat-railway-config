const crypto = require('node:crypto');

const MAX_AUTHORIZED_SOURCES = 1000;
const MAX_IDENTIFIER_LENGTH = 200;
const TOKEN_TYPE = 'BAUER-AUTH';
const TOKEN_VERSION = 1;

const requiredIdentifier = (value, name, maximum = MAX_IDENTIFIER_LENGTH) => {
  const normalized = String(value ?? '').trim();
  if (!normalized) {
    throw new Error(`${name} is required`);
  }
  if (normalized.length > maximum || /[\u0000-\u001f\u007f]/.test(normalized)) {
    throw new Error(`${name} is invalid`);
  }
  return normalized;
};

const canonicalize = (value) => {
  if (Array.isArray(value)) {
    return value.map(canonicalize);
  }
  if (value && typeof value === 'object') {
    return Object.fromEntries(
      Object.keys(value)
        .sort()
        .map((key) => [key, canonicalize(value[key])]),
    );
  }
  return value;
};

const canonicalJson = (value) => JSON.stringify(canonicalize(value));
const base64url = (value) => Buffer.from(value).toString('base64url');

const createV3AuthorizationContext = ({
  userId,
  agentId,
  sourceIds,
  nowSeconds = Math.floor(Date.now() / 1000),
  ttlSeconds = 120,
  tenantId = process.env.BAUER_V3_TENANT_ID,
  knowledgeBaseId = process.env.BAUER_V3_KB_ID,
  audience = process.env.BAUER_V3_AUTH_AUDIENCE || 'bauer-evidence-v3',
  keyId = process.env.BAUER_V3_AUTH_KEY_ID,
  signingKey = process.env.BAUER_V3_AUTH_SIGNING_KEY,
}) => {
  if (!Number.isInteger(nowSeconds) || nowSeconds < 0) {
    throw new Error('nowSeconds must be a non-negative integer');
  }
  if (!Number.isInteger(ttlSeconds) || ttlSeconds < 1 || ttlSeconds > 300) {
    throw new Error('ttlSeconds must be between 1 and 300');
  }
  const secret = Buffer.from(String(signingKey ?? ''), 'utf8');
  if (secret.length < 32) {
    throw new Error('BAUER_V3_AUTH_SIGNING_KEY must contain at least 32 bytes');
  }
  const normalizedSources = [
    ...new Set((sourceIds ?? []).map((value) => requiredIdentifier(value, 'source ID'))),
  ].sort();
  if (normalizedSources.length > MAX_AUTHORIZED_SOURCES) {
    throw new Error(`V3 authorization supports at most ${MAX_AUTHORIZED_SOURCES} sources`);
  }

  const header = {
    alg: 'HS256',
    kid: requiredIdentifier(keyId, 'BAUER_V3_AUTH_KEY_ID', 128),
    typ: TOKEN_TYPE,
    v: TOKEN_VERSION,
  };
  const claims = {
    agent_id: requiredIdentifier(agentId, 'agent ID'),
    audience: requiredIdentifier(audience, 'audience'),
    authorized_source_ids: normalizedSources,
    expires_at: nowSeconds + ttlSeconds,
    issued_at: nowSeconds,
    knowledge_base_id: requiredIdentifier(knowledgeBaseId, 'BAUER_V3_KB_ID'),
    tenant_id: requiredIdentifier(tenantId, 'BAUER_V3_TENANT_ID'),
    user_id: requiredIdentifier(userId, 'user ID'),
  };
  const headerSegment = base64url(canonicalJson(header));
  const claimsSegment = base64url(canonicalJson(claims));
  const signingInput = `${headerSegment}.${claimsSegment}`;
  const signature = crypto.createHmac('sha256', secret).update(signingInput).digest('base64url');
  return `${signingInput}.${signature}`;
};

module.exports = {
  MAX_AUTHORIZED_SOURCES,
  canonicalJson,
  createV3AuthorizationContext,
};
