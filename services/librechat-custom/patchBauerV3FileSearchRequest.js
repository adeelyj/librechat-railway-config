const crypto = require('node:crypto');
const fs = require('node:fs');

const EXPECTED_UPSTREAM_SHA256 =
  '0d4758dab28293fc3f68914183acb495e3880fa53401017ce4587b56ad1bd922';

const before = [
  '        return createFileSearchTool({',
  '          userId: user,',
  '          files,',
  '          entity_id: agent?.id,',
  '          fileCitations,',
  '        });',
].join('\n');

const after = [
  '        return createFileSearchTool({',
  '          userId: user,',
  '          files,',
  '          entity_id: agent?.id,',
  '          req: options.req,',
  '          fileCitations,',
  '        });',
].join('\n');

const replaceExactlyOnce = (source, target, replacement) => {
  const first = source.indexOf(target);
  if (first < 0 || source.indexOf(target, first + target.length) >= 0) {
    throw new Error(
      `expected one upstream file-search request patch anchor, found ${first < 0 ? 0 : 'multiple'}`,
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
      `refusing to patch unexpected LibreChat handleTools.js: expected ${EXPECTED_UPSTREAM_SHA256}, got ${actual}`,
    );
  }
  fs.writeFileSync(target, patchSource(source), 'utf8');
};

if (require.main === module) {
  const target = process.argv[2];
  if (!target) {
    throw new Error('usage: node patchBauerV3FileSearchRequest.js <handleTools.js>');
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
