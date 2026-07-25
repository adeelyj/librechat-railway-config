const V3_ANSWERED_STATUSES = new Set(['answered', 'answered_after_repair']);
const V3_REFUSAL_STATUSES = new Set(['refused_no_evidence', 'refused_after_validation']);
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
}) => {
  if (typeof baseToolEndCallback !== 'function') {
    throw new TypeError('baseToolEndCallback must be a function');
  }
  if (!Array.isArray(contentParts)) {
    throw new TypeError('contentParts must be an array');
  }

  const state = {
    enabled: false,
    final: null,
    toolEndCount: 0,
    wrapped: false,
  };

  const toolEndCallback = async (data, metadata) => {
    await baseToolEndCallback(data, metadata);
    if (!state.enabled) {
      return;
    }
    state.toolEndCount += 1;
    state.final =
      state.toolEndCount === 1 ? extractDirectFinal(data?.output) : null;
  };

  const activate = ({ agentId, appConfig, primaryConfig, eventHandlers }) => {
    state.enabled = parseIdAllowlist(v3AgentIds).has(agentId);
    if (!state.enabled) {
      return false;
    }
    if (!appConfig || !primaryConfig || !eventHandlers) {
      throw new Error('Bauer V3 final boundary requires an initialized Agent and handlers');
    }

    primaryConfig.toolEnd = true;
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
      throw new Error('Bauer V3 direct-final Agent must not use connected agents or subagents');
    }

    const tools = configuredToolNames(primaryConfig);
    if (!tools.has('file_search') || [...tools].some((name) => name !== 'file_search')) {
      throw new Error('Bauer V3 direct-final Agent must expose only the file_search tool');
    }
  };

  const wrapClient = (client) => {
    if (!state.enabled || state.wrapped) {
      return;
    }
    if (!client || typeof client.sendCompletion !== 'function') {
      throw new TypeError('Bauer V3 final boundary requires an AgentClient');
    }
    const request = client.options?.req;
    const requestConfig = request?.config;
    if (!request || !requestConfig || typeof requestConfig !== 'object') {
      throw new TypeError('Bauer V3 final boundary requires request-local app configuration');
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
    client.resumeCompletion = async function rejectBauerV3Resume() {
      throw new Error('Bauer V3 direct-final runs cannot resume from a tool-approval checkpoint');
    };
    client.sendCompletion = async function sendBauerV3Completion(...args) {
      const result = await Reflect.apply(originalSendCompletion, this, args);
      const target = Array.isArray(this.contentParts) ? this.contentParts : contentParts;
      const toolParts = target.filter((part) => part?.type === 'tool_call');
      const finalText =
        state.toolEndCount === 1 && state.final?.answer
          ? state.final.answer
          : DETERMINISTIC_BOUNDARY_REFUSAL;
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
  parseIdAllowlist,
};
