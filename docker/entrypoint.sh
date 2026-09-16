#!/usr/bin/env bash
# Dispatch for the two things this image does. Anything unrecognised is exec'd verbatim,
# so `docker compose run serve python tools/meet_signin.py --check` still works.
set -euo pipefail

DISPLAY_NUM="${DISPLAY_NUM:-99}"
SCREEN_GEOMETRY="${SCREEN_GEOMETRY:-1600x1000x24}"
VNC_PORT="${VNC_PORT:-5900}"
NOVNC_PORT="${NOVNC_PORT:-7900}"

start_display() {
    echo "==> starting Xvfb on :${DISPLAY_NUM} (${SCREEN_GEOMETRY})"
    Xvfb ":${DISPLAY_NUM}" -screen 0 "${SCREEN_GEOMETRY}" -nolisten tcp &

    # Poll for the socket rather than sleeping a fixed interval: Chromium launched against
    # a display that is not up yet dies with "Missing X server", which reads like a
    # configuration fault rather than a race.
    for _ in $(seq 1 50); do
        [ -e "/tmp/.X11-unix/X${DISPLAY_NUM}" ] && break
        sleep 0.1
    done
    if [ ! -e "/tmp/.X11-unix/X${DISPLAY_NUM}" ]; then
        echo "!! Xvfb did not come up on :${DISPLAY_NUM}" >&2
        exit 1
    fi

    export DISPLAY=":${DISPLAY_NUM}"

    # -nopw is deliberate and safe *only* because nothing is published off-host: compose
    # binds noVNC to 127.0.0.1. Do not expose this port on a shared machine — the session
    # on the other end is an authenticated browser.
    echo "==> starting x11vnc on :${VNC_PORT}"
    x11vnc -display ":${DISPLAY_NUM}" -rfbport "${VNC_PORT}" \
        -forever -shared -nopw -quiet -noxdamage &

    echo "==> starting noVNC on :${NOVNC_PORT}"
    websockify --web=/usr/share/novnc "${NOVNC_PORT}" "localhost:${VNC_PORT}" >/dev/null 2>&1 &
}

start_audio() {
    # **Both modes need this, for different halves of the same problem.** At sign-in Zoom
    # needs a microphone it can offer in its device menu; at join time it needs the device
    # that its stored preference names to still exist. A null sink with a source remapped
    # off its monitor supplies one without a sound card, without privileges, and with a
    # stable name across both images.
    mkdir -p "${XDG_RUNTIME_DIR:-/tmp/pulse}"

    if ! pulseaudio --check 2>/dev/null; then
        echo "==> starting pulseaudio"
        # --exit-idle-time=-1 because nothing is connected at startup and Pulse would
        # otherwise shut down before the browser ever asks for a device.
        pulseaudio --start --exit-idle-time=-1 --disallow-exit 2>/dev/null || {
            echo "!! pulseaudio failed to start - Zoom will have no microphone to select" >&2
            return 0
        }
    fi

    # Idempotent: loading these twice would present duplicate devices in Zoom's menu,
    # which is its own kind of confusing.
    if ! pactl list short sinks 2>/dev/null | grep -q VirtualOutput; then
        pactl load-module module-null-sink \
            sink_name=VirtualOutput \
            sink_properties=device.description=VirtualOutput >/dev/null
    fi
    if ! pactl list short sources 2>/dev/null | grep -q VirtualMic; then
        # A remapped source rather than the raw monitor: Chromium lists monitors
        # separately and Zoom does not always treat one as a usable capture device.
        pactl load-module module-remap-source \
            source_name=VirtualMic \
            master=VirtualOutput.monitor \
            source_properties=device.description=VirtualMic >/dev/null
    fi

    echo "==> audio: $(pactl list short sources 2>/dev/null | wc -l | tr -d ' ') source(s) available"
}

login_banner() {
    cat <<'BANNER'

  ------------------------------------------------------------------
   Open  http://localhost:7900/vnc.html  and run, in this shell:

     python tools/meet_signin.py
     python scripts/zoom_web_login.py  --profile /var/lib/mc/zoom-web-profile
     python scripts/teams_web_login.py --profile /var/lib/mc/teams-web-profile

   Zoom: you MUST pick a microphone in the meeting's device menu. Zoom
   will not start capturing without one, and the avatar then joins,
   reports healthy, and publishes silence.

   Verify Meet when done:  python tools/meet_signin.py --check

   Profiles persist in the mc-profiles volume. Exit when finished.
  ------------------------------------------------------------------

BANNER
}

# **Every mode, including the `*)` passthrough.** It was originally called only from
# `serve` and `login`, which left an ad-hoc `docker compose run serve python ...` — the
# shape every diagnostic takes — with no microphone at all, and a Chromium with no audio
# input is the one failure this image cannot detect on its own: getUserMedia simply
# rejects, and the connector reports a page that never attached. start_audio is idempotent,
# so a second invocation inside a running container is a no-op rather than a duplicate
# device in Zoom's menu.
start_audio

case "${1:-serve}" in
    serve)
        shift || true
        exec uvicorn src.main:app --host 0.0.0.0 --port "${MC_PORT:-8000}" "$@"
        ;;
    login)
        shift || true
        start_display
        login_banner
        if [ "$#" -gt 0 ]; then
            exec "$@"
        fi
        exec bash
        ;;
    *)
        exec "$@"
        ;;
esac
