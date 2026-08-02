# --- builder: resolve and install dependencies with uv ---------------------------
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim AS builder

ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
WORKDIR /app

# Dependency layer (cached until pyproject/uv.lock change)
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --group embeddings --no-install-project

# Project layer
COPY . .
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --group embeddings

# --- runtime ----------------------------------------------------------------------
FROM python:3.13-slim-bookworm

RUN useradd --system --uid 10001 --create-home apilot \
    && mkdir -p /data/uploads \
    && chown apilot:apilot /data/uploads

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY --from=builder --chown=apilot:apilot /app /app

USER apilot
# 8000 API, 9110 the worker's own metrics exporter (the worker has no HTTP server
# of its own, and the pipeline's metrics are incremented there).
EXPOSE 8000 9110

# Explicit, so an orchestrator that reads image metadata gets the same signal the
# runtime expects rather than inferring it.
STOPSIGNAL SIGTERM

# Liveness for plain `docker run`; Kubernetes uses the probes in deploy/k8s instead.
HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health')"

# --timeout-graceful-shutdown bounds how long uvicorn waits for in-flight requests
# before closing; without it the only bound is the orchestrator's SIGKILL.
# Same image serves the worker via a command override (arq).
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--timeout-graceful-shutdown", "20"]
