#!/usr/bin/env python
"""One-off: sign the Google Meet browser profile in to a Google account.

**Why this exists as a separate tool rather than a mode of the service.** The join flow
verifies the Google session *before* it opens the meeting, deliberately — an unauthenticated
profile otherwise presents as an unexplained join timeout twenty seconds later. The
consequence is that a first run can never sign itself in: ``MeetJoiner`` raises
``GoogleAuthError`` before a human could type anything.

So authentication is a provisioning step, done once, with a human present. That is also the
right shape for it: Google's sign-in can present a second factor, a device-verification
challenge, or an outright "this browser may not be secure" refusal, and scripting past those
repeatedly is what gets an automated account restricted.

    .venv/bin/python tools/meet_signin.py

Writes **directly to the template profile**, not to a per-session clone — the whole point is
to leave a durable session behind for every later run to inherit
(``connectors/google_meet/browser/profile.py``).

Verify an existing profile without touching it:

    .venv/bin/python tools/meet_signin.py --check
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config.settings import Settings
from src.connectors.google_meet.auth.google_login import verify_signed_in
from src.connectors.google_meet.automation.driver import PlaywrightDriver
from src.connectors.google_meet.browser.launcher import build_launch_plan
from src.connectors.google_meet.browser.profile import ProfileManager
from src.connectors.google_meet.config import GoogleMeetConnectorConfig
from src.domain.media import VideoFormat

SIGN_IN_URL = "https://accounts.google.com/"
POLL_SECONDS = 3.0


async def run(*, check_only: bool, timeout_s: float) -> int:
    settings = Settings()  # reads .env, exactly as the service does
    config = GoogleMeetConnectorConfig.from_settings(settings)

    try:
        template = config.require_configured()
    except Exception as exc:
        print(f"\n  x {exc}\n")
        return 2

    profiles = ProfileManager(template=template, clone_per_session=False)
    profiles.ensure_template()

    print(f"\n  profile : {template.resolve()}")
    print(f"  cookies : {'present' if profiles.is_authenticated() else 'absent'}")

    driver = PlaywrightDriver()
    plan = build_launch_plan(
        user_data_dir=template,
        video_format=VideoFormat(width=1280, height=720, fps=25),
        # Headed unless only checking: the entire point is that a human can see and use the
        # sign-in form, including whatever challenge Google decides to present.
        headless=check_only,
        executable_path=config.chromium_executable,
        extra_args=config.extra_browser_args,
    )

    try:
        await driver.start(plan)
    except Exception as exc:
        print(f"\n  x cannot launch chromium: {exc}")
        print("    install it:  .venv/bin/pip install playwright")
        print("                 .venv/bin/playwright install chromium\n")
        return 2

    try:
        status = await verify_signed_in(driver, timeout_s=60.0)
        if status.signed_in:
            print(f"  account : {status.account_hint or 'signed in'}")
            print("\n  OK - this profile is already signed in. Nothing to do.\n")
            return 0

        if check_only:
            print(f"\n  x not signed in: {status.detail}")
            print("    run without --check to sign in interactively.\n")
            return 1

        print(f"  status  : not signed in ({status.detail})")
        print("\n  A Chromium window is open.")
        print("  1. Sign in as the account the AVATAR will join meetings as.")
        print("     Prefer a *different* account from the one hosting the meeting.")
        print("  2. Complete any 2-step verification.")
        print("  3. Leave the browser open - this tool detects success on its own.\n")

        await driver.goto(SIGN_IN_URL, timeout_s=60.0)

        deadline = asyncio.get_running_loop().time() + timeout_s
        while asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(POLL_SECONDS)
            if not driver.is_alive():
                print("\n  x the browser was closed before sign-in completed.\n")
                return 1
            status = await verify_signed_in(driver, timeout_s=30.0)
            if status.signed_in:
                print(f"\n  OK - signed in as {status.account_hint or 'the chosen account'}")
                print(f"    session saved to {template.resolve()}")
                print("    every later run inherits it; you should not need this tool")
                print("    again unless Google expires the session.\n")
                return 0
            print(f"    ...waiting ({status.detail})")

        print(f"\n  x timed out after {timeout_s:.0f}s.\n")
        return 1
    finally:
        # Closed cleanly so Chromium flushes its cookie store to disk. Killing the process
        # here can leave the profile without the session that was just established.
        await driver.stop()


def main() -> int:
    parser = argparse.ArgumentParser(description="Sign the Meet browser profile in to Google.")
    parser.add_argument(
        "--check",
        action="store_true",
        help="report whether the profile is signed in, headless, without changing it",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=600.0,
        help="how long to wait for the human to finish (default: 600s)",
    )
    args = parser.parse_args()
    return asyncio.run(run(check_only=args.check, timeout_s=args.timeout))


if __name__ == "__main__":
    raise SystemExit(main())
