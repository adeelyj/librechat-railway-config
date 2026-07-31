const test = require('node:test');
const assert = require('node:assert/strict');

const {
  DETERMINISTIC_BOUNDARY_REFUSAL,
  createBauerV3FinalBoundary,
  extractDirectFinal,
  extractV4DirectFinal,
} = require('../bauerV3FinalBoundary');

const RELEASE_ID = '40000000-0000-4000-8000-000000000003';
const appConfig = () => ({
  endpoints: {
    agents: {
      toolApproval: {
        enabled: false,
      },
    },
  },
});

const validOutput = () => ({
  artifact: {
    file_search: {
      bauerV3: {
        directFinal: true,
        finalAnswer: 'BM 40 supports 350 bar [E-ONE].',
        releaseId: RELEASE_ID,
        status: 'answered',
        validationPassed: true,
      },
    },
  },
});

const validV4Output = ({
  status = 'complete',
  answer = 'BM 40 supports 350 bar [citation-one].',
  supportedCoverage = 1,
} = {}) => ({
  artifact: {
    file_search: {
      bauerV4: {
        directFinal: true,
        finalAnswer: answer,
        releaseId: 'bauer-rag-v4-private-20260729-r1',
        status,
        validationPassed: status !== 'refused',
        coverage: Array.from({ length: supportedCoverage }, (_, index) => ({
          field: `field-${index}`,
          state: 'supported',
        })),
      },
    },
  },
});

test('direct-final envelopes require known status, release, answer, and validation', () => {
  assert.deepEqual(extractDirectFinal(validOutput()), {
    answer: 'BM 40 supports 350 bar [E-ONE].',
    releaseId: RELEASE_ID,
    status: 'answered',
  });

  const invalid = validOutput();
  invalid.artifact.file_search.bauerV3.validationPassed = false;
  assert.equal(extractDirectFinal(invalid), null);
  invalid.artifact.file_search.bauerV3.status = 'unknown';
  assert.equal(extractDirectFinal(invalid), null);
  invalid.artifact.file_search.bauerV3.status = 'answered';
  invalid.artifact.file_search.bauerV3.validationPassed = true;
  invalid.artifact.file_search.bauerV3.releaseId = 'not-a-release-id';
  assert.equal(extractDirectFinal(invalid), null);
});

test('allow-listed V3 Agents end on file_search and materialize only validated text', async () => {
  const contentParts = [
    { type: 'text', text: 'Unvalidated model preamble' },
    { type: 'tool_call', tool_call: { id: 'call-1', name: 'file_search' } },
  ];
  const baseCalls = [];
  const boundary = createBauerV3FinalBoundary({
    contentParts,
    v3AgentIds: 'agent-v3',
    baseToolEndCallback: async (...args) => baseCalls.push(args),
  });
  const primaryConfig = {
    id: 'agent-v3',
    tools: ['file_search'],
    toolDefinitions: [{ name: 'file_search' }],
    edges: [],
  };
  const eventHandlers = {
    on_message_delta: { handle: async () => assert.fail('model text must be suppressed') },
    on_reasoning_delta: { handle: async () => assert.fail('reasoning must be suppressed') },
  };
  assert.equal(
    boundary.activate({
      agentId: 'agent-v3',
      appConfig: appConfig(),
      primaryConfig,
      eventHandlers,
    }),
    true,
  );
  assert.equal(primaryConfig.bauerV3DirectFinal, true);
  assert.equal(primaryConfig.toolEnd, undefined);
  await eventHandlers.on_message_delta.handle();
  await eventHandlers.on_reasoning_delta.handle();
  boundary.sealGraph({ primaryConfig, agentConfigs: new Map() });

  await boundary.toolEndCallback({ output: validOutput() }, { run_id: 'run-1' });
  assert.equal(baseCalls.length, 1);

  const client = {
    contentParts,
    options: { req: { config: appConfig() } },
    async sendCompletion() {
      return { completion: [...this.contentParts], metadata: { retained: true } };
    },
  };
  boundary.wrapClient(client);
  const result = await client.sendCompletion();
  assert.deepEqual(result.completion, [
    { type: 'tool_call', tool_call: { id: 'call-1', name: 'file_search' } },
    { type: 'text', text: 'BM 40 supports 350 bar [E-ONE].' },
  ]);
  assert.deepEqual(result.metadata, { retained: true });
  assert.equal(result.completion.some((part) => part.text === 'Unvalidated model preamble'), false);
});

