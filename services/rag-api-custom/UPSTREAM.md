# RAG API runtime overlay

| Item | Pinned value |
| --- | --- |
| Upstream image | `ghcr.io/danny-avila/librechat-rag-api-dev-lite@sha256:c0ad82657b556c1e16dcfca85d045788f67caa223e25e70eb687f4d16b41dedc` |
| Upstream commit | `12a446950f952a00ad16d5d90d2163bb69f81de1` |
| Upstream build date | `2026-06-18T21:44:03Z` |

The overlay adds an optional `entity_id` to `/query_multiple`, bounds the number of supplied file IDs, and filters returned results to the same namespace semantics used by `/query`.

LibreChat remains the primary authorization boundary: it resolves Agent permissions in MongoDB and sends only the resulting allow-listed file IDs. The RAG namespace check is defense in depth and does not replace LibreChat authorization.
