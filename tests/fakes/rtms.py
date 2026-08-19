"""RTMS doubles.

``FakeRtmsTransport`` drives the **real** handshake code in ``RtmsService`` — signature,
``msg_type`` sequencing, keep-alive, audio translation — against scripted replies. So
M2's protocol logic is fully covered without a Zoom account or a live meeting.

``ReplayAudioSource`` is the second implementation that justifies the ``AudioSource``
port (doc 003 §0): it runs the whole downstream pipeline from a PCM buffer.
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import AsyncIterator
from typing import Any

from src.connectors.zoom.exceptions import RtmsConnectionError
from src.connectors.zoom.rtms.enums import RtmsMessageType
from src.domain.avatar import AVATAR_INPUT_FORMAT
from src.domain.context import FrameContext
from src.domain.health import ComponentHealth, ComponentState
from src.domain.media import AudioFormat, AudioFrame
from src.domain.meeting import ParticipantRef

MEDIA_URL = "wss://rtms-media-test.zoom.us/media"


class FakeRtmsTransport:
    """Scripted ``JsonWebSocket``.

    Replies to handshakes correctly by default; ``responses`` overrides a reply per
    ``msg_type`` so rejections and malformed frames can be tested.
    """

    def __init__(
        self,
        *,
        url: str,
        role: str,
        responses: dict[int, dict[str, Any]] | None = None,
        media_url: str = MEDIA_URL,
    ) -> None:
        self.url = url
        self.role = role
        self.sent: list[dict[str, Any]] = []
        self.closed = False
        self._media_url = media_url
        self._overrides = responses or {}
        self._inbound: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

    async def send_json(self, payload: dict[str, Any]) -> None:
        if self.closed:
            raise RtmsConnectionError("transport closed")
        self.sent.append(payload)

        msg_type = payload.get("msg_type")
        override = self._overrides.get(msg_type) if isinstance(msg_type, int) else None
        if override is not None:
            await self._inbound.put(override)
            return

        if msg_type == RtmsMessageType.SIGNALING_HAND_SHAKE_REQ:
            await self._inbound.put(
                {
                    "msg_type": int(RtmsMessageType.SIGNALING_HAND_SHAKE_RESP),
                    "status_code": 0,
                    "media_server": {"server_urls": {"all": self._media_url}},
                }
            )
        elif msg_type == RtmsMessageType.DATA_HAND_SHAKE_REQ:
            await self._inbound.put(
                {"msg_type": int(RtmsMessageType.DATA_HAND_SHAKE_RESP), "status_code": 0}
            )

    async def recv_json(self) -> dict[str, Any]:
        message = await self._inbound.get()
        if message is None:
            raise RtmsConnectionError("transport closed")
        return message

    async def messages(self) -> AsyncIterator[dict[str, Any]]:
        while True:
            message = await self._inbound.get()
            if message is None:
                return
            yield message

    async def close(self) -> None:
        self.closed = True
        await self._inbound.put(None)

    # -- server-side injection --------------------------------------------

    def push(self, message: dict[str, Any]) -> None:
        self._inbound.put_nowait(message)

    def push_audio(self, pcm: bytes, *, user_id: int = 42, user_name: str = "Human") -> None:
        self.push(
            {
                "msg_type": int(RtmsMessageType.MEDIA_DATA_AUDIO),
                "content": base64.b64encode(pcm).decode("ascii"),
                "user_id": user_id,
                "user_name": user_name,
                "timestamp": 1_700_000_000,
            }
        )

    def push_transcript(
        self, text: str, *, user_id: int = 42, user_name: str = "Human"
    ) -> None:
        """One line of Zoom's live transcription, in the envelope shape RTMS uses.

        The envelope rather than a bare string, because that is what the multi-stream
        subscription produces and it is the shape that carries the name — which is the
        entire reason this stream is subscribed to at all.
        """
        self.push(
            {
                "msg_type": int(RtmsMessageType.MEDIA_DATA_TRANSCRIPT),
                "content": {"data": text, "user_id": user_id, "user_name": user_name},
            }
        )

    def push_chat(self, text: str, *, user_id: int = 42, user_name: str = "Human") -> None:
        self.push(
            {
                "msg_type": int(RtmsMessageType.MEDIA_DATA_CHAT),
                "content": {"data": text, "user_id": user_id, "user_name": user_name},
            }
        )

    def push_keepalive(self, timestamp: int = 12345) -> None:
        self.push({"msg_type": int(RtmsMessageType.KEEP_ALIVE_REQ), "timestamp": timestamp})

    def push_event(self, event_type: int, **fields: Any) -> None:
        self.push(
            {"msg_type": int(RtmsMessageType.EVENT_UPDATE), "event_type": event_type, **fields}
        )

    def disconnect(self) -> None:
        """Simulate an abrupt close, to exercise reconnect."""
        self._inbound.put_nowait(None)

    def keepalive_responses(self) -> list[dict[str, Any]]:
        return [m for m in self.sent if m.get("msg_type") == RtmsMessageType.KEEP_ALIVE_RESP]


class FakeTransportFactory:
    """Hands out ``FakeRtmsTransport`` instances, labelling signaling vs media."""

    def __init__(
        self,
        *,
        responses: dict[int, dict[str, Any]] | None = None,
        media_url: str = MEDIA_URL,
    ) -> None:
        self.created: list[FakeRtmsTransport] = []
        self._responses = responses or {}
        self._media_url = media_url

    async def __call__(self, url: str) -> FakeRtmsTransport:
        role = "media" if "media" in url else "signaling"
        transport = FakeRtmsTransport(
            url=url, role=role, responses=self._responses, media_url=self._media_url
        )
        self.created.append(transport)
        return transport

    @property
    def signaling(self) -> FakeRtmsTransport:
        return next(t for t in self.created if t.role == "signaling")

    @property
    def media(self) -> FakeRtmsTransport:
        return next(t for t in self.created if t.role == "media")


class ReplayAudioSource:
    """``AudioSource`` that replays PCM from memory.

    Lets the entire downstream pipeline be exercised with no live meeting — the reason
    the ``AudioSource`` port earns its place.
    """

    def __init__(
        self,
        *,
        ctx: FrameContext,
        pcm: bytes,
        audio_format: AudioFormat | None = None,
        chunk_ms: int = 20,
        participant: ParticipantRef | None = None,
        interval_s: float = 0.0,
        repeat: int = 1,
    ) -> None:
        self._ctx = ctx
        self._format = audio_format or AVATAR_INPUT_FORMAT
        self._pcm = pcm
        self._chunk_bytes = self._format.bytes_for_duration(chunk_ms * 1_000)
        self._chunk_us = chunk_ms * 1_000
        self._participant = participant or ParticipantRef(user_id=42, display_name="Human")
        self._interval_s = interval_s
        self._repeat = repeat
        self._state = ComponentState.UNKNOWN
        self.started = False
        self.stopped = False

    async def start(self) -> None:
        self.started = True
        self._state = ComponentState.HEALTHY

    async def stop(self) -> None:
        self.stopped = True
        self._state = ComponentState.UNKNOWN

    async def frames(self) -> AsyncIterator[AudioFrame]:
        pts = 0
        for _ in range(self._repeat):
            for offset in range(0, len(self._pcm), self._chunk_bytes):
                chunk = self._pcm[offset : offset + self._chunk_bytes]
                if len(chunk) % self._format.bytes_per_frame:
                    return
                yield AudioFrame(
                    pcm=chunk,
                    pts_us=pts,
                    format=self._format,
                    ctx=self._ctx,
                    participant=self._participant,
                )
                pts += self._chunk_us
                if self._interval_s:
                    await asyncio.sleep(self._interval_s)

    def health(self) -> ComponentHealth:
        return ComponentHealth(name="replay_audio_source", state=self._state)
