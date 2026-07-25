const crypto = require('node:crypto');
const fs = require('node:fs');

const EXPECTED_UPSTREAM_SHA256 =
  'e8ab5bcb5cd8e2dd47f46f382de15f4b4deb438eb6ec29a030b1205900328147';

const replacements = [
  {
    before: [
      "const { processAddedConvo } = require('./addedConvo');",
      "const { logViolation } = require('~/cache');",
    ].join('\n'),
    after: [
      "const { processAddedConvo } = require('./addedConvo');",
      "const { createBauerV3FinalBoundary } = require('./bauerV3FinalBoundary');",
      "const { logViolation } = require('~/cache');",
    ].join('\n'),
  },
  {
    before:
      '  const toolEndCallback = createToolEndCallback({ req, res, artifactPromises, streamId });',
    after: [
      '  const baseToolEndCallback = createToolEndCallback({',
      '    req,',
      '    res,',
      '    artifactPromises,',
      '    streamId,',
      '  });',
      '  const bauerV3Boundary = createBauerV3FinalBoundary({',
      '    baseToolEndCallback,',
      '    contentParts,',
      '  });',
      '  const toolEndCallback = bauerV3Boundary.toolEndCallback;',
    ].join('\n'),
  },
  {
    before: [
      '  );',
      '',
      '  /** Price emitted usage with the primary agent\'s resolved endpoint config so',
    ].join('\n'),
    after: [
      '  );',
      '',
      '  bauerV3Boundary.activate({',
      '    agentId: primaryConfig.id,',
      '    appConfig,',
      '    primaryConfig,',
      '    eventHandlers,',
      '  });',
      '',
      '  /** Price emitted usage with the primary agent\'s resolved endpoint config so',
    ].join('\n'),
  },
  {
    before: [
      '  const agentContextAttachmentsByAgentId = buildAgentContextAttachmentsByAgentId([',
      '    primaryConfig,',
      '    ...agentConfigs.values(),',
      '  ]);',
    ].join('\n'),
    after: [
      '  bauerV3Boundary.sealGraph({ primaryConfig, agentConfigs });',
      '',
      '  const agentContextAttachmentsByAgentId = buildAgentContextAttachmentsByAgentId([',
      '    primaryConfig,',
      '    ...agentConfigs.values(),',
      '  ]);',
    ].join('\n'),
  },
  {
    before: [
      '  });',
      '',
      '  if (streamId) {',
      '    GenerationJobManager.setCollectedUsage(streamId, collectedUsage);',
    ].join('\n'),
    after: [
      '  });',
      '',
      '  bauerV3Boundary.wrapClient(client);',
      '',
      '  if (streamId) {',
      '    GenerationJobManager.setCollectedUsage(streamId, collectedUsage);',
    ].join('\n'),
  },
];

const replaceExactlyOnce = (source, before, after) => {
  const first = source.indexOf(before);
  if (first < 0 || source.indexOf(before, first + before.length) >= 0) {
    throw new Error(`expected one upstream patch anchor, found ${first < 0 ? 0 : 'multiple'}`);
  }
  return `${source.slice(0, first)}${after}${source.slice(first + before.length)}`;
};

const patchSource = (source) =>
  replacements.reduce(
    (patched, replacement) =>
      replaceExactlyOnce(patched, replacement.before, replacement.after),
    source,
  );

const sha256 = (value) => crypto.createHash('sha256').update(value).digest('hex');

const patchFile = (target) => {
  const source = fs.readFileSync(target, 'utf8');
  const actual = sha256(source);
  if (actual !== EXPECTED_UPSTREAM_SHA256) {
    throw new Error(
      `refusing to patch unexpected LibreChat initialize.js: expected ${EXPECTED_UPSTREAM_SHA256}, got ${actual}`,
    );
  }
  fs.writeFileSync(target, patchSource(source), 'utf8');
};

if (require.main === module) {
  const target = process.argv[2];
  if (!target) {
    throw new Error('usage: node patchBauerV3FinalBoundary.js <initialize.js>');
  }
  patchFile(target);
}

module.exports = {
  EXPECTED_UPSTREAM_SHA256,
  patchFile,
  patchSource,
  replacements,
  replaceExactlyOnce,
  sha256,
};
