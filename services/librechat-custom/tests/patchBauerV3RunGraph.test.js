const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');

const {
  EXPECTED_UPSTREAM_SHA256,
  after,
  before,
  patchSource,
  replaceExactlyOnce,
} = require('../patchBauerV3RunGraph');

test('run-graph bundle patch is checksum-bound and propagates only the private V3 marker', () => {
  assert.match(EXPECTED_UPSTREAM_SHA256, /^[0-9a-f]{64}$/);
  const patched = patchSource(`prefix\n${before}\nsuffix`);
  assert.equal(patched.split(after).length - 1, 1);
  assert.match(patched, /agents\.length === 1/);
  assert.match(patched, /agents\[0\]\.bauerV3DirectFinal === true/);
  assert.match(patched, /agentInputs\[0\]\.toolEnd = true/);
  assert.doesNotMatch(patched, /graphConfig[\s\S]*\{ toolEnd: true \}/);
});

const executePatchedProjection = ({ agents, agentInputs }) => {
  const context = {
    agents,
    agentInputs,
    signal: 'test-signal',
  };
  vm.runInNewContext(
    `${after}\nresult = { graphConfig, agentInputs };`,
    context,
  );
  return context.result;
};

test('run-graph bundle patch terminates only a single marked V3 agent after its tool', () => {
  const v3Inputs = [{ agentId: 'agent-v3' }];
  const v3 = executePatchedProjection({
    agents: [{ bauerV3DirectFinal: true, edges: [] }],
    agentInputs: v3Inputs,
  });
  assert.equal(v3.agentInputs[0].toolEnd, true);
  assert.equal(v3.graphConfig.toolEnd, undefined);
  assert.equal(v3.graphConfig.agents, v3Inputs);

  const ordinaryInputs = [{ agentId: 'agent-v2' }];
  const ordinary = executePatchedProjection({
    agents: [{ edges: [] }],
    agentInputs: ordinaryInputs,
  });
  assert.equal(ordinary.agentInputs[0].toolEnd, undefined);
  assert.equal(ordinary.graphConfig.toolEnd, undefined);

  const multiInputs = [{ agentId: 'agent-v3' }, { agentId: 'agent-other' }];
  const multi = executePatchedProjection({
    agents: [
      { bauerV3DirectFinal: true, edges: [] },
      { edges: [] },
    ],
    agentInputs: multiInputs,
  });
  assert.equal(multi.agentInputs[0].toolEnd, undefined);
  assert.equal(multi.agentInputs[1].toolEnd, undefined);
});

test('run-graph bundle patch refuses absent or ambiguous anchors', () => {
  assert.throws(
    () => replaceExactlyOnce('missing', before, after),
    /found 0/,
  );
  assert.throws(
    () => replaceExactlyOnce(`${before}\n${before}`, before, after),
    /found multiple/,
  );
});
