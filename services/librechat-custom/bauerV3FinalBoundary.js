const V3_ANSWERED_STATUSES = new Set(['answered', 'answered_after_repair']);
const V3_REFUSAL_STATUSES = new Set(['refused_no_evidence', 'refused_after_validation']);
const V4_STATUSES = new Set(['complete', 'partial', 'not_found', 'refused']);
const V4_ANSWER_MODES = new Set(['lossless_deterministic', 'grounded_structured']);
const SHA256_PATTERN = /^[0-9a-f]{64}$/i;
const DETERMINISTIC_BOUNDARY_REFUSAL =
  'I could not produce a validated Bauer answer for this request.';
const UUID_PATTERN =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

const parseIdAllowlist = (value) =>
  new Set(
    String(value ?? '')
      .split(',')
      .map((item) => item.trim())
      .filter(Boolean),
  );

const extractDirectFinal = (output) => {
  const envelope = output?.artifact?.file_search?.bauerV3;
  if (!envelope || envelope.directFinal !== true) {
    return null;
  }

  const status = typeof envelope.status === 'string' ? envelope.status : '';
  const answer = typeof envelope.finalAnswer === 'string' ? envelope.finalAnswer.trim() : '';
  const releaseId = typeof envelope.releaseId === 'string' ? envelope.releaseId.trim() : '';
  const answered = V3_ANSWERED_STATUSES.has(status);
  const refused = V3_REFUSAL_STATUSES.has(status);
  if (
    (!answered && !refused) ||
    answer.length === 0 ||
    !UUID_PATTERN.test(releaseId) ||
    (answered && envelope.validationPassed !== true)
  ) {
    return null;
  }

  return { answer, releaseId, status };
};

const extractV4DirectFinal = (output) => {
  const envelope = output?.artifact?.file_search?.bauerV4;
  if (!envelope || envelope.directFinal !== true) {
    return null;
  }
  const status = typeof envelope.status === 'string' ? envelope.status : '';
  const answer = typeof envelope.finalAnswer === 'string' ? envelope.finalAnswer.trim() : '';
  const releaseId = typeof envelope.releaseId === 'string' ? envelope.releaseId.trim() : '';
  const answerMode = typeof envelope.answerMode === 'string' ? envelope.answerMode.trim() : '';
  const validationFingerprint =
    typeof envelope.validationFingerprint === 'string'
      ? envelope.validationFingerprint.trim()
      : '';
  const answered = status !== 'refused';
  if (
    !V4_STATUSES.has(status) ||
    !answer ||
    !releaseId ||
    releaseId.length > 256 ||
    /[\u0000-\u001f\u007f]/.test(releaseId) ||
    (answered &&
      (envelope.validationPassed !== true ||
        !V4_ANSWER_MODES.has(answerMode) ||
        !SHA256_PATTERN.test(validationFingerprint)))
  ) {
    return null;
  }
  return {
    answer,
    releaseId,
    status,
    answerMode: answered ? answerMode : null,
    validationFingerprint: answered ? validationFingerprint : null,
  };
};

const selectV4Final = (current, candidate) => {
  if (!candidate) {
    return current;
  }
  if (!current) {
    return candidate;
  }
  if (current.conflict === true) {
    return current;
  }
  const identical =
    current.answer === candidate.answer &&
    current.releaseId === candidate.releaseId &&
    current.status === candidate.status &&
    current.answerMode === candidate.answerMode &&
    current.validationFingerprint === candidate.validationFingerprint;
  return identical ? current : { conflict: true };
};

const configuredToolNames = (primaryConfig) => {
  const names = new Set();
  for (const item of primaryConfig?.tools ?? []) {
    if (typeof item === 'string' && item.trim()) {
      names.add(item.trim());
    }
  }
  for (const item of primaryConfig?.toolDefinitions ?? []) {
    if (typeof item?.name === 'string' && item.name.trim()) {
      names.add(item.name.trim());
    }
  }
  if (primaryConfig?.toolRegistry instanceof Map) {
    for (const name of primaryConfig.toolRegistry.keys()) {
      if (typeof name === 'string' && name.trim()) {
        names.add(name.trim());
      }
    }
  }
  return names;
};

/**
 * Enforce a direct-final boundary for allow-listed Bauer V3 Agents.
 *
 * The first model may select `file_search`, but its text/reasoning deltas are
 * suppressed. The graph ends after the tool call, and sendCompletion replaces
 * every model-authored content part with the exact answer already validated by
 * Bauer RAG V3. Request-local tool approval is disabled because the only
 * permitted tool is the read-only V3 file_search path; a stale HITL resume is
 * rejected. If the tool does not provide a valid direct-final envelope, the
 * response fails closed with a fixed refusal.
 */
