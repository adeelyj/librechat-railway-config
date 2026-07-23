FROM ghcr.io/danny-avila/librechat-rag-api-dev-lite@sha256:c0ad82657b556c1e16dcfca85d045788f67caa223e25e70eb687f4d16b41dedc

WORKDIR /app

COPY batch_authorization.py /app/app/batch_authorization.py
COPY bauer_rag_v2 /app/app/bauer_rag_v2
COPY apply_batch_patch.py /tmp/apply_batch_patch.py
RUN python /tmp/apply_batch_patch.py --root /app && rm /tmp/apply_batch_patch.py
