# syntax=docker/dockerfile:1

# ---- base -------------------------------------------------------------------
# The uv image ships both uv and a pinned CPython, so no interpreter is
# downloaded at build time.
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS base
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH"
WORKDIR /app

# ---- dependencies -----------------------------------------------------------
# Dependencies are installed from the lockfile in their own layer so that
# application edits do not invalidate the dependency cache.
FROM base AS deps
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

FROM base AS deps-dev
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project

# ---- development ------------------------------------------------------------
# Source is bind-mounted by Compose; only the virtualenv is baked in.
FROM base AS dev
COPY --from=deps-dev /app/.venv /app/.venv
COPY . .
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]

# ---- production -------------------------------------------------------------
FROM base AS runtime
RUN groupadd --system app && useradd --system --gid app --home-dir /app app
COPY --from=deps /app/.venv /app/.venv
COPY . .
RUN chown -R app:app /app
USER app
EXPOSE 8000
# urllib keeps the image free of a curl dependency just for the probe.
HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health/live', timeout=4).status == 200 else 1)"
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
