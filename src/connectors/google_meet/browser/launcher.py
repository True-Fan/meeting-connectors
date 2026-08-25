"""Chromium launch flags, and why each one is there.

Launch flags are the kind of thing that accretes: someone adds one to fix a symptom,
nobody can later say which fix depended on which flag, and the list becomes untouchable.
So every flag below is grouped by the problem it solves and none is here "just in case".

The one deliberate *omission* is worth as much as the inclusions —
``--use-fake-device-for-media-stream`` is **not** set. It would hand Meet Chromium's own
test pattern if our ``getUserMedia`` patch ever failed to install, which sounds like
useful insurance and is in fact the worst possible outcome: the avatar would appear as a
rolling colour bar and the session would look healthy. Without it, a failed patch means
no video track at all, which surfaces immediately.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.domain.media import VideoFormat

MEDIA_ARGS: tuple[str, ...] = (
    "--use-fake-ui-for-media-stream",
    "--autoplay-policy=no-user-gesture-required",
)
"""The two flags the media path genuinely cannot work without.

``--use-fake-ui-for-media-stream`` auto-accepts the camera and microphone permission
prompt. Without it Meet's ``getUserMedia`` call blocks on a dialog nobody can click, and
in headless mode the dialog is not even rendered — the promise simply never settles.

``--autoplay-policy=no-user-gesture-required`` is the subtler one. Chromium starts an
``AudioContext`` suspended until a user gesture, and a suspended context renders nothing:
the capture worklet is never pulled and the playout worklet produces no samples. There is
no error, no warning, and no event — just a session that joins successfully and carries
silence in both directions. ``bridge.js`` also calls ``resume()`` defensively, but this
flag is what makes the normal path work."""

STABILITY_ARGS: tuple[str, ...] = (
    "--disable-dev-shm-usage",
    "--disable-background-timer-throttling",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
)
"""Flags that keep a headless, containerised Chromium rendering at full rate.

``--disable-dev-shm-usage`` is the container one: Docker's default ``/dev/shm`` is 64 MB,
which a video-carrying renderer exhausts and then crashes with a bare "target closed".

The three ``backgrounding`` flags matter because a headless window is, by Chromium's
reckoning, never visible. Left alone it throttles timers and the compositor for the tab,
which starves ``requestAnimationFrame`` and drops the published frame rate to a few per
second — while every health check still reports a healthy session."""

DISABLED_FEATURES: tuple[str, ...] = (
    "LocalNetworkAccessChecks",
    "CalculateNativeWinOcclusion",
)
"""Chromium features to switch off, as *names* rather than as flags.

**They must end up in one ``--disable-features=`` switch, and that is the whole reason this
is a list of names.** Chromium honours only the last occurrence of a repeated switch, so two
groups each contributing their own ``--disable-features`` silently discards one of them. That
happened during this connector's own bring-up: the Local Network Access flag was added
alongside an existing occlusion flag, the second overwrote the first, and the bridge went on
failing exactly as before with nothing in the argument list looking wrong.
``build_launch_plan`` joins these into a single switch and
``tests/unit/test_google_meet_browser.py`` asserts the switch appears exactly once.

* ``LocalNetworkAccessChecks`` — the flag without which the connector cannot work at all;
  see ``LOCAL_NETWORK_ACCESS_NOTE``.
* ``CalculateNativeWinOcclusion`` — stops Chromium deciding a headless window is occluded and
  throttling its compositor."""

LOCAL_NETWORK_ACCESS_NOTE = """Why LocalNetworkAccessChecks is disabled.

Chromium's **Local Network Access** checks block a page on a public origin from opening a
connection to a loopback address. ``meet.google.com`` is a public origin and the page bridge
listens on ``127.0.0.1``, so every attempt is refused with
``ERR_BLOCKED_BY_LOCAL_NETWORK_ACCESS_CHECKS`` and the injected script never reaches Python —
no audio in, no media out, and the only symptom is a page that silently never connects.

Verified empirically rather than assumed: with the flag absent the WebSocket fails from both
``about:blank`` and a simulated ``https://meet.google.com`` origin, and with it present both
succeed. ``BlockInsecurePrivateNetworkRequests``, the older Private Network Access flag, has no
effect — the feature was renamed, so the obvious flag is the wrong one.

**What this gives up, and what still protects the bridge.** The check exists to stop a hostile
public page from reaching services on the user's own machine. Disabling it is acceptable here
for reasons specific to this deployment, and would not be in a user's browser:

* the browser is ours, headless, in a container, and visits exactly one site;
* the bridge is bound to loopback, so nothing off-host can reach it regardless;
* the URL carries a per-session ``secrets.token_urlsafe`` token compared with
  ``compare_digest``, so even a co-resident process cannot attach (``websocket/server.py``);
* nothing on that wire is a credential — the Google session lives in the browser profile.

It is nonetheless a browser hardening feature being switched off, and the trend is toward
tightening it further. ``docs/design/007`` §6 records the fallback if the flag is ever removed:
move the channel onto Playwright's own CDP transport, which costs base64 framing and a
throughput ceiling but needs no network permission at all."""

AUTOMATION_ARGS: tuple[str, ...] = (
    "--disable-blink-features=AutomationControlled",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-popup-blocking",
    "--disable-notifications",
    "--disable-extensions",
)
"""Keeps the browser out of its own way.

