FROM ghcr.io/danny-avila/librechat-dev@sha256:3d0a3031a97afb58dd79d1cffd9c5ad3572e1dc13d8084575814c6bbadc18daf

WORKDIR /app

# The checksum binds this overlay to LibreChat commit
# 8e5ef1fb31e9d63b735c089b21cbc82c50acce46. An upstream image change must
# be reviewed rather than silently accepting a potentially incompatible file.
RUN echo "1433f681d27979ee73389b18a9e4e2204aaeae201a675719004c4e79ad4f4b34  /app/api/app/clients/tools/util/fileSearch.js" | sha256sum -c -
RUN echo "e8ab5bcb5cd8e2dd47f46f382de15f4b4deb438eb6ec29a030b1205900328147  /app/api/server/services/Endpoints/agents/initialize.js" | sha256sum -c -
RUN echo "0d4758dab28293fc3f68914183acb495e3880fa53401017ce4587b56ad1bd922  /app/api/app/clients/tools/util/handleTools.js" | sha256sum -c -

COPY fileSearchBatch.js /app/api/app/clients/tools/util/fileSearchBatch.js
COPY v3Authorization.js /app/api/app/clients/tools/util/v3Authorization.js
COPY fileSearch.js /app/api/app/clients/tools/util/fileSearch.js
COPY bauerV3FinalBoundary.js /app/api/server/services/Endpoints/agents/bauerV3FinalBoundary.js
COPY patchBauerV3FinalBoundary.js /tmp/patchBauerV3FinalBoundary.js
COPY patchBauerV3FileSearchRequest.js /tmp/patchBauerV3FileSearchRequest.js
COPY patchBauerV3RunGraph.js /tmp/patchBauerV3RunGraph.js
RUN node /tmp/patchBauerV3FileSearchRequest.js /app/api/app/clients/tools/util/handleTools.js \
    && echo "dadb1a782c43d1afd367159b6e0f896348a246977debea18fd0d23af46b5ef32  /app/api/app/clients/tools/util/handleTools.js" | sha256sum -c -
RUN node /tmp/patchBauerV3FinalBoundary.js /app/api/server/services/Endpoints/agents/initialize.js \
    && echo "3586ebdfefdc44c07eac5d5b4b2ff94988a02a03911cb54202377bf2ccc77d85  /app/api/server/services/Endpoints/agents/initialize.js" | sha256sum -c -
RUN node /tmp/patchBauerV3RunGraph.js /app/packages/api/dist/index.cjs
