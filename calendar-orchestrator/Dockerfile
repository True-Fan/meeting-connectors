# syntax=docker/dockerfile:1

# The calendar orchestrator: polls Google Calendar (and optionally Gmail) and POSTs a
# session to meeting-connectors when a meeting is about to start. Its own deployable —
# nothing here imports meeting-connectors, it only speaks to its HTTP API.

FROM python:3.12-slim-bookworm AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY scripts/ ./scripts/

# **Both of these are runtime state, and both default to paths relative to the working
# directory** (`credentials/token.json`, `state/triggered_events.json`,
# `state/processed_invites.json`). Created and owned here so a fresh named volume inherits
# the ownership; mounted as volumes in compose because losing either one has consequences:
# the token means re-running the OAuth flow by hand, and the state files are what stop the
# orchestrator re-joining a meeting it has already handled on every restart.
RUN mkdir -p /app/credentials /app/state \
    && useradd --create-home --uid 10003 orch \
    && chown -R orch:orch /app

USER orch

VOLUME ["/app/credentials", "/app/state"]

EXPOSE 8090

# Port 8090 rather than 8000: meeting-connectors owns 8000 in this stack, and the
# orchestrator's own HTTP surface is small — health, a forced sync, and an inspection of
# what is currently scheduled.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8090"]
