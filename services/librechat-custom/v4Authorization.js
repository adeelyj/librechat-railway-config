const { createV3AuthorizationContext } = require('./v3Authorization');

/**
 * Sign the request-local V4 source scope. The wire token intentionally keeps
 * the proven V3 envelope format consumed by the shared API verifier while
 * binding it to a distinct V4 audience, tenant, and knowledge base.
 */
const createV4AuthorizationContext = ({
  userId,
  agentId,
  sourceIds,
  nowSeconds,
  ttlSeconds,
  tenantId = process.env.BAUER_V4_TENANT_ID,
  knowledgeBaseId = process.env.BAUER_V4_KB_ID,
  audience = process.env.BAUER_V4_AUTH_AUDIENCE || 'bauer-evidence-v4',
  keyId =
    process.env.BAUER_V4_AUTH_KEY_ID ||
    process.env.BAUER_V3_AUTH_KEY_ID,
  signingKey =
    process.env.BAUER_V4_AUTH_SIGNING_KEY ||
    process.env.BAUER_V3_AUTH_SIGNING_KEY,
}) =>
  createV3AuthorizationContext({
    userId,
    agentId,
    sourceIds,
    nowSeconds,
    ttlSeconds,
    tenantId,
    knowledgeBaseId,
    audience,
    keyId,
    signingKey,
  });

module.exports = { createV4AuthorizationContext };
