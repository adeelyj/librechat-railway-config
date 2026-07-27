const test = require('node:test');
const assert = require('node:assert/strict');

const {
  EXPECTED_UPSTREAM_SHA256,
  after,
  before,
  patchSource,
  replaceExactlyOnce,
} = require('../patchBauerV3FileSearchRequest');

test('file-search request patch is checksum-bound and passes only the current request', () => {
  assert.match(EXPECTED_UPSTREAM_SHA256, /^[0-9a-f]{64}$/);
  const patched = patchSource(`prefix\n${before}\nsuffix`);
  assert.equal(patched.split(after).length - 1, 1);
  assert.match(patched, /req: options\.req/);
});

test('file-search request patch refuses absent or ambiguous anchors', () => {
  assert.throws(
    () => replaceExactlyOnce('missing', before, after),
    /found 0/,
  );
  assert.throws(
    () => replaceExactlyOnce(`${before}\n${before}`, before, after),
    /found multiple/,
  );
});
