const axios = require('axios');
const { logger } = require('@librechat/data-schemas');
const { tool } = require('@librechat/agents/langchain/tools');
const { generateShortLivedToken, logAxiosError } = require('@librechat/api');
const { Tools, EToolResources } = require('librechat-data-provider');
const { filterFilesByAgentAccess } = require('~/server/services/Files/permissions');
const { getFiles } = require('~/models');
const {
  buildFileSearchContext,
  createBatchGroups,
  createBatchQueryBody,
  createV2QueryBody,
  normalizeBatchResults,
  selectFileSearchRoute,
} = require('./fileSearchBatch');

const fileSearchJsonSchema = {
  type: 'object',
  properties: {
    query: {
      type: 'string',
      description:
        "A natural language query to search for relevant information in the files. Be specific and use keywords related to the information you're looking for. The query will be used for semantic similarity matching against the file contents.",
    },
  },
  required: ['query'],
};

/**
 * Resolve the files this request is authorized to search and build a compact,
 * model-visible summary. Agent knowledge-base filenames stay server-side.
 *
 * @param {Object} options
 * @param {ServerRequest} options.req
 * @param {Agent['tool_resources']} options.tool_resources
 * @param {string} [options.agentId] - The agent ID for file access control
 * @returns {Promise<{
 *   files: Array<{ file_id: string; filename: string; fromAgent: boolean }>,
 *   toolContext: string
 * }>}
 */
const primeFiles = async (options) => {
  const { tool_resources, req, agentId } = options;
  const file_ids = tool_resources?.[EToolResources.file_search]?.file_ids ?? [];
  const agentResourceIds = new Set(file_ids);
  const resourceFiles = tool_resources?.[EToolResources.file_search]?.files ?? [];

  const allFiles = (await getFiles({ file_id: { $in: file_ids } }, null, { text: 0 })) ?? [];

  let dbFiles;
  if (req?.user?.id && agentId) {
    dbFiles = await filterFilesByAgentAccess({
      files: allFiles,
      userId: req.user.id,
      role: req.user.role,
      agentId,
    });
  } else {
    dbFiles = allFiles;
  }

  dbFiles = dbFiles.concat(resourceFiles);
  const files = dbFiles.filter(Boolean).map((file) => ({
    file_id: file.file_id,
    filename: file.filename,
    fromAgent: agentResourceIds.has(file.file_id),
  }));

  return { files, toolContext: buildFileSearchContext(files, Tools.file_search) };
};

/**
 * @param {Object} options
 * @param {string} options.userId
 * @param {Array<{ file_id: string; filename: string; fromAgent?: boolean }>} options.files
 * @param {string} [options.entity_id]
 * @param {boolean} [options.fileCitations=false] - Whether to include citation instructions
 * @returns
 */
