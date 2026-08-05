# Python bridge. The C++ publisher sidecar is a separate image (M5) so that a
# native crash cannot take the bridge down with it — see doc 001 §3.2.

FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    POETRY_VERSION=1.8.3 \
    POETRY_VIRTUALENVS_CREATE=false

# ffmpeg is used by FfmpegDecoder from M4 onward; installed here so the image is
# stable across milestones rather than changing under M4.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg curl \
    && rm -rf /var/lib/apt/lists/*

RUN pip install "poetry==${POETRY_VERSION}"

WORKDIR /app

# ---------------------------------------------------------------- deps layer
COPY pyproject.toml poetry.lock* README.md ./
RUN poetry install --no-root --only main

# ---------------------------------------------------------------- app layer
COPY src ./src

RUN groupadd --system app \
    && useradd --system --gid app --home /app app \
    && mkdir -p /run/meeting-connectors \
    && chown -R app:app /app /run/meeting-connectors
USER app

EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=3 \
    CMD curl -fsS http://localhost:8000/health || exit 1

CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]
