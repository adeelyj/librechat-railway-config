FROM ghcr.io/danny-avila/librechat-dev@sha256:3d0a3031a97afb58dd79d1cffd9c5ad3572e1dc13d8084575814c6bbadc18daf

WORKDIR /app

# The checksum binds this overlay to LibreChat commit
# 8e5ef1fb31e9d63b735c089b21cbc82c50acce46. An upstream image change must
# be reviewed rather than silently accepting a potentially incompatible file.
RUN echo "1433f681d27979ee73389b18a9e4e2204aaeae201a675719004c4e79ad4f4b34  /app/api/app/clients/tools/util/fileSearch.js" | sha256sum -c -

COPY fileSearchBatch.js /app/api/app/clients/tools/util/fileSearchBatch.js
COPY fileSearch.js /app/api/app/clients/tools/util/fileSearch.js
