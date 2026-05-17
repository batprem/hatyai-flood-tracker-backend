# syntax=docker/dockerfile:1.7
# Production image for the Hat Yai flood warning backend.
#
# The image is uv-based and installs project dependencies from
# ``pyproject.toml`` plus ``uv.lock``. The eccodes C library is required at
# runtime because the GFS ingestion client decodes GRIB2 messages with the
# Python ``eccodes`` binding, so we install ``libeccodes-dev`` from Debian
# packages in both the build and runtime stages.

FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim AS builder

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        libeccodes-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install dependencies first to leverage Docker layer caching.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

COPY app ./app
COPY main.py ./main.py

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev


FROM python:3.13-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:${PATH}" \
    UV_PROJECT_ENVIRONMENT=/opt/venv

# eccodes runtime: ``libeccodes0`` ships the shared libraries the Python
# binding loads at import time. Keep curl available for container healthchecks.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libeccodes0 \
        curl \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system app \
    && useradd --system --gid app --home /app --shell /usr/sbin/nologin app

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /usr/local/bin/uv /usr/local/bin/uv
COPY --from=builder /app/app ./app
COPY --from=builder /app/main.py ./main.py
COPY pyproject.toml uv.lock ./

USER app

EXPOSE 8000

# Default to the API process. The GFS ingestion cron runs as a separate
# Cloud Run Job (see deploy/gfs-ingest-job.yaml).
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
