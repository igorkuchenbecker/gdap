# syntax=docker/dockerfile:1.7
# ── build stage ────────────────────────────────────────────────────────────────
FROM python:3.13-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    UV_LINK_MODE=copy

RUN apt-get update && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*
RUN pip install uv

WORKDIR /build
COPY pyproject.toml README.md ./
COPY src ./src
RUN uv pip install --system --no-cache ".[postgres]"

# ── runtime stage ──────────────────────────────────────────────────────────────
FROM python:3.13-slim AS runtime

# Run as a non-root user: the container writes only to its data volume.
RUN groupadd --system gdap && useradd --system --gid gdap --home /home/gdap --create-home gdap

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    GDAP_HOME=/data \
    GDAP_ENVIRONMENT=production \
    GDAP_API__HOST=0.0.0.0

COPY --from=builder /usr/local/lib/python3.13/site-packages /usr/local/lib/python3.13/site-packages
COPY --from=builder /usr/local/bin/gdap /usr/local/bin/gdap

WORKDIR /app
COPY config ./config
COPY web ./web
COPY examples ./examples
COPY migrations ./migrations
COPY alembic.ini ./

RUN mkdir -p /data && chown -R gdap:gdap /data /app
USER gdap
VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/readyz', timeout=4).status==200 else 1)"

# The API by default; the same image runs the worker with: gdap worker start
CMD ["gdap", "system", "serve"]
