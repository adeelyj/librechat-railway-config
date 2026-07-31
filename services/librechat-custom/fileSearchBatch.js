const DEFAULT_BATCH_K = 10;
const MAX_BATCH_FILES = 1000;
const MAX_VISIBLE_USER_FILES = 10;
const MAX_VISIBLE_FILENAME_CHARS = 160;
const MAX_V3_QUERY_CHARS = 4000;
const MAX_V4_QUESTION_CHARS = 4000;
const MAX_V4_SUPPLEMENTAL_SOURCES = 16;
const V3_ANSWERED_STATUSES = new Set(['answered', 'answered_after_repair']);
const V3_REFUSAL_STATUSES = new Set(['refused_no_evidence', 'refused_after_validation']);
const V4_STATUSES = new Set(['complete', 'partial', 'not_found', 'refused']);

const parseIdAllowlist = (value) =>
  new Set(
    String(value ?? '')
      .split(',')
      .map((item) => item.trim())
      .filter(Boolean),
  );

const sanitizeVisibleFilename = (filename) => {
  const normalized = String(filename ?? '')
    .replace(/[\u0000-\u001f\u007f]+/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();

  if (normalized.length <= MAX_VISIBLE_FILENAME_CHARS) {
    return normalized;
  }

  return `${normalized.slice(0, MAX_VISIBLE_FILENAME_CHARS - 1)}…`;
};

const uniqueFiles = (files) => {
  const byId = new Map();
  for (const file of files ?? []) {
    if (!file?.file_id || byId.has(file.file_id)) {
      continue;
    }
    byId.set(file.file_id, file);
  }
  return [...byId.values()];
};

const parseV4SupplementalSources = (
  value = process.env.BAUER_V4_SUPPLEMENTAL_SOURCES_JSON,
) => {
  const normalized = value == null ? '' : String(value).trim();
  if (normalized === '') {
    return [];
  }

  let parsed;
  try {
    parsed = JSON.parse(normalized);
  } catch {
    throw new Error('BAUER_V4_SUPPLEMENTAL_SOURCES_JSON must be valid JSON');
  }
  if (!Array.isArray(parsed) || parsed.length > MAX_V4_SUPPLEMENTAL_SOURCES) {
    throw new Error(
      `BAUER_V4_SUPPLEMENTAL_SOURCES_JSON must contain at most ${MAX_V4_SUPPLEMENTAL_SOURCES} sources`,
    );
  }

  const identifiers = new Set();
  return parsed.map((item) => {
    const fileId = typeof item?.file_id === 'string' ? item.file_id.trim() : '';
    const filename =
      typeof item?.filename === 'string' ? sanitizeVisibleFilename(item.filename) : '';
    if (
      !fileId ||
      fileId.length > 256 ||
      !filename ||
      identifiers.has(fileId)
    ) {
      throw new Error('BAUER_V4_SUPPLEMENTAL_SOURCES_JSON contains an invalid source');
    }
    identifiers.add(fileId);
    return {
      file_id: fileId,
      filename,
      fromAgent: true,
      v4Supplemental: true,
    };
  });
};

const createV4AuthorizedFiles = (
  files,
  value = process.env.BAUER_V4_SUPPLEMENTAL_SOURCES_JSON,
) => {
  const base = uniqueFiles(files);
  const baseIds = new Set(base.map((file) => file.file_id));
  const supplemental = parseV4SupplementalSources(value);
  if (supplemental.some((file) => baseIds.has(file.file_id))) {
    throw new Error('A V4 supplemental source collides with an Agent file');
  }
  return [...base, ...supplemental];
};

const partitionFiles = (files) => {
  const unique = uniqueFiles(files);
  return {
    agentFiles: unique.filter((file) => file.fromAgent === true),
    userFiles: unique.filter((file) => file.fromAgent !== true),
  };
};

const buildFileSearchContext = (files, toolName = 'file_search') => {
  const { agentFiles, userFiles } = partitionFiles(files);
  if (agentFiles.length === 0 && userFiles.length === 0) {
    return `- Note: Semantic search is available through the ${toolName} tool but no files are currently loaded. Request the user to upload documents to search through.`;
  }

  const lines = [];
  if (agentFiles.length > 0) {
    const noun = agentFiles.length === 1 ? 'document' : 'documents';
    lines.push(
      `- Note: Use the ${toolName} tool to search the ${agentFiles.length} ${noun} attached to this Agent's knowledge base.`,
    );
  }

  if (userFiles.length > 0) {
    lines.push(
      agentFiles.length > 0
        ? '- Files attached specifically to this conversation:'
        : `- Note: Use the ${toolName} tool to search files attached to this conversation:`,
    );
    for (const file of userFiles.slice(0, MAX_VISIBLE_USER_FILES)) {
      lines.push(`\t- ${JSON.stringify(sanitizeVisibleFilename(file.filename))}`);
    }
    if (userFiles.length > MAX_VISIBLE_USER_FILES) {
      lines.push(`\t- …and ${userFiles.length - MAX_VISIBLE_USER_FILES} more conversation files.`);
    }
  }

  return lines.join('\n');
};

const createBatchGroups = (files, entityId) => {
  const { agentFiles, userFiles } = partitionFiles(files);
  if (agentFiles.length + userFiles.length > MAX_BATCH_FILES) {
    throw new Error(`file_search supports at most ${MAX_BATCH_FILES} files per request`);
  }

  const groups = [];
  if (agentFiles.length > 0) {
    groups.push({ files: agentFiles, entity_id: entityId || undefined });
  }
  if (userFiles.length > 0) {
    groups.push({ files: userFiles, entity_id: undefined });
  }
  return groups;
};

const createBatchQueryBody = (group, query, k = DEFAULT_BATCH_K) => {
  const body = {
    query,
    file_ids: group.files.map((file) => file.file_id),
    k,
  };
  if (group.entity_id) {
    body.entity_id = group.entity_id;
  }
  return body;
};

const selectFileSearchRoute = (
  entityId,
  v2AllowlistValue = process.env.RAG_V2_AGENT_IDS,
  v3AllowlistValue = process.env.BAUER_V3_AGENT_IDS,
  v4AllowlistValue = process.env.BAUER_V4_AGENT_IDS,
) => {
  if (!entityId) {
    return 'v1';
  }
  if (parseIdAllowlist(v4AllowlistValue).has(entityId)) {
    return 'v4';
  }
  if (parseIdAllowlist(v3AllowlistValue).has(entityId)) {
    return 'v3';
  }
  return parseIdAllowlist(v2AllowlistValue).has(entityId) ? 'v2' : 'v1';
};

const createV2QueryBody = (group, query, k = 8) => {
  const body = createBatchQueryBody(group, query, k);
  body.debug = false;
  return body;
};

const createV3AnswerBody = (query, topK = 8) => ({
  query,
  top_k: topK,
});

const createV4AnswerBody = ({
  question,
  searchHint,
  signedScope,
  requestId,
  locale = 'de-DE',
  clientInstance = 'railway-testing',
}) => ({
  schema_version: '4.0',
  request_id: requestId,
  question,
  search_hint:
    typeof searchHint === 'string' && searchHint.trim()
      ? searchHint.trim()
      : null,
  conversation_context: [],
  locale,
  client: {
    type: 'librechat',
    instance: clientInstance,
  },
  authorization: {
    signed_scope: signedScope,
  },
  typed_constraints: [],
});

const resolveOriginalQuestion = ({ route, toolQuery, requestBody }) => {
  if (route !== 'v3' && route !== 'v4') {
    return toolQuery;
  }
  const originalUserText =
    typeof requestBody?.text === 'string' ? requestBody.text.trim() : '';
  if (!originalUserText) {
    throw new Error(`${route.toUpperCase()} file_search requires the original user request`);
  }
  const maximum = route === 'v4' ? MAX_V4_QUESTION_CHARS : MAX_V3_QUERY_CHARS;
  if (originalUserText.length > maximum) {
    throw new Error(
      `${route.toUpperCase()} file_search supports at most ${maximum} query characters`,
    );
  }
  return originalUserText;
};

const resolveV3RequestQuery = resolveOriginalQuestion;

const normalizeV3Answer = (response, files) => {
  const body = response?.data;
  if (!body || typeof body.answer !== 'string' || typeof body.status !== 'string') {
    return null;
  }
  const answer = body.answer.trim();
  const releaseId = typeof body.release_id === 'string' ? body.release_id.trim() : '';
  const answered = V3_ANSWERED_STATUSES.has(body.status);
  const refused = V3_REFUSAL_STATUSES.has(body.status);
  if (
    (!answered && !refused) ||
    answer.length === 0 ||
    releaseId.length === 0 ||
    (answered && body.validation?.valid !== true)
  ) {
    return {
      accepted: false,
      droppedUnauthorized: 0,
      status: body.status,
      release_id: releaseId,
      answer: '',
      evidence: [],
    };
  }
  const allowedFiles = new Map(uniqueFiles(files).map((file) => [file.file_id, file]));
  const evidence = [];
  let droppedUnauthorized = 0;
  for (const item of Array.isArray(body.evidence) ? body.evidence : []) {
    const fileId = item?.external_file_id;
    if (!fileId || !allowedFiles.has(fileId)) {
      droppedUnauthorized += 1;
      continue;
    }
    evidence.push({
      file_id: fileId,
      filename: item.title || allowedFiles.get(fileId)?.filename || fileId,
      content: String(item.content ?? ''),
      page: Number.isInteger(item.page_number) ? item.page_number : null,
      citation_id: item.citation_id,
      evidence_id: item.evidence_id,
      source_version_id: item.source_version_id,
      source_sha256: item.source_sha256,
      source_type: item.source_type,
      channels: Array.isArray(item.channels) ? item.channels : [],
      table_headers: Array.isArray(item.table_headers) ? item.table_headers : [],
      table_values: Array.isArray(item.table_values) ? item.table_values : [],
      unit: item.unit || null,
      footnotes: Array.isArray(item.footnotes) ? item.footnotes : [],
      score: Number(item.score) || 0,
    });
  }
  if (droppedUnauthorized > 0 || (answered && evidence.length === 0)) {
    return {
      accepted: false,
      droppedUnauthorized,
      status: body.status,
      release_id: body.release_id,
      answer: '',
      evidence: [],
    };
  }
  return {
    accepted: true,
    droppedUnauthorized: 0,
    status: body.status,
    release_id: releaseId,
    answer,
    repair_attempted: body.repair_attempted === true,
    validation: body.validation ?? null,
    evidence,
  };
};

const normalizeV4Answer = (response, files) => {
  const body = response?.data;
  const status = typeof body?.status === 'string' ? body.status : '';
  const answer = typeof body?.answer === 'string' ? body.answer.trim() : '';
  const releaseId =
    typeof body?.release?.public_id === 'string' ? body.release.public_id.trim() : '';
  const validated = body?.validation?.passed === true;
  const answerMode =
    typeof body?.validation?.answer_mode === 'string'
      ? body.validation.answer_mode.trim()
      : '';
  const validationFingerprint =
    typeof body?.validation?.validation_fingerprint === 'string'
      ? body.validation.validation_fingerprint.trim()
      : '';
  if (
    !V4_STATUSES.has(status) ||
    !answer ||
    !releaseId ||
    (status !== 'refused' &&
      (!validated ||
        !['lossless_deterministic', 'grounded_structured'].includes(answerMode) ||
        !/^[0-9a-f]{64}$/i.test(validationFingerprint)))
  ) {
    return {
      accepted: false,
      droppedUnauthorized: 0,
      status,
      release_id: releaseId,
      answer: '',
      citations: [],
    };
  }

  const allowedFiles = new Map(uniqueFiles(files).map((file) => [file.file_id, file]));
  const citations = [];
  let droppedUnauthorized = 0;
  for (const item of Array.isArray(body.citations) ? body.citations : []) {
    const fileId = item?.coordinate?.source_id;
    if (!fileId || !allowedFiles.has(fileId)) {
      droppedUnauthorized += 1;
      continue;
    }
    citations.push({
      file_id: fileId,
      filename:
        item.original_filename ||
        item.source_title ||
        allowedFiles.get(fileId)?.filename ||
        fileId,
      content: String(item.excerpt ?? ''),
      page: Number.isInteger(item?.coordinate?.page) ? item.coordinate.page : null,
      printed_page: item?.coordinate?.printed_page ?? null,
      citation_id: item.citation_id,
      evidence_id: item.evidence_id,
      source_version_id: item?.coordinate?.source_version_id,
      document_number: item.document_number ?? null,
      section_path: Array.isArray(item?.coordinate?.section_path)
        ? item.coordinate.section_path
        : [],
      table_id: item?.coordinate?.table_id ?? null,
      row_index: Number.isInteger(item?.coordinate?.row_index)
        ? item.coordinate.row_index
        : null,
      column_index: Number.isInteger(item?.coordinate?.column_index)
        ? item.coordinate.column_index
        : null,
      table_title: item.table_title ?? null,
      header_path: Array.isArray(item.header_path) ? item.header_path : [],
      raw_value: item.raw_value ?? null,
      normalized_value: item.normalized_value ?? null,
      raw_unit: item.raw_unit ?? null,
      normalized_unit: item.normalized_unit ?? null,
      qualifier: item.qualifier ?? null,
      footnotes: Array.isArray(item.footnotes) ? item.footnotes : [],
    });
  }
  const requiresCitations = status === 'complete' || status === 'partial';
  if (droppedUnauthorized > 0 || (requiresCitations && citations.length === 0)) {
    return {
      accepted: false,
      droppedUnauthorized,
      status,
      release_id: releaseId,
      answer: '',
      citations: [],
    };
  }
  return {
    accepted: true,
    droppedUnauthorized: 0,
    status,
    release_id: releaseId,
    answer,
    coverage: Array.isArray(body.coverage) ? body.coverage : [],
    not_found: Array.isArray(body.not_found) ? body.not_found : [],
    validation: body.validation ?? null,
    answer_mode: answerMode || null,
    validation_fingerprint: validationFingerprint || null,
    citations,
  };
};

const normalizeBatchResults = (responses, files, maxResults = DEFAULT_BATCH_K) => {
  const allowedFiles = new Map(uniqueFiles(files).map((file) => [file.file_id, file]));
  const seen = new Set();
  const normalized = [];
  let droppedUnauthorized = 0;

  for (const response of responses ?? []) {
    if (response?.data?.route === 'v2' && Array.isArray(response.data.results)) {
      for (const item of response.data.results) {
        const fileId = item?.file_id;
        if (!fileId || !allowedFiles.has(fileId)) {
          droppedUnauthorized += 1;
          continue;
        }
        const content = String(item?.content ?? '');
        if (!content) {
          continue;
        }
        const page = item?.page || null;
        const location = [
          ...(Array.isArray(item?.section) ? item.section : []),
          item?.table_title,
          item?.row_label,
        ]
          .filter(Boolean)
          .join(' / ');
        const key = `${fileId}\u0000${page ?? ''}\u0000${location}\u0000${content}`;
        if (seen.has(key)) {
          continue;
        }
        seen.add(key);
        const fallbackFilename = allowedFiles.get(fileId)?.filename ?? fileId;
        normalized.push({
          filename: item?.filename || fallbackFilename,
          content,
          distance: 1 - Math.max(0, Math.min(1, Number(item?.score) || 0)),
          file_id: fileId,
          page,
          route: 'v2',
          citation_id: item?.citation_id,
          index_version: response.data.index_version,
          location,
          source_type: item?.source_type || 'public_document',
          channels: Array.isArray(item?.channels) ? item.channels : [],
          language: item?.language || null,
          publication_date: item?.publication_date || null,
          certificates: Array.isArray(item?.certificates) ? item.certificates : [],
          product_families: Array.isArray(item?.product_families) ? item.product_families : [],
          media: Array.isArray(item?.media) ? item.media : [],
          component_categories: Array.isArray(item?.component_categories)
            ? item.component_categories
            : [],
          standards: Array.isArray(item?.standards) ? item.standards : [],
          headers: Array.isArray(item?.headers) ? item.headers : [],
          row_values: Array.isArray(item?.row_values) ? item.row_values : [],
          units: Array.isArray(item?.units) ? item.units : [],
          footnotes: item?.footnotes || null,
        });
      }
      continue;
    }

    for (const item of response?.data ?? []) {
      if (!Array.isArray(item) || item.length < 2) {
        continue;
      }
      const [docInfo, distance] = item;
      const metadata = docInfo?.metadata ?? {};
      const fileId = metadata.file_id;
      if (!fileId || !allowedFiles.has(fileId)) {
        droppedUnauthorized += 1;
        continue;
      }

      const content = String(docInfo?.page_content ?? '');
      if (!content) {
        continue;
      }
      const page = metadata.page || null;
      const key = `${fileId}\u0000${page ?? ''}\u0000${content}`;
      if (seen.has(key)) {
        continue;
      }
      seen.add(key);

      const fallbackFilename = allowedFiles.get(fileId)?.filename ?? fileId;
      const source = typeof metadata.source === 'string' ? metadata.source : '';
      normalized.push({
        filename: source ? source.split('/').pop() : fallbackFilename,
        content,
        distance: Number(distance),
        file_id: fileId,
        page,
        route: 'v1',
      });
    }
  }

  normalized.sort((a, b) => {
    if (a.route === 'v2' && b.route !== 'v2') {
      return -1;
    }
    if (b.route === 'v2' && a.route !== 'v2') {
      return 1;
    }
    return a.distance - b.distance;
  });
  return { results: normalized.slice(0, maxResults), droppedUnauthorized };
};

module.exports = {
  DEFAULT_BATCH_K,
  MAX_BATCH_FILES,
  MAX_V4_SUPPLEMENTAL_SOURCES,
  MAX_VISIBLE_USER_FILES,
  MAX_V3_QUERY_CHARS,
  MAX_V4_QUESTION_CHARS,
  buildFileSearchContext,
  createBatchGroups,
  createBatchQueryBody,
  createV2QueryBody,
  createV3AnswerBody,
  createV4AnswerBody,
  createV4AuthorizedFiles,
  normalizeBatchResults,
  normalizeV3Answer,
  normalizeV4Answer,
  parseIdAllowlist,
  parseV4SupplementalSources,
  partitionFiles,
  resolveOriginalQuestion,
  resolveV3RequestQuery,
  sanitizeVisibleFilename,
  selectFileSearchRoute,
};
