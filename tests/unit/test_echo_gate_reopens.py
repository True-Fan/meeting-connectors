"""Regression: the speaking gate must reopen, or the avatar goes permanently deaf.

**A real bug in shared code, found against a live avatar agent.** The gate was armed by
``Pacer._publish_audio`` whenever it published a *non-idle* frame — which silently assumed
the avatar streams only while it is talking. It does not. ``domain.avatar`` states the
contract plainly: the output is **a continuously streamed fragmented MP4**. So the decoder
produced an audio frame every 20 ms for the whole session, ``is_idle`` was never ``True``,
``note_publishing`` fired on every tick, and ``EchoGuard.is_gate_open`` never returned
``False`` again.

The consequence is total and silent. ``MediaRouter._forward`` consults the guard for every
inbound frame, so after the avatar's first fragment arrived **no meeting audio ever reached
the avatar again**. Every layer reported health: the browser was in the meeting, the pacer was
publishing, the session was ``ACTIVE``, and the agent sat in its LiveKit room hearing two
seconds of a conversation and then nothing. In the live run the router forwarded 124 frames —
about 2.5 seconds — and froze there while the meeting ran on for two minutes.

The fix is that silence cannot echo, so publishing it is not grounds to suppress anything.
Energy, not frame arrival, arms the gate.

Covers all three connectors, because both the bug and the fix are in shared code — and Zoom
and Teams were equally affected the moment their avatar streamed continuously.
"""

from __future__ import annotations

import asyncio
import math
import struct

import pytest

from src.domain.context import FrameContext
from src.domain.health import ComponentHealth
from src.domain.media import AudioFormat, AudioFrame, VideoFormat
from src.services.media.clock import MediaClock
from src.services.media.echo_guard import EchoGuard
from src.services.media.idle_source import IdleFrameSource
from src.services.media.pacer import SILENCE_FLOOR, Pacer, _is_audible

PUBLISH_AUDIO = AudioFormat(sample_rate_hz=48_000, channels=1)
VIDEO = VideoFormat(width=320, height=180, fps=10)
SAMPLES_PER_FRAME = 960  # 20 ms at 48 kHz

SILENT = bytes(SAMPLES_PER_FRAME * 2)
LOUD = struct.pack(f"<{SAMPLES_PER_FRAME}h", *([8_000] * SAMPLES_PER_FRAME))
"""Decoded silence and decoded speech. Both are non-idle frames — the distinction the gate
used to be blind to."""


class RecordingSink:
    """A ``MediaSink`` that accepts everything and remembers nothing but counts."""

    def __init__(self) -> None:
        self.audio = 0
        self.video = 0

    async def publish_audio(self, frame: AudioFrame) -> None:
        self.audio += 1

    async def publish_video(self, frame: object) -> None:
        self.video += 1

    def health(self) -> ComponentHealth:
        return ComponentHealth.healthy("recording_sink")


def _pacer(guard: EchoGuard, ctx: FrameContext, clock: MediaClock) -> Pacer:
    return Pacer(
        ctx=ctx,
        clock=clock,
        sink=RecordingSink(),
        idle=IdleFrameSource(ctx=ctx, video_format=VIDEO, audio_format=PUBLISH_AUDIO),
        video_format=VIDEO,
        audio_format=PUBLISH_AUDIO,
        echo_guard=guard,
    )


def _frame(pcm: bytes, ctx: FrameContext) -> AudioFrame:
    return AudioFrame(pcm=pcm, pts_us=0, format=PUBLISH_AUDIO, ctx=ctx)


def test_is_audible_separates_silence_from_speech() -> None:
    assert not _is_audible(SILENT)
    assert _is_audible(LOUD)
    assert not _is_audible(b"")
    assert not _is_audible(b"\x00")  # odd length, no whole sample

    # The floor itself, checked without striding so the boundary is unambiguous. Lossy-decode
    # noise must not read as speech; anything at the floor must.
    assert not _is_audible(struct.pack("<4h", 12, -30, 5, -8), stride=1)
    assert _is_audible(struct.pack("<4h", 0, 0, 0, SILENCE_FLOOR), stride=1)
    assert _is_audible(struct.pack("<4h", 0, 0, 0, -SILENCE_FLOOR), stride=1)
    assert not _is_audible(struct.pack("<4h", 0, 0, 0, SILENCE_FLOOR - 1), stride=1)


def test_striding_still_detects_a_real_frame_of_speech() -> None:
    """The stride is an optimisation, so it must not miss speech in a real-sized frame.

    A 20 ms frame carries 960 samples and every eighth is inspected; speech is periodic over
    far longer than 8 samples, so a sine at a speech-like pitch must register.
    """
    tone = struct.pack(
        f"<{SAMPLES_PER_FRAME}h",
        *(
            int(6_000 * math.sin(2 * math.pi * 200 * n / 48_000))
            for n in range(SAMPLES_PER_FRAME)
        ),
    )
    assert _is_audible(tone)


@pytest.mark.asyncio
async def test_publishing_decoded_silence_leaves_the_gate_open(
    frame_ctx: FrameContext,
) -> None:
    """The core regression: a continuously streaming avatar must not gag the meeting.

    These are ``is_idle=False`` frames — genuinely decoded avatar output — that merely happen
    to be silent. Before the fix each one re-armed the gate.
    """
    guard = EchoGuard(per_participant_audio=False, hangover_ms=200)
    clock = MediaClock()
    pacer = _pacer(guard, frame_ctx, clock)

    for _ in range(50):  # a full second of streamed silence
        await pacer._publish_audio(_frame(SILENT, frame_ctx), is_idle=False)

    assert not guard.is_gate_open(clock.now_us()), (
        "publishing decoded silence armed the echo gate; inbound audio is now suppressed "
        "for the rest of the session"
    )
    assert not pacer.is_speaking


@pytest.mark.asyncio
async def test_publishing_decoded_speech_still_arms_the_gate(frame_ctx: FrameContext) -> None:
    """The protection itself must survive the fix — this is what stops feedback."""
    guard = EchoGuard(per_participant_audio=False, hangover_ms=200)
    clock = MediaClock()
    pacer = _pacer(guard, frame_ctx, clock)

    await pacer._publish_audio(_frame(LOUD, frame_ctx), is_idle=False)

    assert guard.is_gate_open(clock.now_us()), "audible avatar audio must suppress the echo"
    assert pacer.is_speaking


@pytest.mark.asyncio
async def test_gate_reopens_after_the_hangover_once_speech_stops(
    frame_ctx: FrameContext,
) -> None:
    """Speech arms it, the hangover holds it, then continued silence lets it lapse."""
    guard = EchoGuard(per_participant_audio=False, hangover_ms=50)
    clock = MediaClock()
    pacer = _pacer(guard, frame_ctx, clock)

    await pacer._publish_audio(_frame(LOUD, frame_ctx), is_idle=False)
    assert guard.is_gate_open(clock.now_us())

    # Keep streaming — silently, as a continuous avatar does between utterances.
    await asyncio.sleep(0.12)
    for _ in range(3):
        await pacer._publish_audio(_frame(SILENT, frame_ctx), is_idle=False)

    assert not guard.is_gate_open(clock.now_us()), (
        "the gate never reopened, so the avatar cannot hear anyone again"
    )
