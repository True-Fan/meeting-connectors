"""Teams sidecar doubles.

``FakeTeamsSidecar`` is an in-process sidecar that speaks the **real** wire protocol from
``connectors/teams/sidecar/protocol.py``. It drives the real ``TeamsSidecarLink`` — the
join handshake, ``READY`` validation, roster handling, audio translation, backpressure,
and reconnect — with no Windows host, no Azure tenant, and no admin consent.

That is the same de-risking strategy the Zoom connector used: ``FakeRtmsTransport`` proved
RTMS's protocol logic before a Zoom account existed, and the Zoom publisher was verified
against a stub before the C++ SDK build did. The Teams pipeline is testable today for the
same reason.

It substitutes for ``TeamsSidecarClient`` rather than for a socket, because the socket
layer is thin and the *protocol* is where the bugs live. ``feed_*`` methods push traffic
from the sidecar's side; ``sent`` records what the bridge wrote, already decoded.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from src.connectors.teams.exceptions import SidecarUnavailableError
from src.connectors.teams.sidecar.protocol import (
    MIXED_SOURCE,
    TeamsFlags,
    TeamsFrameDecoder,
    TeamsMessage,
    TeamsMessageType,
    encode_audio,
    encode_json,
)
from src.domain.avatar import AVATAR_INPUT_FORMAT
from src.domain.context import FrameContext
from src.domain.media import AudioFormat, AudioFrame

DEFAULT_READY = {
    "callId": "call-test-0001",
    "wireVersion": 1,
    "audioSampleRateHz": 16_000,
    "audioChannels": 1,
    "unmixedAudio": True,
    "videoWidth": 1280,
    "videoHeight": 720,
    "videoFps": 30,
    "sdkVersion": "1.2.0.0-fake",
}


class FakeTeamsSidecar:
    """An in-process stand-in for ``TeamsSidecarClient``."""

    def __init__(
        self,
        *,
        ready: dict | None = None,
        auto_ready: bool = True,
        fail_connect: Exception | None = None,
        write_buffer_size: int = 0,
    ) -> None:
        self._ready = dict(DEFAULT_READY) if ready is None else dict(ready)
        self._auto_ready = auto_ready
        self._fail_connect = fail_connect
        self._write_buffer_size = write_buffer_size

        self._inbound: asyncio.Queue[bytes | None] = asyncio.Queue()
        # Two decoders, one per direction. Sharing one would interleave the bridge's
        # outbound frames with the sidecar's inbound frames in a single buffer and
        # desync both.
        self._outbound_decoder = TeamsFrameDecoder()
        self._inbound_decoder = TeamsFrameDecoder()
        self._connected = False

        self.sent: list[TeamsMessage] = []
        self.connect_calls = 0
        self.close_calls = 0

    # -- TeamsSidecarClient surface ---------------------------------------

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def endpoint(self) -> str:
        return "fake-teams-sidecar:0"

    async def connect(self) -> None:
        self.connect_calls += 1
        if self._fail_connect is not None:
            raise self._fail_connect

        # Drain anything left from a previous connection, including the EOF sentinel that
        # ``close`` queues. Without this, the sentinel from the *old* link would end the
        # new link's read loop immediately and reconnect would spin.
        while not self._inbound.empty():
            self._inbound.get_nowait()

        self._connected = True
        self._outbound_decoder.reset()
        self._inbound_decoder.reset()

    async def close(self) -> None:
        self.close_calls += 1
        self._connected = False
        # Wake any pending read so the link's loops unwind rather than hanging.
        self._inbound.put_nowait(None)

    async def send_raw(self, payload: bytes, *, drain: bool = True) -> None:
        if not self._connected:
            raise SidecarUnavailableError("fake sidecar is not connected")
        for message in self._outbound_decoder.feed(payload):
            self.sent.append(message)
            if message.msg_type is TeamsMessageType.CONTROL_JOIN and self._auto_ready:
                self.feed_ready()

    async def send_json(self, msg_type: TeamsMessageType, body: dict, *, seq: int = 0) -> None:
        await self.send_raw(encode_json(msg_type, body, seq=seq))

    def write_buffer_size(self) -> int:
        return self._write_buffer_size

    async def messages(self) -> AsyncIterator[TeamsMessage]:
        while True:
            data = await self._inbound.get()
            if data is None:
                self._connected = False
                return
            for message in self._inbound_decoder.feed(data):
                yield message

    async def await_message(
        self, expected: TeamsMessageType, *, timeout_s: float
    ) -> TeamsMessage:
        # Reuses the real client's semantics closely enough for the link's join path:
        # skip anything that is not what we are waiting for, and surface a fatal error.
        from src.connectors.teams.exceptions import SidecarFatalError
        from src.connectors.teams.graph.models import SidecarError

        async def _wait() -> TeamsMessage:
            async for message in self.messages():
                if message.msg_type is expected:
                    return message
                if message.msg_type is TeamsMessageType.ERROR:
                    error = SidecarError.model_validate(message.json())
                    if error.fatal:
                        raise SidecarFatalError(error.code, error.message)
            raise SidecarUnavailableError(f"fake sidecar closed before {expected.name}")

        try:
            return await asyncio.wait_for(_wait(), timeout=timeout_s)
        except TimeoutError as exc:
            raise SidecarUnavailableError(
                f"fake sidecar did not send {expected.name}"
            ) from exc

    # -- driving the sidecar's side ---------------------------------------

    def feed_ready(self, **overrides: object) -> None:
        """Send ``READY``, as the sidecar does once the Graph call is established."""
        payload = dict(self._ready)
        payload.update(overrides)
        self._push(encode_json(TeamsMessageType.READY, payload))

    def feed_audio(
        self,
        pcm: bytes,
        *,
        ctx: FrameContext,
        pts_us: int = 0,
        source_msi: int = MIXED_SOURCE,
        unmixed: bool = False,
        sample_rate_hz: int | None = None,
    ) -> None:
        """Send one participant audio frame."""
        audio_format = (
            AVATAR_INPUT_FORMAT
            if sample_rate_hz is None
            else AudioFormat(
                sample_rate_hz=sample_rate_hz,
                channels=1,
                sample_format=AVATAR_INPUT_FORMAT.sample_format,
            )
        )
        frame = AudioFrame(pcm=pcm, pts_us=pts_us, format=audio_format, ctx=ctx)
        flags = TeamsFlags.UNMIXED if unmixed else TeamsFlags.NONE
        self._push(encode_audio(frame, source_msi=source_msi, flags=flags))

    def feed_roster(self, participants: list[dict]) -> None:
        """Send a roster update."""
        self._push(
            encode_json(TeamsMessageType.ROSTER, {"participants": participants})
        )

    def feed_call_state(self, state: int, reason: str | None = None) -> None:
        self._push(
            encode_json(
                TeamsMessageType.CALL_STATE,
                {"state": state, "reason": reason},
            )
        )

    def feed_error(self, code: str, message: str, *, fatal: bool = False) -> None:
        self._push(
            encode_json(
                TeamsMessageType.ERROR,
                {"code": code, "message": message, "fatal": fatal},
            )
        )

    def feed_heartbeat(self, sent_at_us: int = 123) -> None:
        self._push(encode_json(TeamsMessageType.HEARTBEAT, {"sent_at_us": sent_at_us}))

    def feed_raw(self, payload: bytes) -> None:
        """Push arbitrary bytes, for framing and desync tests."""
        self._push(payload)

    def feed_eof(self) -> None:
        """Close the link from the sidecar's side, as a crash or a call end would."""
        self._inbound.put_nowait(None)

    def set_write_buffer_size(self, size: int) -> None:
        """Simulate socket backpressure, so the video drop policy can be tested."""
        self._write_buffer_size = size

    # -- assertions helpers -----------------------------------------------

    def sent_of(self, msg_type: TeamsMessageType) -> list[TeamsMessage]:
        return [m for m in self.sent if m.msg_type is msg_type]

    def join_payload(self) -> dict:
        joins = self.sent_of(TeamsMessageType.CONTROL_JOIN)
        if not joins:
            raise AssertionError("no CONTROL_JOIN was sent")
        return joins[-1].json()

    def _push(self, payload: bytes) -> None:
        self._inbound.put_nowait(payload)
