const test = require('node:test');
const assert = require('node:assert/strict');

const {
  EXPECTED_UPSTREAM_SHA256,
  patchSource,
  replacements,
  replaceExactlyOnce,
} = require('../patchBauerV3FinalBoundary');

test('upstream patch is checksum-bound and inserts every boundary hook once', () => {
  assert.match(EXPECTED_UPSTREAM_SHA256, /^[0-9a-f]{64}$/);
  const source = replacements.map((replacement) => replacement.before).join('\n// gap\n');
  const patched = patchSource(source);
  for (const replacement of replacements) {
    assert.equal(patched.split(replacement.after).length - 1, 1);
  }
  assert.notEqual(patched, source);
  assert.match(patched, /createBauerV3FinalBoundary/);
  assert.match(patched, /agentId: primaryConfig\.id,\n    appConfig,/);
  assert.match(patched, /bauerV3Boundary\.sealGraph/);
  assert.match(patched, /bauerV3Boundary\.wrapClient/);
});

test('upstream patch refuses absent or ambiguous anchors', () => {
  assert.throws(() => replaceExactlyOnce('missing', 'anchor', 'replacement'), /found 0/);
  assert.throws(
    () => replaceExactlyOnce('anchor anchor', 'anchor', 'replacement'),
    /found multiple/,
  );
});
