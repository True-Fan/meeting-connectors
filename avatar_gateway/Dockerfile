# syntax=docker/dockerfile:1

# The avatar gateway: meeting-connectors' PCM/fMP4 protocol on one side, a LiveKit room on
# the other, and a call into realtime-product's Initialisation service to get an
# agent-worker into that room.
#
# Built separately from meeting-connectors because it is a separate deployable with its own
# dependency set — LiveKit and numpy here, Playwright and Chromium there — and nothing is
# shared but the WebSocket contract between them.

# ---------------------------------------------------------------- dependencies

FROM python:3.12-slim-bookworm AS deps

# uv, because uv.lock is the lockfile this project actually keeps. Installing with pip from
# pyproject.toml would ignore it and re-resolve `httpx>=0.27` and `numpy>=1.26` on every
# build — and numpy minors are exactly the kind of thing that changes an audio path's
# behaviour without changing a line of this code.
RUN pip install --no-cache-dir uv

WORKDIR /build
COPY pyproject.toml uv.lock ./

# `package = false` in pyproject: these are two scripts, not an installable package, so
# only the dependencies are synced. --frozen fails the build if uv.lock has drifted from
# pyproject.toml rather than quietly resolving something else.
RUN uv sync --frozen --no-dev

# ---------------------------------------------------------------- runtime

FROM python:3.12-slim-bookworm AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# **ffmpeg is not optional here.** The gateway muxes the agent's speech and a video leg into
# the fMP4 fragments the bridge expects, and it shells out to ffmpeg to do it — with the
# command line fixed at startup. Without the binary the muxer fails on the first session,
# after the bridge has already connected.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY --from=deps /build/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH"

COPY gateway.py init_client.py ./

# **Bound to every interface, unlike the default.** GATEWAY_HOST defaults to 127.0.0.1,
# which is right for a process on a laptop and useless in a container: the connectors
# service is a different container, so a loopback-bound socket is unreachable and the
# bridge's connection is refused with nothing logged on this side at all.
ENV GATEWAY_HOST=0.0.0.0 \
    GATEWAY_PORT=8300

RUN useradd --create-home --uid 10002 gw && chown -R gw:gw /app
USER gw

EXPOSE 8300

CMD ["python", "gateway.py"]
