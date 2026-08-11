"""Regression: the decoder's audio must actually leave ffmpeg, and stopping must return.

**Two real bugs in shared code, both found while wiring a live avatar agent to Google Meet,
both invisible to every existing test** — because nothing had ever run ``FfmpegDecoder``
inside a process whose file-descriptor table looked like a running session's.

**1. Audio went to a descriptor that did not exist.** The command hardcoded ``pipe:3``, while
the write end came from ``os.pipe()`` and was inherited via ``pass_fds``. ``pass_fds``
inherits descriptors *at their existing numbers*; it does not renumber them to 3. In a
freshly-started interpreter ``os.pipe()`` happens to return 3, which is why this ever
appeared to work. In a real session — event loop kqueue, HTTP listener, avatar socket,
browser CDP connection already open — it returns something like 11, so ``pipe:3`` named a
descriptor that was closed in the child. ffmpeg died with ``Error submitting a packet to the
muxer: Bad file descriptor`` before emitting a single audio sample.

The failure is worse than it sounds, because it is asymmetric: video goes to stdout and was
unaffected. So the avatar's **face appeared in the meeting while its voice never did** — the
exact symptom of an avatar that joins, looks alive, and never speaks.

**2. ``stop()`` never returned.** Tearing down cancels the stderr drain and the frame
readers, so nothing consumes stdout or stderr — the condition ``asyncio.subprocess`` documents
as a ``wait()`` deadlock. Observed with both pipes full: ``await process.wait()`` never
resolved even after ``SIGKILL`` had been sent and the kernel had reaped the process, hanging
``stop()`` — and with it ``GoogleMeetSession.stop()`` — indefinitely.

Both tests deliberately open spare descriptors first. Without that, test 1 passes against the
bug, which is precisely how it survived this long.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import subprocess
import time
from collections.abc import Iterator

import pytest

from src.domain.context import FrameContext
from src.domain.media import AudioFormat, MediaChunk, VideoFormat
from src.services.media.clock import MediaClock
from src.services.media.decoders.ffmpeg import FfmpegDecoder

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None, reason="ffmpeg is required to exercise the real decoder"
)

VIDEO_FORMAT = VideoFormat(width=320, height=180, fps=10)
AUDIO_FORMAT = AudioFormat(sample_rate_hz=48_000, channels=1)


@pytest.fixture
def crowded_fd_table() -> Iterator[None]:
    """Occupy the low descriptor numbers, the way a live session does.

    3 is the number that matters: with it free, ``os.pipe()`` hands it straight back and a
    hardcoded ``pipe:3`` is accidentally correct.
    """
    pipes = [os.pipe() for _ in range(8)]
    try:
        yield
    finally:
        for read_fd, write_fd in pipes:
            for fd in (read_fd, write_fd):
                with contextlib.suppress(OSError):
                    os.close(fd)


@pytest.fixture
def fragmented_mp4(tmp_path_factory: pytest.TempPathFactory) -> bytes:
    """A short real fMP4 with both a video and an audio track.

    Both tracks are required: ``_build_command`` maps video to one output and audio to
    another, and an audio-only input leaves the video output with no streams — ffmpeg then
    refuses to start at all, so the audio never arrives either.
    """
    path = tmp_path_factory.mktemp("media") / "av.mp4"
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc=size=320x180:rate=10:duration=1",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=1:sample_rate=48000",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-g", "10",
            "-c:a", "aac",
            "-movflags", "+frag_keyframe+empty_moov+default_base_moof",
            "-frag_duration", "100000",
            "-f", "mp4", str(path),
        ],
        check=True,
        capture_output=True,
    )
    return path.read_bytes()


def _chunk(data: bytes, ctx: FrameContext) -> MediaChunk:
    return MediaChunk(data=data, seq=0, received_at_us=0, ctx=ctx, is_init_segment=True)


@pytest.mark.asyncio
async def test_audio_reaches_the_reader_when_fd_three_is_taken(
    frame_ctx: FrameContext, fragmented_mp4: bytes, crowded_fd_table: None
) -> None:
    """PCM must come out of the audio pipe whatever number that pipe was given.

    This is the assertion the hardcoded ``pipe:3`` could not satisfy, and the one that maps
    directly onto "the avatar is visible but silent".
    """
    decoder = FfmpegDecoder(
        ctx=frame_ctx,
        clock=MediaClock(),
        video_format=VIDEO_FORMAT,
        audio_format=AUDIO_FORMAT,
    )
    await decoder.start()
    try:
        await decoder.feed(_chunk(fragmented_mp4, frame_ctx))

        frames = []
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(15):
                async for frame in decoder.audio():
                    frames.append(frame)
                    if len(frames) >= 5:
                        break

        assert frames, "no PCM left ffmpeg: the audio pipe is not connected to the reader"
        assert all(frame.format == AUDIO_FORMAT for frame in frames)
        assert any(any(frame.pcm) for frame in frames), "audio arrived but was pure silence"
    finally:
        await decoder.stop()


@pytest.mark.asyncio
async def test_audio_pipe_is_named_by_its_real_descriptor(frame_ctx: FrameContext) -> None:
    """The command must reference the fd it was given, never a fixed 3."""
    decoder = FfmpegDecoder(
        ctx=frame_ctx,
        clock=MediaClock(),
        video_format=VIDEO_FORMAT,
        audio_format=AUDIO_FORMAT,
    )
    command = decoder._build_command(audio_fd=17)
    assert "pipe:17" in command
    assert "pipe:3" not in command


@pytest.mark.asyncio
async def test_stop_returns_with_both_pipes_undrained(
    frame_ctx: FrameContext, fragmented_mp4: bytes, crowded_fd_table: None
) -> None:
    """``stop()`` must return promptly even though nothing is consuming stdout or stderr.

    No reader is started here on purpose: that is the state a real teardown is in, and it is
    the state in which ``await process.wait()`` used to block forever.
    """
    decoder = FfmpegDecoder(
        ctx=frame_ctx,
        clock=MediaClock(),
        video_format=VIDEO_FORMAT,
        audio_format=AUDIO_FORMAT,
    )
    await decoder.start()
    await decoder.feed(_chunk(fragmented_mp4, frame_ctx))
    await asyncio.sleep(1.0)  # let ffmpeg fill the pipes it is writing into

    started = time.monotonic()
    await asyncio.wait_for(decoder.stop(), timeout=20.0)
    elapsed = time.monotonic() - started

    # Generous next to the 0.04s it actually takes, and far below the 5s grace period the
    # polite signal would cost if the pipes were still blocking ffmpeg's exit.
    assert elapsed < 4.0, f"stop() took {elapsed:.1f}s; the wait is blocking on full pipes"

    await decoder.stop()  # idempotent
