"""PageChannel — the accepted page connection, framed in both directions.

Transport only. It moves encoded messages and knows nothing about Meet, Playwright, the
avatar, or the media pipeline — all of which live above it. That split is what lets the
whole page protocol be exercised against an in-process fake page, with no Chromium.

**Backpressure policy matches the other two connectors: drop video, keep audio.** A lost
video frame costs one frame of smoothness; a lost audio frame is an audible gap. Drops
are counted, never silent.

The mechanism differs from Teams', though, and deliberately. ``TeamsSidecarClient``
inspects the asyncio transport's write-buffer depth, which it can do because it owns a
raw ``StreamWriter``. Here the socket belongs to the ``websockets`` library, and its
buffer depth is an implementation detail that has moved between releases. So the guard is
a property this layer owns outright: **one video send may be in flight at a time.** If a
frame arrives while the previous one is still going out, the link is already the
bottleneck and the new frame is dropped. That is a stricter bound than a byte threshold,
it needs no library internals, and for a 25 fps stream over loopback it engages only when
something is genuinely wrong.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import suppress
from typing import Any

from websockets.asyncio.server import ServerConnection
from websockets.exceptions import WebSocketException

from src.connectors.google_meet.exceptions import (
    BridgeProtocolError,
    BridgeUnavailableError,
)
from src.connectors.google_meet.websocket.protocol import (
    MeetMessage,
    MeetMessageType,
    decode,
    encode_json,
)
from src.infrastructure.logging import get_logger

logger = get_logger(__name__)


class PageChannel:
    """A framed, bidirectional channel to one Chromium page."""

    __slots__ = ("_closed", "_connection", "_dropped_video", "_video_inflight")

    def __init__(self, connection: ServerConnection) -> None:
        self._connection = connection
        self._closed = False
        self._video_inflight = False
        self._dropped_video = 0

    @property
    def is_connected(self) -> bool:
        return not self._closed

    @property
    def dropped_video(self) -> int:
        """Video frames discarded because a send was already in flight."""
        return self._dropped_video

    @property
    def remote(self) -> str:
        """The peer's address, for logging."""
        try:
            return str(self._connection.remote_address)
        except (AttributeError, OSError):  # pragma: no cover - platform dependent
            return "unknown"

    # -- sending -----------------------------------------------------------

    async def send_raw(self, payload: bytes) -> None:
        """Send pre-encoded bytes.

        Raises:
            BridgeUnavailableError: the channel is closed or the write failed.
        """
        if self._closed:
            raise BridgeUnavailableError("page bridge channel is closed")
        try:
            await self._connection.send(payload)
        except (WebSocketException, OSError, RuntimeError) as exc:
            self._closed = True
            raise BridgeUnavailableError(f"page bridge write failed: {exc}") from exc

    async def send_json(
        self, msg_type: MeetMessageType, body: dict[str, Any], *, seq: int = 0
    ) -> None:
        """Send a control message."""
        await self.send_raw(encode_json(msg_type, body, seq=seq))

    async def try_send_video(self, payload: bytes) -> bool:
        """Send a video frame, dropping it if one is already in flight.

        Returns:
            True when the frame was written, False when the policy dropped it.

        Raises:
            BridgeUnavailableError: the channel is closed or the write failed.
        """
        if self._video_inflight:
            self._dropped_video += 1
            return False
        self._video_inflight = True
        try:
            await self.send_raw(payload)
        finally:
            # Cleared even on failure: leaving it set would silently drop every
            # subsequent frame for the life of a channel that is about to be replaced
            # anyway, turning one transport error into permanently frozen video.
            self._video_inflight = False
        return True

    # -- receiving ---------------------------------------------------------

    async def messages(self) -> AsyncIterator[MeetMessage]:
        """Yield decoded messages until the page disconnects.

        Text frames are rejected rather than ignored. Every message this protocol
        defines is binary, so a text frame means the page is running something other
        than ``js/bridge.js`` — worth failing on, not skipping past.

        Raises:
            BridgeUnavailableError: the connection dropped.
            BridgeProtocolError: the page violated the wire contract.
        """
        try:
            async for raw in self._connection:
                if isinstance(raw, str):
                    raise BridgeProtocolError(
                        "page sent a text frame; the bridge protocol is binary only"
                    )
                yield decode(raw)
        except (WebSocketException, OSError) as exc:
            self._closed = True
            raise BridgeUnavailableError(f"page bridge read failed: {exc}") from exc
        self._closed = True

    async def await_message(
        self, expected: MeetMessageType, *, timeout_s: float
    ) -> MeetMessage:
        """Wait for one specific message type, handling what arrives first.

        ``ERROR`` short-circuits: a page that cannot acquire a synthetic device must
        fail the join immediately rather than waiting out the full timeout and
        reporting it as one. Heartbeats are answered in place so the page's own
        liveness check does not trip while we are still waiting for a join to land.

        Raises:
            BridgeUnavailableError: timeout, disconnect, or transport failure.
            BridgeProtocolError: the page violated the wire contract.
        """

        async def _wait() -> MeetMessage:
            async for message in self.messages():
                if message.msg_type is expected:
                    return message
                if message.msg_type is MeetMessageType.ERROR:
                    body = message.json()
                    if bool(body.get("fatal")):
                        raise BridgeProtocolError(
                            f"page reported a fatal error [{body.get('code', 'UNKNOWN')}]: "
                            f"{body.get('message', '')}"
                        )
                    logger.warning(
                        "meet_bridge.page_error",
                        code=body.get("code"),
                        message=body.get("message"),
                        fatal=False,
                    )
                elif message.msg_type is MeetMessageType.HEARTBEAT:
                    await self._echo_heartbeat(message)
            raise BridgeUnavailableError(
                f"page disconnected before sending {expected.name}"
            )

        try:
            return await asyncio.wait_for(_wait(), timeout=timeout_s)
        except TimeoutError as exc:
            raise BridgeUnavailableError(
                f"page did not send {expected.name} within {timeout_s}s"
            ) from exc

    async def _echo_heartbeat(self, message: MeetMessage) -> None:
        """Echo the page's timestamp so it can measure round-trip latency itself."""
        body = message.json()
        await self.send_json(
            MeetMessageType.HEARTBEAT, {"sent_at_us": body.get("sent_at_us", 0)}
        )

    # -- teardown ----------------------------------------------------------

    async def close(self) -> None:
        """Close the channel. Idempotent.

        Failures are suppressed: an already-broken socket is the normal case here, because
        we are usually closing *because* something failed. There is nothing left to salvage
        and nothing useful to report.
        """
        self._closed = True
        with suppress(WebSocketException, OSError, RuntimeError):
            await self._connection.close()
