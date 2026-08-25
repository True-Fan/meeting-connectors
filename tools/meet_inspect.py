#!/usr/bin/env python
"""Harvest the live Google Meet DOM, and report which of our selectors match.

**Why this exists.** ``automation/selectors.py`` is the connector's most fragile surface —
Meet's markup is machine-generated and its class names are build artefacts. Section 9 of
``setup-google-meet.txt`` tells an operator to right-click the real button and Inspect it, which
works only in a headed browser. Headed Chromium crashes on real Meet (see ``docs/design/007``
§8), so the browser that actually runs this connector is headless and there is nothing to
right-click. This is the replacement: it dumps every candidate element the way Inspect would,
and states plainly which of our selectors hit and which missed.

    .venv/bin/python tools/meet_inspect.py                       # create a scratch meeting
    .venv/bin/python tools/meet_inspect.py --code abc-defg-hij   # inspect a real one
    .venv/bin/python tools/meet_inspect.py --code abc-defg-hij --join

``--code`` is what you want against a meeting a human is hosting; the default creates an
**ephemeral meeting on the avatar's own account** via ``/new``, which is enough to harvest the
pre-join and in-call selectors without involving anybody.

``--join`` additionally runs the real ``MeetJoiner`` and reports the stage it fails at, then
dumps the DOM as it stood at that moment — which is the useful artefact when a join breaks.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config.settings import Settings
from src.connectors.google_meet.automation.driver import PlaywrightDriver
from src.connectors.google_meet.automation.selectors import DEFAULT_SELECTORS
from src.connectors.google_meet.browser.launcher import build_launch_plan
from src.connectors.google_meet.browser.profile import ProfileManager
from src.connectors.google_meet.config import GoogleMeetConnectorConfig
from src.connectors.google_meet.meeting.join import MeetJoiner
from src.connectors.google_meet.meeting.meet_url import MeetJoinTarget, canonical_url

# Every attribute worth seeing, in the order a human would look at them. ``aria-label`` first
# because it is the only one Meet is contractually obliged to keep meaningful.
HARVEST = """
() => {
  const interesting = (el) => {
    const r = el.getBoundingClientRect();
    return {
      tag: el.tagName.toLowerCase(),
      role: el.getAttribute('role'),
      aria: el.getAttribute('aria-label'),
      text: (el.innerText || '').trim().slice(0, 60),
      jsname: el.getAttribute('jsname'),
      dataMuted: el.getAttribute('data-is-muted'),
      dataId: el.getAttribute('data-participant-id') || el.getAttribute('data-meeting-code'),
      disabled: el.disabled === true || el.getAttribute('aria-disabled') === 'true',
      visible: r.width > 0 && r.height > 0,
      box: [Math.round(r.x), Math.round(r.y), Math.round(r.width), Math.round(r.height)],
    };
  };
  const nodes = [...document.querySelectorAll(
    'button, [role="button"], input, [data-meeting-code]'
  )];
  return {
    url: location.href,
    title: document.title,
    bodyText: (document.body ? document.body.innerText : '').slice(0, 900),
    elements: nodes.map(interesting).filter(e => e.visible || e.aria || e.text),
  };
}
"""


async def harvest(driver: PlaywrightDriver) -> dict:
    return await driver.evaluate(HARVEST)


async def report_matches(driver: PlaywrightDriver) -> None:
    """Which of our selector candidates actually resolve on this page."""
    s = DEFAULT_SELECTORS
    groups = {
        "join_button": s.join_button,
        "in_call": s.in_call,
        "leave": s.leave,
        "mute_toggle (matches when UNMUTED)": s.mute_toggle,
        "unmute_toggle (matches when MUTED)": s.unmute_toggle,
        "camera_on_toggle (matches when OFF)": s.camera_on_toggle,
        "camera_off_toggle (matches when ON)": s.camera_off_toggle,
        "lobby": s.lobby,
        "name_input": s.name_input,
        "dismiss_buttons": s.dismiss_buttons,
        "participant": s.participant,
    }
    print("\n  SELECTOR MATCHES")
    for label, candidates in groups.items():
        hit = await driver.wait_for_any(candidates, timeout_s=0.0)
        mark = "MATCH" if hit else "  -  "
        print(f"    [{mark}] {label}")
        if hit:
            print(f"             -> {hit}")


def print_dom(snapshot: dict) -> None:
    print(f"\n  url   : {snapshot['url']}")
    print(f"  title : {snapshot['title']!r}")
    body = " | ".join(x for x in snapshot["bodyText"].splitlines() if x.strip())[:300]
    print(f"  body  : {body!r}")
    print(f"\n  CLICKABLE ELEMENTS ({len(snapshot['elements'])})")
    for e in snapshot["elements"]:
        bits = [f"<{e['tag']}>"]
        if e["role"]:
            bits.append(f"role={e['role']}")
        if e["aria"]:
            bits.append(f'aria-label="{e["aria"]}"')
        if e["text"]:
            bits.append(f"text={e['text']!r}")
        if e["jsname"]:
            bits.append(f"jsname={e['jsname']}")
        if e["dataMuted"] is not None:
            bits.append(f"data-is-muted={e['dataMuted']}")
        if e["dataId"]:
            bits.append(f"data-id={e['dataId']}")
        if e["disabled"]:
            bits.append("DISABLED")
        if not e["visible"]:
            bits.append("hidden")
        print("    " + "  ".join(bits))


async def run(args: argparse.Namespace) -> int:
    settings = Settings()
    config = GoogleMeetConnectorConfig.from_settings(settings)
    profiles = ProfileManager(template=config.require_configured())
    lease = profiles.acquire("inspect")

    driver = PlaywrightDriver()
    plan = build_launch_plan(
        user_data_dir=lease.path,
        video_format=config.video_format,
        headless=config.headless,
        executable_path=config.chromium_executable,
        extra_args=config.extra_browser_args,
    )
    await driver.start(plan)

    out = Path(args.out)
    try:
        url = canonical_url(args.code) if args.code else "https://meet.google.com/new"
        print(f"\n  navigating to {url}")
        await driver.goto(url, timeout_s=config.join_timeout_s)
        await asyncio.sleep(args.settle)

        landed = driver.current_url()
        print(f"  landed on     {landed}")

        snapshot = await harvest(driver)
        print_dom(snapshot)
        await report_matches(driver)

        if args.join:
            print("\n  running the real MeetJoiner…")
            target = MeetJoinTarget(url=landed, meeting_code=args.code)
            joiner = MeetJoiner(
                driver=driver,
                selectors=DEFAULT_SELECTORS,
                display_name=config.display_name,
                join_timeout_s=config.join_timeout_s,
                lobby_timeout_s=args.lobby_timeout,
            )
            try:
                outcome = await joiner.join(target)
                print(f"  JOINED: state={outcome.state} button={outcome.matched_join_button!r}")
                await asyncio.sleep(args.settle)
                print("\n  ==== POST-JOIN DOM ====")
                print_dom(await harvest(driver))
                await report_matches(driver)
            except Exception as exc:
                print(f"  JOIN FAILED: {type(exc).__name__}: {exc}")
                print("\n  ==== DOM AT FAILURE ====")
                print_dom(await harvest(driver))
                await report_matches(driver)

        await driver.screenshot(out.with_suffix(".png"))
        final = await harvest(driver)
        await asyncio.to_thread(
            out.write_text, json.dumps(final, indent=2), encoding="utf-8"
        )
        print(f"\n  screenshot -> {out.with_suffix('.png')}")
        print(f"  dom json   -> {out}\n")
        return 0
    finally:
        await driver.stop()
        profiles.release(lease)


def main() -> int:
    p = argparse.ArgumentParser(description="Harvest the live Meet DOM.")
    p.add_argument("--code", help="meeting code, e.g. abc-defg-hij (default: create a scratch one)")
    p.add_argument("--join", action="store_true", help="also run the real MeetJoiner")
    p.add_argument("--settle", type=float, default=6.0, help="seconds to let Meet render")
    p.add_argument("--lobby-timeout", type=float, default=60.0)
    p.add_argument("--out", default="meet-dom.json")
    return asyncio.run(run(p.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
