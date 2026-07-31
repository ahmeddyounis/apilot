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
EXPOSE 8000

# Same image serves the worker via a compose command override (arq).
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
