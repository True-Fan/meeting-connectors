"""Prepare the Chromium profile the Zoom-web connector joins with.

Run **once**, interactively. It opens a visible browser against the profile
directory the connector will use, so you can sign in to Zoom and — the part that
actually matters — **select a microphone**.

WHY THE MICROPHONE SELECTION IS THE POINT
-----------------------------------------
The connector publishes the avatar through a synthetic ``MediaStreamTrack`` injected
into the page, exactly as the Google Meet connector does. That works only if Zoom
starts its capture pipeline, and Zoom will not start it until its device menu has a
selection. Chromium stores that selection per origin in ``Default/Preferences``,
inside the profile — so it survives into every later session, and a throwaway profile
has none.

This was measured the hard way: with a fresh profile Zoom's microphone menu showed no
device checked, its pipeline stayed idle, and the injected track was consumed and
never transmitted. Joining the same meeting from a normal signed-in browser had a
microphone selected by default and worked.

    python scripts/zoom_web_login.py --profile ~/.mc/zoom-web-profile

Then set the same path:

    MC_ZOOM_WEB__ENABLED=true
    MC_ZOOM_WEB__PROFILE_DIR=~/.mc/zoom-web-profile
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import sys
from pathlib import Path

CHECKLIST = """
In the browser window that just opened:

  1. Sign in to Zoom (zoom.us) with the account the avatar should join as.
  2. Join any meeting — your own Personal Meeting Room is ideal.
  3. Click **Join Audio by Computer** when prompted.
  4. Open the caret beside Mute -> **Select a Microphone**, and click one so it
     shows a checkmark. THIS STEP IS THE WHOLE POINT: without a selected device
     Zoom never starts capturing, and the avatar is silent.
  5. Leave the meeting.

Then close the browser window, or press Ctrl-C here.
"""


async def run(profile: Path, url: str) -> int:
    from playwright.async_api import async_playwright

    print(f"→ profile: {profile}")

    async with async_playwright() as pw:
        context = await pw.chromium.launch_persistent_context(
            str(profile),
            headless=False,
            args=[
                # Match the connector's launch so the preferences written here are
                # the ones it will read. Notably NOT --use-fake-ui-for-media-stream:
                # you want the real permission prompt, because granting it is part
                # of what gets persisted.
                "--autoplay-policy=no-user-gesture-required",
                "--disable-blink-features=AutomationControlled",
                "--no-first-run",
                "--no-default-browser-check",
            ],
            permissions=["microphone", "camera"],
        )
        page = context.pages[0] if context.pages else await context.new_page()
        await page.goto(url)
        print(CHECKLIST)

        # Wait for the operator to finish and close the window. The event is set by
        # Playwright when the browser goes away, so Ctrl-C and closing the window
        # both end this cleanly — and the profile is only written on a clean close.
        finished = asyncio.Event()
        context.on("close", lambda _: finished.set())
        with contextlib.suppress(KeyboardInterrupt, asyncio.CancelledError):
            await finished.wait()
        with contextlib.suppress(Exception):
            await context.close()

    print(f"\n✓ profile saved: {profile}")
    print("  set MC_ZOOM_WEB__PROFILE_DIR to this path, and restart the service.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--profile", required=True, help="directory to create the profile in"
    )
    parser.add_argument("--url", default="https://zoom.us/signin")
    args = parser.parse_args()
    profile = Path(args.profile).expanduser()
    # Created here rather than inside the coroutine: a blocking filesystem call in
    # an async function is a lint error, and it is genuinely setup, not runtime.
    profile.mkdir(parents=True, exist_ok=True)
    return asyncio.run(run(profile, args.url))


if __name__ == "__main__":
    sys.exit(main())
