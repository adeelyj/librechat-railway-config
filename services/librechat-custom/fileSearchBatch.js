const DEFAULT_BATCH_K = 10;
const MAX_BATCH_FILES = 1000;
const MAX_VISIBLE_USER_FILES = 10;
const MAX_VISIBLE_FILENAME_CHARS = 160;

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

const selectFileSearchRoute = (entityId, allowlistValue = process.env.RAG_V2_AGENT_IDS) => {
  if (!entityId) {
    return 'v1';
  }
  return parseIdAllowlist(allowlistValue).has(entityId) ? 'v2' : 'v1';
};

const createV2QueryBody = (group, query, k = 8) => {
  const body = createBatchQueryBody(group, query, k);
  body.debug = false;
  return body;
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
  MAX_VISIBLE_USER_FILES,
  buildFileSearchContext,
  createBatchGroups,
  createBatchQueryBody,
  createV2QueryBody,
  normalizeBatchResults,
  parseIdAllowlist,
  partitionFiles,
  sanitizeVisibleFilename,
  selectFileSearchRoute,
};
