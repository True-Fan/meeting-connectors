# syntax=docker/dockerfile:1

# Meeting-connectors: one image, two targets, one Chromium.
#
# **Why `login` and `serve` are targets of the same base rather than two images.** The
# credential this service runs on is a Chromium profile, and a profile is not portable
# between platforms: on macOS the cookie-encryption key lives in the login Keychain, on
# Linux it comes from the keyring or a fallback — so a profile signed in on a laptop
# decrypts to nothing in a container and presents as signed *out*. The sign-in therefore
# has to happen on the same Chromium that will later join meetings, which means in here.
# `login` adds a virtual display and a VNC bridge so a human can drive that Chromium; it
# changes nothing else, and both targets share the base layer the browser lives in.
#
# Build:   docker compose build
# Log in:  docker compose run --rm --service-ports login     # then open localhost:7900
# Serve:   docker compose up serve

# ---------------------------------------------------------------- dependencies

FROM python:3.12-slim-bookworm AS deps

# The point of this stage is that the runtime installs *pinned* versions. `pip install .`
# would resolve the `^` ranges in pyproject.toml afresh on every build, so an image built
# today and one built next month could carry different Playwright or FastAPI minors — and a
# Chromium-driving service is exactly where that bites.
RUN pip install --no-cache-dir poetry==1.8.5

WORKDIR /build
COPY pyproject.toml poetry.lock README.md ./

# **`poetry lock --no-update` is here because the committed lockfile is missing playwright.**
# It predates that dependency being promoted from a `google-meet` extra to a hard
# requirement (see the note on it in pyproject.toml), and the failure mode is quiet rather
# than loud: `poetry export` emits a requirements file with no browser driver at all, the
# image builds, and `playwright install` then fails with `playwright: not found` — or worse,
# would fail at session start with `PlaywrightUnavailableError` if the browser step were
# absent too. `--no-update` keeps every version the lockfile already pins and resolves only
# what is genuinely missing, so this adds playwright without quietly bumping anything else.
#
# It re-resolves per build, which is weaker than a committed lock. Regenerating
# poetry.lock in the repo is the real fix; this keeps the image correct until then.
RUN set -eux \
    && poetry lock --no-update --no-interaction \
    && poetry export --only main --without-hashes --format requirements.txt --output requirements.txt \
    && grep -q '^playwright==' requirements.txt

# ---------------------------------------------------------------- base

FROM python:3.12-slim-bookworm AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    # Out of /root/.cache so a non-root user can actually read the browser.
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

COPY --from=deps /build/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# `--with-deps` is what pulls the ~80 shared libraries headless Chromium needs; installing
# the Python package alone leaves a browser that fails to start with a linker error.
# Chromium only — the service never launches firefox or webkit.
RUN playwright install --with-deps chromium \
    && chmod -R a+rX /ms-playwright \
    && rm -rf /var/lib/apt/lists/*

# **A virtual microphone, and why it is in the *base* rather than in `login`.**
# A container has no sound card, so Chromium enumerates zero audio inputs — and Zoom will
# not start its capture pipeline until a microphone has been *selected* in its own device
# menu. With no device there is nothing to select, which makes `zoom_web_login.py`
# impossible to complete and leaves the avatar publishing silence exactly as it does with a
# throwaway profile. PulseAudio's null sink plus a remapped source gives a real, selectable
# device entirely in userspace, with no privileges and no host sound card.
#
# It belongs in the base because the device has to exist *identically* in both images:
# Chromium stores the chosen microphone per origin as a device id, so a device present at
# sign-in and absent at join is a stored preference pointing at nothing.
RUN apt-get update && apt-get install -y --no-install-recommends \
        pulseaudio \
        pulseaudio-utils \
        ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# **ffmpeg is what makes the avatar visible and audible, and leaving it out is invisible
# until a session runs.** `services/media/decoders/ffmpeg.py` spawns one process per
# session — the gateway's fMP4 on stdin, raw audio and video out — and those raw frames are
# the only thing the connector has to publish. Without the binary the decoder dies with a
# bare `FileNotFoundError: [Errno 2]` deep inside uvloop, the avatar joins, the browser
# reports `mic_track` live and `video_published=True`, and the meeting sees a grey tile in
# silence. The watchdog names it precisely if you know to look: `buffered: 0` with
# `underruns` climbing by 240k every five seconds.

# Pulse runs per-user, so it needs somewhere writable for its socket.
ENV XDG_RUNTIME_DIR=/tmp/pulse

COPY src/ ./src/
COPY tools/ ./tools/
COPY scripts/ ./scripts/

COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

# **One volume at the parent, not one per profile.** ProfileManager seeds each session's
# working copy at `<template>/../sessions/<session_id>`, so the templates and the session
# directory have to share a writable parent or every join fails trying to create its copy.
ENV MC_GOOGLE_MEET__PROFILE_DIR=/var/lib/mc/meet-profile \
    MC_ZOOM_WEB__PROFILE_DIR=/var/lib/mc/zoom-web-profile \
    MC_TEAMS_WEB__PROFILE_DIR=/var/lib/mc/teams-web-profile

# Non-root, and the browser is given `--no-sandbox` to match (see docker-compose.yml).
# Ownership is set before the volume exists so a fresh named volume inherits it.
RUN useradd --create-home --uid 10001 mc \
    && mkdir -p /var/lib/mc/sessions \
    && chown -R mc:mc /var/lib/mc /app

USER mc
VOLUME ["/var/lib/mc"]

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]

# ---------------------------------------------------------------- serve

FROM base AS serve

EXPOSE 8000
CMD ["serve"]

# ---------------------------------------------------------------- login

FROM base AS login

# Xvfb gives the headed sign-in Chromium something to draw on; x11vnc exports that display
# and websockify puts noVNC's HTML client in front of it, so the operator needs a browser
# and nothing else installed. Only in this target — the serving image never renders.
USER root
RUN apt-get update && apt-get install -y --no-install-recommends \
        xvfb \
        x11vnc \
        novnc \
        websockify \
    && rm -rf /var/lib/apt/lists/*

# **Xvfb's socket directory, created here because the container does not run as root.**
# Without it Xvfb prints `_XSERVTransmkdir: ERROR: euid != 0, directory /tmp/.X11-unix
# will not be created` and then gets away with creating it anyway, because /tmp is
# world-writable — so the display comes up and the error is noise. That is a bad thing to
# depend on: anything that makes /tmp less permissive (a read-only or tmpfs mount, a
# hardened base) turns the warning into a display that never appears, and the entrypoint
# can only report "Xvfb did not come up". Owning the directory explicitly removes both the
# message and the dependency.
RUN mkdir -p /tmp/.X11-unix && chmod 1777 /tmp/.X11-unix

USER mc

ENV DISPLAY=:99 \
    MC_GOOGLE_MEET__HEADLESS=false \
    MC_ZOOM_WEB__HEADLESS=false \
    MC_TEAMS_WEB__HEADLESS=false

EXPOSE 7900
CMD ["login"]
