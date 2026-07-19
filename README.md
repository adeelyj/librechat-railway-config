# RapidDraft LibreChat Railway configuration

This repository contains the non-secret LibreChat configuration used by the
`bk-RAG-test` Railway project in its `testing` environment.

The deployment exposes only the RapidDraft Local AI custom endpoint and uses
LibreChat Agents with file search for logically isolated knowledge bases.

## Runtime secrets

Secrets are injected through Railway and are never committed here:

- `LOCALAI_CHAT_API_KEY`
- `LOCALAI_CHAT_BASE_URL`
- MongoDB, Meilisearch, PostgreSQL, and object-storage credentials

## Knowledge bases

- `Test Archive`
- `Bauer Kompressoren`

Each knowledge base is represented by a private Agent with a distinct file set
and access-control list. Cross-knowledge-base retrieval is covered by the
deployment acceptance tests.

