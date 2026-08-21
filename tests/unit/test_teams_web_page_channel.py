"""The page channel — the codec, and the socket that carries it.

Driven against the **real** ``PageAudioServer`` over loopback with a real WebSocket client, so
these tests exercise the actual framing, the actual token check, and the actual routing. That is
where the value is: the codec is the one place a Python/JavaScript mismatch is invisible until a
live meeting, and ``test_teams_web_js_assets.py`` guards the JavaScript half of the same
contract.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus

from src.connectors.teams_web.page.protocol import (
    HEADER_SIZE,
    KIND_AUDIO_CAPTURE,
    KIND_AUDIO_PCM,
    MAGIC,
    MAX_EVENT_BYTES,
    VERSION,
    decode_audio,
    decode_event,
    encode_audio,
)
from src.connectors.teams_web.page.server import PageAudioServer

PCM = b"\x01\x02" * 320


def _capture_frame(pcm: bytes, *, pts_us: int = 0, kind: int = KIND_AUDIO_CAPTURE) -> bytes:
    """One page→bridge audio frame, assembled the way the JavaScript assembles it."""
    import struct

    return struct.pack("!4sBBHQI", MAGIC, VERSION, kind, 0, pts_us, len(pcm)) + pcm


class TestCodec:
    def test_audio_round_trips(self) -> None:
        framed = _capture_frame(PCM, pts_us=12_345)
        assert decode_audio(framed) == PCM

    def test_the_outbound_header_carries_the_right_kind(self) -> None:
        framed = encode_audio(PCM, pts_us=7)
        assert framed[:4] == MAGIC
        assert framed[4] == VERSION
        assert framed[5] == KIND_AUDIO_PCM
        assert len(framed) == HEADER_SIZE + len(PCM)

    def test_the_two_directions_are_told_apart_by_kind(self) -> None:
        """A frame logged or captured in isolation should say which way it was travelling."""
        assert decode_audio(encode_audio(PCM, pts_us=0)) is None
        assert decode_audio(_capture_frame(PCM)) == PCM

    @pytest.mark.parametrize(
        "framed",
        [
            b"",
            b"short",
            _capture_frame(PCM).replace(MAGIC, b"ZWB1", 1),
            _capture_frame(PCM, kind=99),
            _capture_frame(PCM)[:-10],
            _capture_frame(b""),
        ],
        ids=["empty", "truncated", "wrong-magic", "wrong-kind", "short-payload", "no-payload"],
    )
    def test_an_unusable_frame_is_none_rather_than_an_exception(self, framed: bytes) -> None:
        """Never raises, and that is the contract: this is called from the read loop against
        bytes produced by a script running inside a page this service does not control. A
        malformed frame is a fact about the page."""
        assert decode_audio(framed) is None

    def test_a_zoom_web_frame_is_not_accepted(self) -> None:
        """Independent codecs. The layouts agree and the magic does not, which is what makes a
        cross-wired page a loud failure rather than a silent one."""
        from src.connectors.zoom_web.page.protocol import encode_audio as zoom_encode

        assert decode_audio(zoom_encode(PCM, pts_us=0)) is None

    def test_events_round_trip(self) -> None:
        assert decode_event(json.dumps({"type": "roster", "names": ["Dev"]})) == {
            "type": "roster",
            "names": ["Dev"],
        }

    @pytest.mark.parametrize(
        "raw",
        [
            "",
            "not json",
            "[1, 2, 3]",
            '"a string"',
            "{}",
            '{"type": ""}',
            '{"type": 7}',
        ],
        ids=["empty", "not-json", "array", "string", "no-type", "empty-type", "non-string-type"],
    )
    def test_an_unusable_event_is_none(self, raw: str) -> None:
        assert decode_event(raw) is None

    def test_an_oversized_event_is_refused_without_allocating(self) -> None:
        """A guard against a page that has gone wrong: the largest legitimate message is a
        truncated chat line, so anything approaching the ceiling is a malfunction."""
        payload = json.dumps({"type": "chat", "text": "x" * (MAX_EVENT_BYTES + 1)})
        assert decode_event(payload) is None


class TestServer:
    @pytest.mark.asyncio
    async def test_a_page_without_the_token_is_rejected_before_the_handshake(self) -> None:
        """The socket is reachable by anything on the host, so an unauthenticated one would let
        a co-resident process speak as the avatar."""
        server = PageAudioServer()
        await server.start()
        try:
            endpoint = server.endpoint.split("?")[0]
            with pytest.raises(InvalidStatus):
                await connect(endpoint)
        finally:
            await server.stop()

    @pytest.mark.asyncio
    async def test_the_port_is_ephemeral(self) -> None:
        """A fixed port would make the socket trivially discoverable."""
        server = PageAudioServer()
        await server.start()
        try:
            assert "127.0.0.1:0/" not in server.endpoint
        finally:
            await server.stop()

    @pytest.mark.asyncio
    async def test_audio_is_routed_to_the_audio_handler_and_events_to_the_event_handler(
        self,
    ) -> None:
        """**Binary is audio and text is an event.** Routing on the transport's frame type is
        what keeps a JSON decode off the audio path fifty times a second."""
        server = PageAudioServer()
        received: list[bytes] = []
        events: list[dict[str, object]] = []
        server.set_audio_handler(received.append)
        server.set_event_handler(events.append)
        await server.start()
        try:
            async with connect(server.endpoint) as page:
                await page.send(_capture_frame(PCM))
                await page.send(json.dumps({"type": "speaker", "name": "Priya"}))
                await _until(lambda: bool(received) and bool(events))
        finally:
            await server.stop()

        assert received == [PCM]
        assert events == [{"type": "speaker", "name": "Priya"}]
        assert server.audio_received == 1
        assert server.events_received == 1

    @pytest.mark.asyncio
    async def test_the_avatar_s_audio_is_broadcast_to_every_attached_frame(self) -> None:
        """``add_init_script`` runs in every frame Chromium creates, and nothing here can tell
        which frame's track Teams actually selected — so all of them are fed and the others
        discard it. Keeping a single "latest" client loses the race outright."""
        server = PageAudioServer()
        await server.start()
        try:
            async with connect(server.endpoint) as one, connect(server.endpoint) as two:
                await _until(lambda: server.attached_pages == 2)
                await server.send(encode_audio(PCM, pts_us=1))
                first = await asyncio.wait_for(one.recv(), timeout=1)
                second = await asyncio.wait_for(two.recv(), timeout=1)
        finally:
            await server.stop()

        assert isinstance(first, bytes) and isinstance(second, bytes)
        assert first[5] == KIND_AUDIO_PCM
        assert first == second

    @pytest.mark.asyncio
    async def test_a_handler_that_raises_does_not_drop_the_socket(self) -> None:
        """This is the loop that carries the avatar's voice into the page: a bookkeeping
        listener may not be able to close it."""
        server = PageAudioServer()
        seen: list[str] = []

        def explode(_event: dict[str, object]) -> None:
            seen.append("called")
            raise RuntimeError("observer bug")

        server.set_event_handler(explode)
        await server.start()
        try:
            async with connect(server.endpoint) as page:
                await page.send(json.dumps({"type": "roster", "names": ["Dev"]}))
                await _until(lambda: len(seen) == 1)
                # Still usable afterwards.
                await server.send(encode_audio(PCM, pts_us=1))
                assert isinstance(await asyncio.wait_for(page.recv(), timeout=1), bytes)
        finally:
            await server.stop()

    @pytest.mark.asyncio
    async def test_unusable_frames_are_counted_rather_than_forwarded(self) -> None:
        """``audio_dropped`` non-zero means the injected script and this build have drifted
        apart, which is a different fault from a page that found no audio."""
        server = PageAudioServer()
        server.set_audio_handler(lambda _pcm: None)
        await server.start()
        try:
            async with connect(server.endpoint) as page:
                await page.send(b"not a frame")
                await page.send("not json")
                await _until(
                    lambda: server.audio_dropped == 1 and server.events_dropped == 1
                )
        finally:
            await server.stop()

    @pytest.mark.asyncio
    async def test_waiting_for_a_page_that_never_attaches_is_a_fact_not_an_error(self) -> None:
        """The caller decides what an unattached page means; here it is only a fact — a late
        page still works, and health reports the gap."""
        server = PageAudioServer()
        await server.start()
        try:
            assert await server.wait_connected(timeout_s=0.05) is False
        finally:
            await server.stop()

    @pytest.mark.asyncio
    async def test_sending_with_no_page_attached_never_raises(self) -> None:
        """A send that fails is a page that went away, which the session notices through health
        rather than through an exception on the pacer's path."""
        server = PageAudioServer()
        await server.start()
        try:
            await server.send(encode_audio(PCM, pts_us=0))
        finally:
            await server.stop()

    @pytest.mark.asyncio
    async def test_start_and_stop_are_idempotent(self) -> None:
        server = PageAudioServer()
        await server.start()
        await server.start()
        await server.stop()
        await server.stop()


async def _until(predicate: object, *, timeout_s: float = 1.0) -> None:
    """Poll until ``predicate`` holds, so a test never sleeps for a fixed interval."""
    deadline = asyncio.get_running_loop().time() + timeout_s
    while not predicate():  # type: ignore[operator]
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("condition never became true")
        await asyncio.sleep(0.005)