const createBauerV3FinalBoundary = ({
  baseToolEndCallback,
  contentParts,
  v3AgentIds = process.env.BAUER_V3_AGENT_IDS,
  v4AgentIds = process.env.BAUER_V4_AGENT_IDS,
}) => {
  if (typeof baseToolEndCallback !== 'function') {
    throw new TypeError('baseToolEndCallback must be a function');
  }
  if (!Array.isArray(contentParts)) {
    throw new TypeError('contentParts must be an array');
  }

  const state = {
    enabled: false,
    mode: null,
    final: null,
    toolEndCount: 0,
    validV4Count: 0,
    wrapped: false,
  };

  const toolEndCallback = async (data, metadata) => {
    await baseToolEndCallback(data, metadata);
    if (!state.enabled) {
      return;
    }
    state.toolEndCount += 1;
    if (state.mode === 'v4') {
      const candidate = extractV4DirectFinal(data?.output);
      if (candidate) {
        state.validV4Count += 1;
        state.final = selectV4Final(state.final, candidate);
      }
      return;
    }
    state.final = state.toolEndCount === 1 ? extractDirectFinal(data?.output) : null;
  };

  const activate = ({ agentId, appConfig, primaryConfig, eventHandlers }) => {
    const isV3 = parseIdAllowlist(v3AgentIds).has(agentId);
    const isV4 = parseIdAllowlist(v4AgentIds).has(agentId);
    if (isV3 && isV4) {
      throw new Error('A Bauer Agent cannot be allow-listed for both V3 and V4');
    }
    state.mode = isV4 ? 'v4' : isV3 ? 'v3' : null;
    state.enabled = state.mode !== null;
    if (!state.enabled) {
      return false;
    }
    if (!appConfig || !primaryConfig || !eventHandlers) {
      throw new Error('Bauer V3 final boundary requires an initialized Agent and handlers');
    }

    // `@librechat/api` intentionally propagates this private, request-local
    // marker to the single standard graph AgentInputs object's `toolEnd`
    // option. The legacy marker name is retained for the checksum-bound
    // overlay, but both allow-listed Bauer direct-final modes use it. V4
    // already plans all requested fields from the immutable full question;
    // repeated model-generated search hints must not create another tool
    // round. A provider may still emit parallel calls in the first round.
    primaryConfig.bauerV3DirectFinal = true;
    eventHandlers.on_message_delta = { handle: async () => {} };
    eventHandlers.on_reasoning_delta = { handle: async () => {} };
    return true;
  };

  const sealGraph = ({ primaryConfig, agentConfigs }) => {
    if (!state.enabled) {
      return;
    }
    const extraAgents = Number(agentConfigs?.size ?? 0);
    const edges = Array.isArray(primaryConfig?.edges) ? primaryConfig.edges : [];
    const subagents = primaryConfig?.subagents;
    if (
      extraAgents > 0 ||
      edges.length > 0 ||
      subagents?.enabled === true ||
      (Array.isArray(primaryConfig?.subagentAgentConfigs) &&
        primaryConfig.subagentAgentConfigs.length > 0)
    ) {
      throw new Error('Bauer direct-final Agent must not use connected agents or subagents');
    }

    const tools = configuredToolNames(primaryConfig);
    if (!tools.has('file_search') || [...tools].some((name) => name !== 'file_search')) {
      throw new Error('Bauer direct-final Agent must expose only the file_search tool');
    }
  };

  const wrapClient = (client) => {
    if (!state.enabled || state.wrapped) {
      return;
    }
    if (!client || typeof client.sendCompletion !== 'function') {
      throw new TypeError('Bauer final boundary requires an AgentClient');
    }
    const request = client.options?.req;
    const requestConfig = request?.config;
    if (!request || !requestConfig || typeof requestConfig !== 'object') {
      throw new TypeError('Bauer final boundary requires request-local app configuration');
    }
    const endpoints = requestConfig.endpoints;
    const agentsEndpoint = endpoints?.agents;
    if (agentsEndpoint?.toolApproval?.enabled === true) {
      request.config = {
        ...requestConfig,
        endpoints: {
          ...endpoints,
          agents: {
            ...agentsEndpoint,
            toolApproval: {
              ...agentsEndpoint.toolApproval,
              enabled: false,
            },
          },
        },
      };
    }
    state.wrapped = true;
    const originalSendCompletion = client.sendCompletion;
    client.resumeCompletion = async function rejectBauerResume() {
      throw new Error('Bauer direct-final runs cannot resume from a tool-approval checkpoint');
    };
    client.sendCompletion = async function sendBauerV3Completion(...args) {
      const result = await Reflect.apply(originalSendCompletion, this, args);
      const target = Array.isArray(this.contentParts) ? this.contentParts : contentParts;
      const toolParts = target.filter((part) => part?.type === 'tool_call');
      const validFinal =
        state.mode === 'v4'
          ? state.validV4Count >= 1 && state.final?.conflict !== true && state.final?.answer
          : state.toolEndCount === 1 && state.final?.answer;
      const finalText = validFinal ? state.final.answer : DETERMINISTIC_BOUNDARY_REFUSAL;
      target.splice(
        0,
        target.length,
        ...toolParts,
        {
          type: 'text',
          text: finalText,
        },
      );
      return {
        ...result,
        completion: target.filter(Boolean),
      };
    };
  };

  return {
    activate,
    sealGraph,
    toolEndCallback,
    wrapClient,
  };
};

module.exports = {
  DETERMINISTIC_BOUNDARY_REFUSAL,
  createBauerV3FinalBoundary,
  extractDirectFinal,
  extractV4DirectFinal,
  parseIdAllowlist,
  selectV4Final,
};
