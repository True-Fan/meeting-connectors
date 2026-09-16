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
