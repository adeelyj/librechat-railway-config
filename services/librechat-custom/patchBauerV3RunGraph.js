const crypto = require('node:crypto');
const fs = require('node:fs');

const EXPECTED_UPSTREAM_SHA256 =
  '4d326d04da6a1dd5fc4dcd799ea4cc9a4f15c44da1b4a79101f648b7a9719bdf';

const before = [
  '\tconst graphConfig = {',
  '\t\tsignal,',
  '\t\tagents: agentInputs,',
  '\t\tedges: agents[0].edges',
  '\t};',
].join('\n');

const after = [
  '\tconst bauerV3DirectFinal = agents.length === 1 && agents[0].bauerV3DirectFinal === true;',
  '\tconst graphConfig = {',
  '\t\tsignal,',
  '\t\tagents: agentInputs,',
  '\t\tedges: agents[0].edges,',
  '\t\t...bauerV3DirectFinal ? { toolEnd: true } : {}',
  '\t};',
].join('\n');

const replaceExactlyOnce = (source, target, replacement) => {
  const first = source.indexOf(target);
  if (first < 0 || source.indexOf(target, first + target.length) >= 0) {
    throw new Error(
      `expected one upstream run-graph bundle patch anchor, found ${first < 0 ? 0 : 'multiple'}`,
    );
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
      `refusing to patch unexpected LibreChat API bundle: expected ${EXPECTED_UPSTREAM_SHA256}, got ${actual}`,
    );
  }
  fs.writeFileSync(target, patchSource(source), 'utf8');
};

if (require.main === module) {
  const target = process.argv[2];
  if (!target) {
    throw new Error('usage: node patchBauerV3RunGraph.js <index.cjs>');
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
