"""In-call controls: microphone, camera, and leaving.

**Why the avatar has to touch these at all.** Meet decides independently of us whether the
tracks it was handed are *published*. A browser that joins with its microphone muted holds
a perfectly good synthetic audio track that nobody hears, and the session reports healthy
at every layer: the bridge is up, frames are flowing, the pacer is publishing. The only
symptom is silence in the meeting. So the mute state is not a nicety — it is part of making
egress real, and it is asserted after joining rather than assumed.

**Why the selectors read as inverted.** Meet labels these buttons with the *action* rather
than the state: the button says "Turn off microphone" precisely when the microphone is on.
That makes the selector match itself the state read — ``mute_toggle`` matching means we are
currently unmuted — so no separate query is needed and there is no window in which a
cached state and the real one disagree.

**Why leaving matters.** Closing the browser without clicking Leave drops the peer
connection and leaves the avatar as a frozen tile in the roster until Meet times it out,
which is minutes. Clicking Leave removes it immediately. The same reasoning is why every
connector here drains its sessions on shutdown.
"""

from __future__ import annotations

from src.connectors.google_meet.automation.driver import BrowserDriver
from src.connectors.google_meet.automation.selectors import MeetSelectors
from src.infrastructure.logging import get_logger

logger = get_logger(__name__)


class MeetControls:
    """Drives the in-call buttons for one browser."""

    __slots__ = ("_driver", "_selectors")

    def __init__(self, *, driver: BrowserDriver, selectors: MeetSelectors) -> None:
        self._driver = driver
        self._selectors = selectors

    # -- microphone --------------------------------------------------------

    async def is_muted(self) -> bool | None:
        """Whether the microphone is muted, or ``None`` when neither button is visible.

        ``None`` is a real answer and not a failure: before the call UI mounts, and after
        it tears down, there is no microphone button to read. Returning ``False`` there
        would claim "unmuted" for a browser that is not in a call at all, and the caller
        would then decide no action was needed.
        """
        if await self._visible(self._selectors.unmute_toggle):
            return True
        if await self._visible(self._selectors.mute_toggle):
            return False
        return None

    async def unmute(self) -> bool:
        """Ensure the microphone is publishing. Idempotent.

        Returns True when the microphone is known to be live afterwards.
        """
        muted = await self.is_muted()
        if muted is False:
            return True
        if muted is None:
            logger.warning(
                "meet_controls.no_microphone_button",
                note="cannot confirm the avatar is unmuted; the call UI may not be "
                "mounted yet, or the control was renamed (automation/selectors.py)",
            )
            return False

        clicked = await self._driver.click_first(self._selectors.unmute_toggle)
        if clicked is None:  # pragma: no cover - visible then unclickable is a race
            return False
        logger.info("meet_controls.unmuted", selector=clicked)
        return await self.is_muted() is False

    async def mute(self) -> bool:
        """Stop publishing audio. Idempotent."""
        if await self.is_muted() is True:
            return True
        clicked = await self._driver.click_first(self._selectors.mute_toggle)
        if clicked is None:
            return False
        logger.info("meet_controls.muted", selector=clicked)
        return True

    # -- camera ------------------------------------------------------------

    async def is_camera_on(self) -> bool | None:
        """Whether the camera is publishing, or ``None`` when unreadable."""
        if await self._visible(self._selectors.camera_off_toggle):
            return True
        if await self._visible(self._selectors.camera_on_toggle):
            return False
        return None

    async def camera_on(self) -> bool:
        """Ensure the synthetic camera is publishing. Idempotent."""
        state = await self.is_camera_on()
        if state is True:
            return True
        if state is None:
            logger.warning(
                "meet_controls.no_camera_button",
                note="cannot confirm the avatar's camera is on; the avatar would appear "
                "as an initial rather than as a person",
            )
            return False

        clicked = await self._driver.click_first(self._selectors.camera_on_toggle)
        if clicked is None:
            return False
        logger.info("meet_controls.camera_on", selector=clicked)
        return await self.is_camera_on() is True

    async def camera_off(self) -> bool:
        """Stop publishing video. Idempotent."""
        if await self.is_camera_on() is False:
            return True
        clicked = await self._driver.click_first(self._selectors.camera_off_toggle)
        if clicked is None:
            return False
        logger.info("meet_controls.camera_off", selector=clicked)
        return True

    # -- presence ----------------------------------------------------------

    async def publish_both(self) -> tuple[bool, bool]:
        """Turn on the microphone and the camera, reporting each outcome.

        Called once the media pipeline is running. Both are attempted even if the first
        fails: a session with working audio and no video is degraded but useful, and
        giving up on the camera because the microphone button moved would throw that away.
        """
        audio = await self.unmute()
        video = await self.camera_on()
        if not (audio and video):
            logger.warning("meet_controls.partial_publish", audio=audio, video=video)
        return audio, video

    async def leave(self) -> bool:
        """Click Leave. Returns whether the button was found.

        Failure is not raised: this runs during teardown, where the browser is about to be
        closed regardless, and an exception here would propagate out of a session stop.
        """
        clicked = await self._driver.click_first(self._selectors.leave)
        if clicked is None:
            logger.warning(
                "meet_controls.leave_button_missing",
                note="closing the browser without leaving; the avatar may linger in the "
                "roster as a frozen tile until Meet times it out",
            )
            return False
        logger.info("meet_controls.left", selector=clicked)
        return True

    async def _visible(self, selectors: tuple[str, ...]) -> bool:
        return await self._driver.wait_for_any(selectors, timeout_s=0.0) is not None