test('missing or invalid V3 tool output fails closed with a fixed refusal', async () => {
  const contentParts = [{ type: 'text', text: 'Unvalidated answer' }];
  const boundary = createBauerV3FinalBoundary({
    contentParts,
    v3AgentIds: 'agent-v3',
    baseToolEndCallback: async () => {},
  });
  const primaryConfig = { tools: ['file_search'], edges: [] };
  boundary.activate({
    agentId: 'agent-v3',
    appConfig: appConfig(),
    primaryConfig,
    eventHandlers: {},
  });
  await boundary.toolEndCallback({ output: { artifact: {} } }, {});

  const client = {
    contentParts,
    options: { req: { config: appConfig() } },
    async sendCompletion() {
      return { completion: [...this.contentParts] };
    },
  };
  boundary.wrapClient(client);
  const result = await client.sendCompletion();
  assert.deepEqual(result.completion, [
    { type: 'text', text: DETERMINISTIC_BOUNDARY_REFUSAL },
  ]);
});

test('multiple V3 tool completions fail closed instead of choosing by race order', async () => {
  const contentParts = [];
  const boundary = createBauerV3FinalBoundary({
    contentParts,
    v3AgentIds: 'agent-v3',
    baseToolEndCallback: async () => {},
  });
  boundary.activate({
    agentId: 'agent-v3',
    appConfig: appConfig(),
    primaryConfig: { tools: ['file_search'], edges: [] },
    eventHandlers: {},
  });
  await boundary.toolEndCallback({ output: validOutput() }, {});
  await boundary.toolEndCallback({ output: validOutput() }, {});

  const client = {
    contentParts,
    options: { req: { config: appConfig() } },
    async sendCompletion() {
      return { completion: [] };
    },
  };
  boundary.wrapClient(client);
  assert.deepEqual((await client.sendCompletion()).completion, [
    { type: 'text', text: DETERMINISTIC_BOUNDARY_REFUSAL },
  ]);
});

test('V4 materializes the validated result through the one-round direct-final marker', async () => {
  const contentParts = [
    { type: 'text', text: 'Untrusted model text' },
    { type: 'tool_call', tool_call: { id: 'call-1', name: 'file_search' } },
  ];
  const boundary = createBauerV3FinalBoundary({
    contentParts,
    v3AgentIds: 'agent-v3',
    v4AgentIds: 'agent-v4',
    baseToolEndCallback: async () => {},
  });
  const primaryConfig = {
    id: 'agent-v4',
    tools: ['file_search'],
    edges: [],
  };
  const eventHandlers = {};
  assert.equal(
    boundary.activate({
      agentId: 'agent-v4',
      appConfig: appConfig(),
      primaryConfig,
      eventHandlers,
    }),
    true,
  );
  assert.equal(primaryConfig.bauerV3DirectFinal, true);
  boundary.sealGraph({ primaryConfig, agentConfigs: new Map() });

  await boundary.toolEndCallback(
    { output: validV4Output({ answer: 'Complete validated answer.' }) },
    {},
  );
  const client = {
    contentParts,
    options: { req: { config: appConfig() } },
    async sendCompletion() {
      return { completion: [...this.contentParts] };
    },
  };
  boundary.wrapClient(client);
  assert.deepEqual((await client.sendCompletion()).completion, [
    { type: 'tool_call', tool_call: { id: 'call-1', name: 'file_search' } },
    { type: 'text', text: 'Complete validated answer.' },
  ]);
});

test('V4 direct-final envelopes reject unvalidated claims but allow deterministic refusal', () => {
  const valid = validV4Output();
  assert.equal(extractV4DirectFinal(valid)?.status, 'complete');
  valid.artifact.file_search.bauerV4.validationPassed = false;
  assert.equal(extractV4DirectFinal(valid), null);

  const refusal = validV4Output({
    status: 'refused',
    answer: 'The available evidence could not be validated safely.',
    supportedCoverage: 0,
  });
  assert.equal(extractV4DirectFinal(refusal)?.status, 'refused');
});

