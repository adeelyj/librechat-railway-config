# LibreChat runtime overlay

This image overlays the file-search implementation on the upstream LibreChat development image.

| Item | Pinned value |
| --- | --- |
| Upstream image | `ghcr.io/danny-avila/librechat-dev@sha256:3d0a3031a97afb58dd79d1cffd9c5ad3572e1dc13d8084575814c6bbadc18daf` |
| Upstream commit | `8e5ef1fb31e9d63b735c089b21cbc82c50acce46` |
| Upstream build date | `2026-07-16T15:31:27Z` |
| Base `fileSearch.js` SHA-256 | `1433f681d27979ee73389b18a9e4e2204aaeae201a675719004c4e79ad4f4b34` |

The overlay keeps Agent knowledge-base filenames out of the model-visible prompt and replaces per-file RAG fan-out with authorized batch queries. The base-file checksum in the Dockerfile intentionally stops the build if the upstream runtime changes.

Upgrade procedure:

1. Record the new image digest and `/api/config` build commit.
2. Diff upstream `fileSearch.js` against this overlay.
3. Rebase the overlay and tests.
4. Update the base-file checksum and pinned image digest.
5. Run the Test Archive and Bauer acceptance suites before deployment.
