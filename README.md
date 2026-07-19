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

Authenticated users may create and share private Agents. Public Agents remain
disabled. This preserves per-Agent file isolation while allowing administrators
to add future company or project knowledge bases without a deployment change.

## Admin bootstrap

`scripts/bootstrap-librechat-admin.ps1` creates or verifies the first LibreChat
administrator. Its generated password is stored as a DPAPI-encrypted PowerShell
credential outside this repository and is readable only by the Windows user who
created it.