const createFileSearchTool = async ({ userId, files, entity_id, fileCitations = false }) => {
  return tool(
    async ({ query }) => {
      if (files.length === 0) {
        return ['No files to search. Instruct the user to add files for the search.', undefined];
      }
      const jwtToken = generateShortLivedToken(userId);
      if (!jwtToken) {
        return ['There was an error authenticating the file search request.', undefined];
      }

      let groups;
      try {
        groups = createBatchGroups(files, entity_id);
      } catch (error) {
        logger.warn(`[${Tools.file_search}] Refused invalid batch: ${error.message}`);
        return [`File search could not start: ${error.message}`, undefined];
      }

      const queryPromises = groups.map(async (group) => {
        const route = selectFileSearchRoute(group.entity_id);
        const path = route === 'v2' ? '/query_v2' : '/query_multiple';
        const body =
          route === 'v2' ? createV2QueryBody(group, query) : createBatchQueryBody(group, query);
        logger.debug(`[${Tools.file_search}] RAG API ${path}`, {
          retrievalRoute: route,
          fileCount: body.file_ids.length,
          entity_id: body.entity_id,
          k: body.k,
        });

        try {
          return await axios.post(`${process.env.RAG_API_URL}${path}`, body, {
            headers: {
              Authorization: `Bearer ${jwtToken}`,
              'Content-Type': 'application/json',
            },
          });
        } catch (error) {
          if (error?.response?.status === 404) {
            return { data: [] };
          }
          logAxiosError({
            message: `Error encountered in \`file_search\` while querying ${path}`,
            error,
          });
          return null;
        }
      });

      const responses = (await Promise.all(queryPromises)).filter((result) => result !== null);
      if (responses.length === 0) {
        return ['No results found or errors occurred while searching the files.', undefined];
      }

      const { results: formattedResults, droppedUnauthorized } = normalizeBatchResults(
        responses,
        files,
      );
      if (droppedUnauthorized > 0) {
        logger.warn(
          `[${Tools.file_search}] Dropped ${droppedUnauthorized} result(s) outside the authorized file list`,
        );
      }

      if (formattedResults.length === 0) {
        return [
          'No content found in the files. The files may not have been processed correctly or you may need to refine your query.',
          undefined,
        ];
      }

      const formattedString = formattedResults
        .map(
          (result, index) =>
            `File: ${result.filename}${
              fileCitations ? `\nAnchor: \\ue202turn0file${index} (${result.filename})` : ''
            }${
              result.route === 'v2'
                ? `\nEvidence ID: [${result.citation_id}]` +
                  `\nRetrieval: Bauer RAG V2 (${result.index_version})` +
                  `\nSource type: ${result.source_type}` +
                  (result.location ? `\nLocation: ${result.location}` : '') +
                  (result.page ? `\nPage: ${result.page}` : '')
                : '\nRetrieval: V1 semantic'
            }\nRelevance: ${(1.0 - result.distance).toFixed(4)}\nContent: ${result.content}\n`,
        )
        .join('\n---\n');

      const sources = formattedResults.map((result) => ({
        type: 'file',
        fileId: result.file_id,
        content: result.content,
        fileName: result.filename,
        relevance: 1.0 - result.distance,
        pages: result.page ? [result.page] : [],
        pageRelevance: result.page ? { [result.page]: 1.0 - result.distance } : {},
        metadata: {
          retrievalRoute: result.route,
          indexVersion: result.index_version,
          citationId: result.citation_id,
          location: result.location,
          sourceType: result.source_type,
          channels: result.channels,
          language: result.language,
          publicationDate: result.publication_date,
          certificates: result.certificates,
          productFamilies: result.product_families,
          media: result.media,
          componentCategories: result.component_categories,
          standards: result.standards,
          tableHeaders: result.headers,
          tableRowValues: result.row_values,
          units: result.units,
          footnotes: result.footnotes,
        },
      }));

      return [formattedString, { [Tools.file_search]: { sources, fileCitations } }];
    },
    {
      name: Tools.file_search,
      responseFormat: 'content_and_artifact',
      description: `Performs semantic search across attached "${Tools.file_search}" documents using natural language queries. This tool analyzes the content of uploaded files to find relevant information, quotes, and passages that best match your query. Use this to extract specific information or find relevant sections within the available documents.${
        fileCitations
          ? `

**CITE FILE SEARCH RESULTS:**
Use the EXACT anchor markers shown below (copy them verbatim) immediately after statements derived from file content. Reference the filename in your text:
- File citation: "The document.pdf states that... \\ue202turn0file0"
- Page reference: "According to report.docx... \\ue202turn0file1"
- Multi-file: "Multiple sources confirm... \\ue200\\ue202turn0file0\\ue202turn0file1\\ue201"

**CRITICAL:** Output these escape sequences EXACTLY as shown (e.g., \\ue202turn0file0). DO NOT substitute with other characters like † or similar symbols.
**ALWAYS mention the filename in your text before the citation marker. For Bauer RAG V2 results, also copy the adjacent [V2-N] Evidence ID so the exact passage remains auditable. NEVER use markdown links or footnotes.**`
          : ''
      }`,
      schema: fileSearchJsonSchema,
    },
  );
};

module.exports = { createFileSearchTool, primeFiles, fileSearchJsonSchema };
