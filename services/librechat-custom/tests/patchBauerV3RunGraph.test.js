const test = require('node:test');
const assert = require('node:assert/strict');

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
  assert.match(patched, /\{ toolEnd: true \}/);
  assert.doesNotMatch(patched, /agents\[0\]\.toolEnd/);
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
