"""Prepare an optional Chromium profile for the Teams-web connector.

Run **once**, interactively. It opens a visible browser against the profile directory the
connector will use, so you can sign in to Microsoft Teams as the account the avatar should
join as.

**Optional, unlike the Zoom-web equivalent, and it is worth being clear about why.** That
script exists because Zoom refuses to start its capture pipeline until a microphone has been
selected in its own device menu, and Chromium stores that selection inside the profile — so
without one the avatar joins, reports healthy, and publishes nothing. Teams has no such
requirement: it uses the track ``getUserMedia`` returns, so the connector publishes fine from
a throwaway directory.

WHAT A PROFILE BUYS HERE
------------------------
1. **A named join instead of a guest one.** A signed-in profile joins as a tenant user, which
   some organisers require and which usually skips the lobby entirely. An anonymous guest is
   admitted by hand, every time.
2. **Fewer prompts to click past.** Device permission and the "open the Teams app?" launcher
   preference are both persisted, so the join has less to do.

Neither is necessary. If the meetings are open to guests and somebody is there to admit the
avatar, skip this and leave ``MC_TEAMS_WEB__PROFILE_DIR`` unset.

    python scripts/teams_web_login.py --profile ~/.mc/teams-web-profile

Then set the same path:

    MC_TEAMS_WEB__ENABLED=true
    MC_TEAMS_WEB__PROFILE_DIR=~/.mc/teams-web-profile
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import sys
from pathlib import Path

CHECKLIST = """
In the browser window that just opened:

  1. Sign in to Microsoft Teams with the account the avatar should join as.
  2. Choose **Use the web app instead** if Teams offers to open the desktop app. That
     preference is persisted, which is one fewer thing the joiner has to click past.
  3. Grant the microphone permission when prompted, and leave the microphone **unmuted**
     in the pre-join screen of any meeting you open. Teams carries that toggle into the
     next call, and a muted avatar is inaudible while every other signal says the join
     succeeded.
  4. Leave the meeting.

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
                # Match the connector's launch so the preferences written here are the ones it
                # will read. Notably NOT --use-fake-ui-for-media-stream: you want the real
                # permission prompt, because granting it is part of what gets persisted.
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

        # Wait for the operator to finish and close the window. The event is set by Playwright
        # when the browser goes away, so Ctrl-C and closing the window both end this cleanly —
        # and the profile is only written on a clean close.
        finished = asyncio.Event()
        context.on("close", lambda _: finished.set())
        with contextlib.suppress(KeyboardInterrupt, asyncio.CancelledError):
            await finished.wait()
        with contextlib.suppress(Exception):
            await context.close()

    print(f"\n✓ profile saved: {profile}")
    print("  set MC_TEAMS_WEB__PROFILE_DIR to this path, and restart the service.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True, help="directory to create the profile in")
    parser.add_argument("--url", default="https://teams.microsoft.com/v2/")
    args = parser.parse_args()
    profile = Path(args.profile).expanduser()
    # Created here rather than inside the coroutine: a blocking filesystem call in an async
    # function is a lint error, and it is genuinely setup, not runtime.
    profile.mkdir(parents=True, exist_ok=True)
    return asyncio.run(run(profile, args.url))


if __name__ == "__main__":
    sys.exit(main())
