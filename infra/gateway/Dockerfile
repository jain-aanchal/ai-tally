# Ingest gateway image. Build context is the repo root (see docker-compose `context: ..`) so we can
# COPY both the gateway and the SDK it depends on.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# SDK first (the gateway installs it as a path dependency).
COPY sdk/python /app/sdk/python
COPY infra/gateway /app/infra/gateway

# Install the SDK, then the gateway. We resolve `tally` from the local path rather than PyPI.
# CTO-241: install the [s3] extra so boto3 is present for the s3 replay-blob backend the
# docker-compose stack selects by default (persisting replay bodies in MinIO across restarts).
RUN pip install /app/sdk/python \
 && pip install "/app/infra/gateway[s3]"

EXPOSE 8080

CMD ["uvicorn", "gateway.app:app", "--host", "0.0.0.0", "--port", "8080"]
