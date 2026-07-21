const DEFAULT_BATCH_K = 10;
const MAX_BATCH_FILES = 1000;
const MAX_VISIBLE_USER_FILES = 10;
const MAX_VISIBLE_FILENAME_CHARS = 160;

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

const normalizeBatchResults = (responses, files, maxResults = DEFAULT_BATCH_K) => {
  const allowedFiles = new Map(uniqueFiles(files).map((file) => [file.file_id, file]));
  const seen = new Set();
  const normalized = [];
  let droppedUnauthorized = 0;

  for (const response of responses ?? []) {
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
      });
    }
  }

  normalized.sort((a, b) => a.distance - b.distance);
  return { results: normalized.slice(0, maxResults), droppedUnauthorized };
};

module.exports = {
  DEFAULT_BATCH_K,
  MAX_BATCH_FILES,
  MAX_VISIBLE_USER_FILES,
  buildFileSearchContext,
  createBatchGroups,
  createBatchQueryBody,
  normalizeBatchResults,
  partitionFiles,
  sanitizeVisibleFilename,
};
