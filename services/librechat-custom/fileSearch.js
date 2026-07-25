const axios = require('axios');
const { logger } = require('@librechat/data-schemas');
const { tool } = require('@librechat/agents/langchain/tools');
const { generateShortLivedToken, logAxiosError } = require('@librechat/api');
const { Tools, EToolResources } = require('librechat-data-provider');
const { filterFilesByAgentAccess } = require('~/server/services/Files/permissions');
const { getFiles } = require('~/models');
const { createV3AuthorizationContext } = require('./v3Authorization');
const {
  buildFileSearchContext,
  createBatchGroups,
  createBatchQueryBody,
  createV2QueryBody,
  createV3AnswerBody,
  normalizeBatchResults,
  normalizeV3Answer,
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

      let groups;
      try {
        groups = createBatchGroups(files, entity_id);
      } catch (error) {
        logger.warn(`[${Tools.file_search}] Refused invalid batch: ${error.message}`);
        return [`File search could not start: ${error.message}`, undefined];
      }

      const agentRoute = selectFileSearchRoute(entity_id);
      if (agentRoute === 'v3') {
        // A V3 answer is valid only for the evidence package V3 validated. Do not
        // mix uncompiled conversation-file results into the same final answer.
        groups = groups.filter((group) => group.entity_id === entity_id);
        if (groups.length === 0) {
          return [
            'No V3-indexed Agent documents are available. Conversation uploads are not mixed into a validated V3 answer.',
            undefined,
          ];
        }
      }

      const queryPromises = groups.map(async (group) => {
        const route = selectFileSearchRoute(group.entity_id);
        const path =
          route === 'v3' ? '/v3/answer' : route === 'v2' ? '/query_v2' : '/query_multiple';
        const body =
          route === 'v3'
            ? createV3AnswerBody(query)
            : route === 'v2'
              ? createV2QueryBody(group, query)
              : createBatchQueryBody(group, query);
        logger.debug(`[${Tools.file_search}] evidence route ${path}`, {
          retrievalRoute: route,
          fileCount: group.files.length,
          entity_id: group.entity_id,
          k: body.k ?? body.top_k,
        });

        try {
          let token;
          let baseUrl;
          if (route === 'v3') {
            token = createV3AuthorizationContext({
              userId,
              agentId: group.entity_id,
              sourceIds: group.files.map((file) => file.file_id),
            });
            baseUrl = process.env.BAUER_V3_API_URL;
            if (!baseUrl) {
              throw new Error('BAUER_V3_API_URL is not configured');
            }
          } else {
            token = generateShortLivedToken(userId);
            baseUrl = process.env.RAG_API_URL;
            if (!token) {
              throw new Error('could not create the RAG authorization token');
            }
          }
          const response = await axios.post(`${baseUrl}${path}`, body, {
            headers: {
              Authorization: `Bearer ${token}`,
              'Content-Type': 'application/json',
            },
          });
          return { route, response, authorizedFiles: group.files };
        } catch (error) {
          if (error?.response?.status === 404) {
            return { route, response: { data: [] } };
          }
          if (route === 'v3') {
            logger.error(`[${Tools.file_search}] V3 evidence request failed`, {
              retrievalRoute: 'v3',
              status: error?.response?.status ?? null,
              errorType: error?.name ?? 'Error',
            });
          } else {
            logAxiosError({
              message: `Error encountered in \`file_search\` while querying ${path}`,
              error,
            });
          }
          return null;
        }
      });

      const routedResponses = (await Promise.all(queryPromises)).filter(
        (result) => result !== null,
      );
      if (routedResponses.length === 0) {
        return ['No results found or errors occurred while searching the files.', undefined];
      }

      const v3Response = routedResponses.find((item) => item.route === 'v3');
      if (v3Response) {
        const final = normalizeV3Answer(
          v3Response.response,
          v3Response.authorizedFiles,
        );
        if (!final?.accepted) {
          logger.warn(
            `[${Tools.file_search}] Rejected V3 answer outside the authorized evidence scope`,
            {
              retrievalRoute: 'v3',
              droppedUnauthorized: final?.droppedUnauthorized ?? 0,
            },
          );
          return [
            'The V3 answer was rejected because its evidence did not match the server-authorized file scope.',
            undefined,
          ];
        }
        const sources = final.evidence.map((item) => ({
          type: 'file',
          fileId: item.file_id,
          content: item.content,
          fileName: item.filename,
          relevance: item.score,
          pages: item.page ? [item.page] : [],
          pageRelevance: item.page ? { [item.page]: item.score } : {},
          metadata: {
            retrievalRoute: 'v3',
            releaseId: final.release_id,
            citationId: item.citation_id,
            evidenceId: item.evidence_id,
            sourceVersionId: item.source_version_id,
            sourceSha256: item.source_sha256,
            sourceType: item.source_type,
            channels: item.channels,
            tableHeaders: item.table_headers,
            tableRowValues: item.table_values,
            unit: item.unit,
            footnotes: item.footnotes,
          },
        }));
        return [
          final.answer,
          {
            [Tools.file_search]: {
              sources,
              fileCitations,
              bauerV3: {
                status: final.status,
                releaseId: final.release_id,
                repairAttempted: final.repair_attempted,
                validationPassed: final.validation?.valid === true,
                directFinal: true,
                finalAnswer: final.answer,
              },
            },
          },
        ];
      }

      const responses = routedResponses.map((item) => item.response);
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
**ALWAYS mention the filename in your text before the citation marker. For Bauer RAG V2 results, also copy the adjacent [V2-N] Evidence ID so the exact passage remains auditable. When the tool returns a Bauer RAG V3 validated final answer, reproduce that answer verbatim and do not add, remove, paraphrase, or combine engineering claims. NEVER use markdown links or footnotes.**`
          : ''
      }`,
      schema: fileSearchJsonSchema,
    },
  );
};

module.exports = { createFileSearchTool, primeFiles, fileSearchJsonSchema };