test('an Agent cannot occupy both V3 and V4 private namespaces', () => {
  const boundary = createBauerV3FinalBoundary({
    contentParts: [],
    v3AgentIds: 'shared-agent',
    v4AgentIds: 'shared-agent',
    baseToolEndCallback: async () => {},
  });
  assert.throws(
    () =>
      boundary.activate({
        agentId: 'shared-agent',
        appConfig: appConfig(),
        primaryConfig: { tools: ['file_search'], edges: [] },
        eventHandlers: {},
      }),
    /both V3 and V4/,
  );
});

test('V3 direct-final graph refuses connected agents and extra tools', () => {
  const boundary = createBauerV3FinalBoundary({
    contentParts: [],
    v3AgentIds: 'agent-v3',
    baseToolEndCallback: async () => {},
  });
  const primaryConfig = { tools: ['file_search', 'web_search'], edges: [] };
  boundary.activate({
    agentId: 'agent-v3',
    appConfig: appConfig(),
    primaryConfig,
    eventHandlers: {},
  });
  assert.throws(
    () => boundary.sealGraph({ primaryConfig, agentConfigs: new Map() }),
    /only the file_search tool/,
  );

  primaryConfig.tools = ['file_search'];
  assert.throws(
    () =>
      boundary.sealGraph({
        primaryConfig,
        agentConfigs: new Map([['other', {}]]),
      }),
    /must not use connected agents/,
  );

  primaryConfig.toolRegistry = new Map([
    ['file_search', {}],
    ['hidden_tool', {}],
  ]);
  assert.throws(
    () => boundary.sealGraph({ primaryConfig, agentConfigs: new Map() }),
    /only the file_search tool/,
  );
});

test('non-V3 Agents retain normal callbacks and client behavior', async () => {
  const contentParts = [{ type: 'text', text: 'normal answer' }];
  let baseCalled = false;
  const originalMessageHandler = { handle: async () => {} };
  const eventHandlers = { on_message_delta: originalMessageHandler };
  const boundary = createBauerV3FinalBoundary({
    contentParts,
    v3AgentIds: 'agent-v3',
    baseToolEndCallback: async () => {
      baseCalled = true;
    },
  });
  const primaryConfig = { tools: ['web_search'] };
  assert.equal(
    boundary.activate({
      agentId: 'agent-v2',
      appConfig: appConfig(),
      primaryConfig,
      eventHandlers,
    }),
    false,
  );
  assert.equal(eventHandlers.on_message_delta, originalMessageHandler);
  assert.equal(primaryConfig.bauerV3DirectFinal, undefined);
  await boundary.toolEndCallback({}, {});
  assert.equal(baseCalled, true);

  const client = {
    async sendCompletion() {
      return { completion: contentParts };
    },
  };
  boundary.wrapClient(client);
  assert.deepEqual(await client.sendCompletion(), { completion: contentParts });
});

test('V3 disables approval only on the request clone and rejects resume', async () => {
  const globalConfig = {
    endpoints: {
      agents: {
        capabilities: ['file_search'],
        toolApproval: {
          enabled: true,
          allow: ['file_search'],
        },
      },
    },
  };
  const contentParts = [];
  const boundary = createBauerV3FinalBoundary({
    contentParts,
    v3AgentIds: 'agent-v3',
    baseToolEndCallback: async () => {},
  });
  boundary.activate({
    agentId: 'agent-v3',
    appConfig: globalConfig,
    primaryConfig: { tools: ['file_search'], edges: [] },
    eventHandlers: {},
  });
  const request = { config: globalConfig };
  const client = {
    contentParts,
    options: { req: request },
    async sendCompletion() {
      return { completion: [] };
    },
    async resumeCompletion() {
      assert.fail('the upstream resume path must never run for V3');
    },
  };

  boundary.wrapClient(client);

  assert.equal(globalConfig.endpoints.agents.toolApproval.enabled, true);
  assert.notEqual(request.config, globalConfig);
  assert.equal(request.config.endpoints.agents.toolApproval.enabled, false);
  assert.deepEqual(request.config.endpoints.agents.toolApproval.allow, ['file_search']);
  await assert.rejects(
    client.resumeCompletion(),
    /cannot resume from a tool-approval checkpoint/,
  );
});
