const crypto = require('node:crypto');
const fs = require('node:fs');

const EXPECTED_UPSTREAM_SHA256 =
  '0de9951b68e5d0b718915c81625fe4570315a509a3eb49d4f847ff528ef078a7';

const before = [
  "  const graphConfig: RunConfig['graphConfig'] = {",
  '    signal,',
  '    agents: agentInputs,',
  '    edges: agents[0].edges,',
  '  };',
].join('\n');

const after = [
  '  const bauerV3DirectFinal =',
  '    agents.length === 1 &&',
  '    (agents[0] as RunAgent & { bauerV3DirectFinal?: boolean }).bauerV3DirectFinal === true;',
  "  const graphConfig: RunConfig['graphConfig'] = {",
  '    signal,',
  '    agents: agentInputs,',
  '    edges: agents[0].edges,',
  '    ...(bauerV3DirectFinal ? { toolEnd: true } : {}),',
  '  };',
].join('\n');

const replaceExactlyOnce = (source, target, replacement) => {
  const first = source.indexOf(target);
  if (first < 0 || source.indexOf(target, first + target.length) >= 0) {
    throw new Error(`expected one upstream run-graph patch anchor, found ${first < 0 ? 0 : 'multiple'}`);
  }
  return `${source.slice(0, first)}${replacement}${source.slice(first + target.length)}`;
};

const patchSource = (source) => replaceExactlyOnce(source, before, after);
const sha256 = (value) => crypto.createHash('sha256').update(value).digest('hex');

const patchFile = (target) => {
  const source = fs.readFileSync(target, 'utf8');
  const actual = sha256(source);
  if (actual !== EXPECTED_UPSTREAM_SHA256) {
    throw new Error(
      `refusing to patch unexpected LibreChat run.ts: expected ${EXPECTED_UPSTREAM_SHA256}, got ${actual}`,
    );
  }
  fs.writeFileSync(target, patchSource(source), 'utf8');
};

if (require.main === module) {
  const target = process.argv[2];
  if (!target) {
    throw new Error('usage: node patchBauerV3RunGraph.js <run.ts>');
  }
  patchFile(target);
}

module.exports = {
  EXPECTED_UPSTREAM_SHA256,
  after,
  before,
  patchFile,
  patchSource,
  replaceExactlyOnce,
  sha256,
};
