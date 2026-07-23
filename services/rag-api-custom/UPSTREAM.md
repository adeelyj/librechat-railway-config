# RAG API runtime overlay

| Item | Pinned value |
| --- | --- |
| Upstream image | `ghcr.io/danny-avila/librechat-rag-api-dev-lite@sha256:c0ad82657b556c1e16dcfca85d045788f67caa223e25e70eb687f4d16b41dedc` |
| Upstream commit | `12a446950f952a00ad16d5d90d2163bb69f81de1` |
| Upstream build date | `2026-06-18T21:44:03Z` |
| Image `models.py` SHA-256 | `4a08ee6c1c663dbaa2d359941557865177d29d715aa445f725a7cb8ff632f318` |
| Image `document_routes.py` SHA-256 | `a9679cf143161ab79e224dd82d3ce6971fd3c30d8d7c9455c52d51f20f3806a6` |
| Image `main.py` SHA-256 | `63a0178c2f63abebac08f220b2716ea139931bc14641f7d751f66c06e1d5a10f` |

The overlay adds an optional `entity_id` to `/query_multiple`, bounds the number of supplied file IDs, and filters returned results to the same namespace semantics used by `/query`.

LibreChat remains the primary authorization boundary: it resolves Agent permissions in MongoDB and sends only the resulting allow-listed file IDs. The RAG namespace check is defense in depth and does not replace LibreChat authorization.

The Bauer V2 addition is isolated under `/app/app/bauer_rag_v2`, creates only the additive
`bauer_rag_v2` PostgreSQL schema, and mounts `/query_v2`, administrative indexing routes, and the
deterministic validator. V1 tables and routes are not rewritten. `main.py` is checksum-pinned so
startup migration and router integration cannot silently drift from the reviewed upstream image.