``--disable-blink-features=AutomationControlled`` clears ``navigator.webdriver``. It is
here because Google's sign-in flow treats that flag as a signal and will challenge or
refuse an automated session — which breaks the one part of this connector that cannot be
retried. The rest suppress first-run interstitials and notification prompts that would
otherwise sit on top of the join button."""

SANDBOX_ARGS: tuple[str, ...] = ("--no-sandbox", "--disable-setuid-sandbox")
"""Only applied when ``no_sandbox=True``.

Off by default, and it should stay off wherever the container can be given
``SYS_ADMIN`` or a seccomp profile instead. It exists because many managed container
runtimes cannot, and a Chromium that will not start is worse than one without a second
layer of isolation around a page we already control."""

DEFAULT_LOCALE = "en-US"
"""Pinned so that ``automation/selectors.py``'s English text matching holds. See
``LOCALE_NOTE`` there for what breaks otherwise."""

DEFAULT_TIMEZONE = "UTC"


@dataclass(frozen=True, slots=True)
class LaunchPlan:
    """A fully resolved Chromium launch, ready to hand to Playwright.

    Built and inspected separately from being used, so the argument list is assertable in
    a unit test with no browser installed — which is the only practical way to keep a
    flag list from silently regressing.
    """

    user_data_dir: Path
    args: tuple[str, ...]
    headless: bool
    executable_path: Path | None
    viewport: tuple[int, int]
    locale: str
    timezone_id: str
    timeout_ms: int
    bypass_csp: bool = False
    """Disable the page's own Content Security Policy.

    **Off by default, and the default is what Meet and Zoom-web keep**: neither needs it, and
    the key is omitted from ``to_playwright_kwargs`` entirely when false so their launch is
    byte-for-byte what it was.

    It exists for the Teams-web connector. A page's CSP ``connect-src`` governs *WebSockets*
    too, and a blocked one fails in the least helpful way available: Chromium returns a socket
    object already in ``CLOSED``, throws nothing, and fires neither ``error`` nor ``close`` — so
    the page cannot tell that it was refused, and neither can the bridge. The observed symptom
    was 58 silent retries against a channel that never opened, on an origin whose *earlier*
    pages had connected to the same loopback port perfectly.

    Disabling CSP for a page we already fully control is a narrow cost: the browser is ours,
    headless, visits one site, and the channel it protects is loopback-bound and
    token-authenticated (``page/server.py``). It is nonetheless a page-hardening feature being
    switched off, which is why it is a field with a default rather than something applied to
    every connector."""

    def to_playwright_kwargs(self) -> dict[str, Any]:
        """Keyword arguments for ``chromium.launch_persistent_context``."""
        kwargs: dict[str, Any] = {
            "user_data_dir": str(self.user_data_dir),
            "headless": self.headless,
            "args": list(self.args),
            "viewport": {"width": self.viewport[0], "height": self.viewport[1]},
            "locale": self.locale,
            "timezone_id": self.timezone_id,
            "timeout": self.timeout_ms,
            # Granting up front means Meet's permission query resolves without a prompt
            # even if the fake-UI flag is ever dropped. Two independent mechanisms for
            # the same guarantee, because a permission prompt in a headless browser is
            # unrecoverable rather than merely inconvenient.
            "permissions": ["camera", "microphone"],
            "ignore_default_args": ["--mute-audio"],
        }
        if self.executable_path is not None:
            kwargs["executable_path"] = str(self.executable_path)
        # Added only when asked for, so a connector that does not want it launches with exactly
        # the kwargs it launched with before this field existed.
        if self.bypass_csp:
            kwargs["bypass_csp"] = True
        return kwargs


def build_launch_plan(
    *,
    user_data_dir: Path,
    video_format: VideoFormat,
    headless: bool = True,
    executable_path: Path | None = None,
    extra_args: tuple[str, ...] = (),
    no_sandbox: bool = False,
    timeout_s: float = 60.0,
    locale: str = DEFAULT_LOCALE,
    timezone_id: str = DEFAULT_TIMEZONE,
    bypass_csp: bool = False,
) -> LaunchPlan:
    """Resolve every launch parameter for one session.

    The window is sized to the publish geometry with headroom for Meet's own chrome. It
    is not cosmetic in headless mode: Meet chooses its layout — and therefore how many
    video tiles it renders and how hard the compositor works — from the viewport, and a
    tiny viewport makes it collapse to a layout that suppresses the controls the join
    flow needs to click.

    Args:
        extra_args: Appended last so a deployment can override any flag above; Chromium
            takes the final occurrence of a repeated switch.
    """
    args: list[str] = [
        *MEDIA_ARGS,
        *STABILITY_ARGS,
        *AUTOMATION_ARGS,
        # One switch, built from DISABLED_FEATURES. Emitting these per group would mean two
        # ``--disable-features`` flags and Chromium honouring only the last — see that
        # constant for the bug this prevents.
        f"--disable-features={','.join(DISABLED_FEATURES)}",
        f"--lang={locale}",
    ]
    if no_sandbox:
        args.extend(SANDBOX_ARGS)

    width = max(video_format.width, 1280)
    height = max(video_format.height, 720)
    args.append(f"--window-size={width},{height}")
    args.extend(extra_args)

    return LaunchPlan(
        user_data_dir=user_data_dir,
        args=tuple(args),
        headless=headless,
        executable_path=executable_path,
        viewport=(width, height),
        locale=locale,
        timezone_id=timezone_id,
        timeout_ms=int(timeout_s * 1000),
        bypass_csp=bypass_csp,
    )
