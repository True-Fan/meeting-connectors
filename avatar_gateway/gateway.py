"""avatar_gateway — the missing leg between meeting-connectors and agent-worker.

meeting-connectors' avatar port (``src/avatar/ws_transport.py``) speaks exactly one wire
protocol:

1. the bridge opens a WebSocket to ``MC_AVATAR__URL`` and sends ``AvatarClientHello`` as
   text JSON;
2. the agent replies with ``AvatarServerHello`` as text JSON;
3. thereafter both directions are binary — PCM 16 kHz mono s16le up, fragmented MP4 down.

That protocol has not changed. What changed is the AI behind it: the old Streaming Avatar
Agent (``agent.py`` + the original ``avatar_gateway.py``, both in ``/Users/dev/work/test/``)
is retired in favour of ``realtime-product/agent-worker`` — a LiveKit worker that is **never
dispatched by name**. It only receives a job when Initialisation (``:8000``) asks Worker
Handling (``:8100``) to assign a registered worker pod to a room, the same way
``agent-worker/devtools/local_session.py`` — the reference client for that flow — does it.

This process is still the translation between the two protocols, and it still lives on this
side of the boundary for the same reason as before: meeting-connectors contains no AI and
knows nothing about LiveKit, which is the property that lets one bridge serve Zoom, Teams and
Meet unchanged. Only one thing inside it is new — how the agent gets into the room:

    Meet PCM ──► LiveKit room (published mic track) ──┐
                                                        ├─► agent-worker STT → LLM → TTS
    Init POST /sessions ──► WH assign ──► agent-worker ┘   (assigned, not dispatched)
                                                              │
    Meet ◄── fMP4 ◄── ffmpeg mux ◄── agent speech track ◄────┘

Three details are load-bearing, and all were established by running ffmpeg rather than by
reasoning about it (the first two unchanged from the original gateway):

* **The fMP4 must always carry a video track**, whether or not the agent's own video is
  present yet. ``FfmpegDecoder`` maps video to one output pipe and audio to another. Given
  audio-only input the video output has no streams and ffmpeg refuses to start at all —
  ``Output file does not contain any stream`` — so the audio never arrives either. A
  placeholder video stream is not cosmetic; it is what makes the decoder run before any real
  video exists, and whenever it doesn't.

* **The muxer's audio timeline must be continuous.** The mp4 muxer interleaves, so it
  cannot emit a fragment covering a period in which one stream has no packets. TTS is
  bursty, so ``_pump_muxer`` writes silence between utterances. Without it, output stalls
  at the end of every sentence and resumes only when the agent next speaks.

* **The video timeline needs the same discipline, for the same reason.** An avatar backend
  (anam, musetalk, …) publishes real video once it starts rendering, not from the moment the
  job begins, and can drop a frame or two under load. ``_pump_video`` writes the *latest*
  real frame on every tick when one exists, and the placeholder otherwise — never a gap —
  exactly mirroring how ``_pump_muxer`` never lets the audio leg run dry.

meeting-connectors now gets **both** real audio and real video back from the agent when an
avatar backend is configured for it (``_on_track_subscribed`` subscribes to both kinds of
track); with no avatar backend it is audio plus the placeholder, same as before — the
connector-facing contract (one fMP4 stream, audio+video) never changes either way.

When the real avatar streams its own fMP4, set ``GATEWAY_PASSTHROUGH=true``: the muxer
drops out and the avatar's bytes are forwarded to the bridge verbatim.

Run it alongside the bridge, Initialisation, Worker Handling and agent-worker::

    python gateway.py              # listens on ws://127.0.0.1:8300/stream
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
import tempfile
import time
from dataclasses import dataclass, field

import httpx
import numpy as np
from dotenv import load_dotenv
from livekit import api, rtc
from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosed

from init_client import InitClient, InitError, InitSession

load_dotenv()

logging.basicConfig(
    level=os.getenv("GATEWAY_LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s - %(levelname)s %(name)s - %(message)s",
)
logger = logging.getLogger("avatar_gateway")

# -- the avatar contract, from src/domain/avatar.py -------------------------------------
#
# Mirrored here as constants rather than imported: this process must not depend on the
# bridge's source tree. If these drift, the handshake fails loudly on the next connection
# instead of silently degrading, which is the failure mode worth having.

PROTOCOL_VERSION = "1.2"
"""The avatar protocol version this gateway speaks.

1.1 adds inbound chat: the bridge sends ``{"kind":"chat",...}`` as a text frame and this process
relays it into the LiveKit room on ``lk.chat``. Advertising 1.0 while being able to handle chat
is not a harmless understatement — ``AvatarClient`` withholds chat below 1.1 by design, so the
bridge would silently never send any and log ``avatar.chat_unsupported``.

**1.2 adds ``{"kind":"meeting_context",...}``.** The bridge withholds that frame below 1.2.
Anything added to ``_handle_control`` must be matched here, or it is dead code by handshake.

Whether ``agent-worker`` actually has a handler registered on ``lk.chat`` /
``meeting.context`` is a separate question from this negotiation — see the note on
``CHAT_TOPIC`` below."""
INPUT_SAMPLE_RATE_HZ = 16_000
INPUT_CHANNELS = 1
CONTAINER = "fmp4"

BYTES_PER_SAMPLE = 2
TICK_MS = 20
"""Muxer write cadence. Matches the bridge's own 20 ms audio granularity."""

CHAT_TOPIC = "lk.chat"
"""Text-stream topic ``livekit.agents`` RoomIO listens on when ``text_enabled`` is set.

Mirrored from ``livekit.agents.types.TOPIC_CHAT`` rather than imported, because this process
should not depend on the agent framework's internals to know where to put a message — the topic
is the contract between us. Sent unconditionally: if ``agent-worker`` has not registered a
handler for this topic, LiveKit drops the stream with "no callback attached" and the avatar
simply stays quiet on chat, the same graceful-miss the old gateway relied on."""

MEETING_CONTEXT_TOPIC = "meeting.context"
"""Where standing meeting facts go — deliberately **not** ``lk.chat``.

``RoomIO``'s handler for ``lk.chat`` interrupts the agent, treats the text as the user's turn and
generates a spoken reply. That is exactly right for a typed question and exactly wrong for "who
is in the meeting": the avatar would announce the roster, unprompted, every time somebody's wifi
dropped. So this rides its own topic; a receiver with no handler for it just drops the stream."""

GREETING_READY_TOPIC = "greeting"
"""Mirrored from ``agent-worker/app/features/greeting/services/greeting_constants.py``.

agent-worker's greeting feature holds its opening line until something publishes one data
packet on this topic — it is meant for a browser to signal "the call is on screen now", so
the greeting is never spoken over a spinner. There is no spinner here: this gateway *is* the
call's whole client, and the meeting audio starts flowing the moment the agent's track is
subscribed. So the gateway sends this the instant an AGENT participant joins (see
``_on_participant_connected``), rather than leaving every session to pay the feature's own
20s default timeout in silence before it greets anyway."""

SILENCE_FLOOR = 512
"""Peak ``|sample|`` at or above which audio counts as sound, on int16's 32767 scale. Matches
the bridge's ``Pacer.SILENCE_FLOOR``, so both sides of the socket agree on what silence is."""


def _is_audible(pcm: bytes, *, floor: int = SILENCE_FLOOR, stride: int = 8) -> bool:
    """Whether this PCM carries sound rather than silence — for reporting only."""
    usable = len(pcm) - len(pcm) % 2
    if not usable:
        return False
    samples = memoryview(pcm)[:usable].cast("h")
    for index in range(0, len(samples), stride):
        sample = samples[index]
        if sample >= floor or sample <= -floor:
            return True
    return False


# ---------------------------------------------------------------------------------------
# I420 video — raw frames in, on the muxer's fixed geometry
# ---------------------------------------------------------------------------------------
#
# The muxer takes its video leg as raw I420 over a pipe now, not an ffmpeg-generated
# pattern, so real agent video and the placeholder are just two sources of the same bytes —
# ``_pump_video`` doesn't care which one it's holding.


def _make_solid_i420(width: int, height: int, hex_color: str) -> bytes:
    """One solid-color I420 frame. Same box the old lavfi ``color=`` source drew."""
    color = hex_color.removeprefix("0x").removeprefix("#")
    r, g, b = (int(color[i : i + 2], 16) for i in (0, 2, 4))
    # BT.601 — good enough for a flat placeholder box, not meant to be colorimetrically exact.
    y = round(0.299 * r + 0.587 * g + 0.114 * b)
    u = round(128 - 0.168736 * r - 0.331264 * g + 0.5 * b)
    v = round(128 + 0.5 * r - 0.418688 * g - 0.081312 * b)
    y, u, v = (max(0, min(255, c)) for c in (y, u, v))
    chroma_w, chroma_h = (width + 1) // 2, (height + 1) // 2
    return bytes([y]) * (width * height) + bytes([u]) * (chroma_w * chroma_h) + bytes(
        [v]
    ) * (chroma_w * chroma_h)


async def _decode_still_image_i420(
    path: str, width: int, height: int, ffmpeg_path: str
) -> bytes:
    """One frame of ``GATEWAY_VIDEO_IMAGE``, decoded and scaled once at muxer startup.

    ffmpeg does the decode+scale (any format in, one raw I420 frame out) so this process
    doesn't need an image-decoding dependency of its own for what is a one-shot, startup-only
    conversion — everything after this is pure Python on raw frames.
    """
    proc = await asyncio.create_subprocess_exec(
        ffmpeg_path,
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        path,
        "-frames:v",
        "1",
        "-vf",
        f"scale={width}:{height}",
        "-pix_fmt",
        "yuv420p",
        "-f",
        "rawvideo",
        "pipe:1",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    data, err = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(
            f"failed to decode {path!r} for the video placeholder: "
            f"{err.decode(errors='replace').strip()}"
        )
    expected = width * height + 2 * ((width + 1) // 2) * ((height + 1) // 2)
    return data[:expected]


def _nn_resize_plane(plane: memoryview, src_w: int, src_h: int, dst_w: int, dst_h: int) -> bytes:
    """Nearest-neighbour resize of one I420 plane. Cheap, and good enough for a live feed."""
    if src_w == dst_w and src_h == dst_h:
        return bytes(plane)
    arr = np.frombuffer(plane, dtype=np.uint8).reshape(src_h, src_w)
    rows = np.minimum((np.arange(dst_h) * src_h) // dst_h, src_h - 1)
    cols = np.minimum((np.arange(dst_w) * src_w) // dst_w, src_w - 1)
    return arr[rows][:, cols].tobytes()


def _scale_i420(frame: rtc.VideoFrame, width: int, height: int) -> bytes:
    """One agent video frame, resampled onto the muxer's fixed WxH.

    The muxer's ffmpeg command line is fixed at startup (``-s WxH``), so every frame handed
    to it — real or placeholder — has to already be exactly that size; ffmpeg itself never
    sees a frame whose dimensions could change the graph underneath it.
    """
    sw, sh = frame.width, frame.height
    if sw == width and sh == height:
        return bytes(frame.data)
    src_cw, src_ch = (sw + 1) // 2, (sh + 1) // 2
    dst_cw, dst_ch = (width + 1) // 2, (height + 1) // 2
    y = _nn_resize_plane(frame.get_plane(0), sw, sh, width, height)
    u = _nn_resize_plane(frame.get_plane(1), src_cw, src_ch, dst_cw, dst_ch)
    v = _nn_resize_plane(frame.get_plane(2), src_cw, src_ch, dst_cw, dst_ch)
    return y + u + v


@dataclass(frozen=True)
class Config:
    """Everything tunable, resolved once at startup."""

    livekit_url: str
    livekit_api_key: str
    livekit_api_secret: str

    # Initialisation (realtime-product) — how the agent actually gets into the room now.
    # See InitClient.create_session: this gateway is a customer as far as Init is concerned.
    init_url: str
    init_tenant_id: str
    init_username: str
    init_password: str
    agent_id: str

    # Loopback rather than 0.0.0.0, so a port collision is an *error* instead of a silent
    # shadowing — see Config.port's default for why 8300, not the old gateway's 8100.
    host: str = "127.0.0.1"
    port: int = 8300
    path: str = "/stream"

    room_prefix: str = "meet-"
    fixed_room: str | None = None
    bridge_identity: str = "meet-bridge"

    # The agent's speech is resampled to this rate on the way out of the room, so ffmpeg
    # sees one fixed format regardless of which TTS voice or provider is in use.
    agent_audio_rate_hz: int = 48_000

    # Video geometry — shared by the placeholder *and* any real avatar video
    # (``_pump_video`` scales real frames onto this same fixed size before muxing).
    # The small, cheap defaults below predate real avatar video: they were sized
    # only for the placeholder box, which "carries no information" so nothing was
    # lost rescaling it. Real avatar video (Anam etc.) arrives around 1152x768 to
    # 1280x720, and the bridge's own decoder upscales whatever it receives back up
    # to its own MC_MEDIA__VIDEO_WIDTH/HEIGHT (1280x720@25 by default) before
    # publishing — so at the small defaults, every real session was silently
    # downscaled here and then upscaled again downstream, which never recovers
    # detail and reads as visibly blurry video in the meeting. Set
    # GATEWAY_VIDEO_WIDTH/HEIGHT/FPS to match the bridge's own target (see its
    # MC_MEDIA__VIDEO_* settings) to remove that wasted round trip.
    video_width: int = 320
    video_height: int = 180
    video_fps: int = 10
    video_bitrate_kbps: int = 2000
    """Was hardcoded at 200 — fine for a 320x180 placeholder box, visibly blocky
    once real avatar video is flowing at anything close to 720p. 2000 (2 Mbps) is a
    reasonable talking-head-at-720p25 default for libx264's ultrafast/zerolatency
    preset; raise it further if the network budget allows and quality still isn't
    where it should be."""
    video_color: str = "0x202124"
    video_image: str | None = None

    fragment_ms: int = 100
    ffmpeg_path: str = "ffmpeg"
    passthrough: bool = False
    agent_join_timeout_s: float = 30.0

    @classmethod
    def from_env(cls) -> Config:
        url = os.getenv("LIVEKIT_URL")
        key = os.getenv("LIVEKIT_API_KEY")
        secret = os.getenv("LIVEKIT_API_SECRET")
        missing = [
            name
            for name, value in (
                ("LIVEKIT_URL", url),
                ("LIVEKIT_API_KEY", key),
                ("LIVEKIT_API_SECRET", secret),
            )
            if not value
        ]
        if missing:
            raise SystemExit(f"avatar_gateway: missing required env: {', '.join(missing)}")

        return cls(
            livekit_url=url,  # type: ignore[arg-type]
            livekit_api_key=key,  # type: ignore[arg-type]
            livekit_api_secret=secret,  # type: ignore[arg-type]
            init_url=os.getenv("INIT_URL", "http://localhost:8000"),
            init_tenant_id=os.getenv(
                "INIT_TENANT_ID", "11111111-1111-1111-1111-111111111111"
            ),
            init_username=os.getenv("INIT_USERNAME", "demo_user"),
            init_password=os.getenv("INIT_PASSWORD", "password123"),
            agent_id=os.getenv("AGENT_ID", "1"),
            host=os.getenv("GATEWAY_HOST", "127.0.0.1"),
            port=int(os.getenv("GATEWAY_PORT", "8300")),
            path=os.getenv("GATEWAY_PATH", "/stream"),
            room_prefix=os.getenv("GATEWAY_ROOM_PREFIX", "meet-"),
            fixed_room=os.getenv("LIVEKIT_ROOM") or None,
            agent_audio_rate_hz=int(os.getenv("GATEWAY_AGENT_AUDIO_RATE", "48000")),
            video_width=int(os.getenv("GATEWAY_VIDEO_WIDTH", "320")),
            video_height=int(os.getenv("GATEWAY_VIDEO_HEIGHT", "180")),
            video_fps=int(os.getenv("GATEWAY_VIDEO_FPS", "10")),
            video_bitrate_kbps=int(os.getenv("GATEWAY_VIDEO_BITRATE_KBPS", "2000")),
            video_color=os.getenv("GATEWAY_VIDEO_COLOR", "0x202124"),
            video_image=os.getenv("GATEWAY_VIDEO_IMAGE") or None,
            fragment_ms=int(os.getenv("GATEWAY_FRAGMENT_MS", "100")),
            ffmpeg_path=os.getenv("GATEWAY_FFMPEG", "ffmpeg"),
            passthrough=os.getenv("GATEWAY_PASSTHROUGH", "").lower() in ("1", "true", "yes"),
            agent_join_timeout_s=float(os.getenv("GATEWAY_AGENT_JOIN_TIMEOUT_S", "30")),
        )

    @property
    def api_url(self) -> str:
        """The REST base URL. Same host as the signalling URL, different scheme."""
        url = self.livekit_url
        if url.startswith("wss://"):
            return "https://" + url[len("wss://") :]
        if url.startswith("ws://"):
            return "http://" + url[len("ws://") :]
        return url


class HandshakeError(Exception):
    """The bridge's hello was absent, malformed, or incompatible."""


class MuxerStalledError(Exception):
    """A write into ffmpeg's stdin or video FIFO did not complete within
    ``MUXER_WRITE_TIMEOUT_S``.

    Found live, against a real Anam avatar: a hiccup in the avatar's video track
    (LiveKit's own engine logs ``native video stream queue overflow`` right before
    it) can leave ffmpeg's video input starved. Because ``-movflags
    +frag_keyframe`` interleaves audio and video into one fragmented stream,
    ffmpeg then stops draining its *audio* stdin too — so the muxer's audio leg
    blocks on a video problem, silently, forever, with no timeout anywhere to
    catch it. That is not hypothetical: it reproduced identically three times in a
    row, each time taking the whole gateway process down hard enough that even
    ``kill -9`` did not return it (a genuinely wedged native write), and the
    bridge's own WebSocket read then blocked in turn, pegging *that* process too.

    The old dispatch-by-name gateway (``/Users/dev/work/test/avatar_gateway.py``)
    never hit this because its video is synthesized entirely inside ffmpeg
    (``-f lavfi -i color=...``) — nothing external ever feeds it, so nothing
    external can ever stall it. This gateway cannot do that once real avatar video
    is involved, so instead every write that could block on ffmpeg is bounded: a
    stall becomes a clean, *session-scoped* failure (this session ends, the
    process does not) rather than a process-wide, unrecoverable hang."""


MUXER_WRITE_TIMEOUT_S = 3.0
"""How long a single write into ffmpeg may take before it counts as stalled.

Generous relative to the 20ms audio tick and 100ms video tick — a few slow
ticks under normal jitter must not trip this — but short enough that a real
stall ends the session in seconds, not indefinitely. The write itself may not
actually be cancellable (a blocking OS-level write in a thread keeps running
after this coroutine gives up on it — see ``write_video``), so the timeout does
not guarantee the stuck resource is reclaimed; it guarantees the *pump loop*
stops waiting on it and the session can be torn down instead of hanging with it.
"""


# ---------------------------------------------------------------------------------------
# fMP4 muxing
# ---------------------------------------------------------------------------------------


class Fmp4Muxer:
    """Wraps one ffmpeg subprocess: live PCM + raw video in, fragmented MP4 out.

    The output shape is what ``avatar/framing.py`` parses — ``ftyp``, ``moov``, then one
    ``moof``+``mdat`` per fragment — which is why ``+empty_moov`` is not optional. Without
    it ``moov`` lands at the end of the stream and the framer raises immediately, correctly
    reporting that a plain MP4 cannot be decoded while streaming.

    ``-flush_packets 1`` matters as much as the movflags: buffered fragments would arrive
    in bursts and add latency the bridge would then have to pace away.

    Video comes in over a named pipe (FIFO) rather than ffmpeg's own ``lavfi`` generator —
    the same discipline the audio leg already used, and for the same reason: whether a frame
    is real agent video or the placeholder, ``_pump_video`` decides that in Python and ffmpeg
    just reads whatever raw I420 bytes show up, on schedule, from one fixed-geometry source.
    """

    def __init__(self, config: Config) -> None:
        self._config = config
        self._process: asyncio.subprocess.Process | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._bytes_in = 0
        self._bytes_out = 0
        self._video_fifo_dir: str | None = None
        self._video_fifo_path: str | None = None
        self._video_fd: int | None = None
        self._placeholder_frame = b""

    @property
    def bytes_out(self) -> int:
        return self._bytes_out

    @property
    def placeholder_frame(self) -> bytes:
        """The precomputed frame ``_pump_video`` writes when no real video has arrived yet."""
        return self._placeholder_frame

    def _command(self) -> list[str]:
        cfg = self._config
        return [
            cfg.ffmpeg_path,
            "-hide_banner",
            "-loglevel", "warning",
            # The video leg: raw I420 frames at a fixed geometry, paced by _pump_video —
            # never ffmpeg's own clock, so real frames and placeholder frames are
            # indistinguishable to it.
            "-f", "rawvideo",
            "-pix_fmt", "yuv420p",
            "-s", f"{cfg.video_width}x{cfg.video_height}",
            "-r", str(cfg.video_fps),
            "-i", self._video_fifo_path,
            # The live audio leg. Read from stdin at the rate we write it, which the
            # 20 ms ticker keeps equal to real time.
            "-f", "s16le",
            "-ar", str(cfg.agent_audio_rate_hz),
            "-ac", str(INPUT_CHANNELS),
            "-i", "pipe:0",
            "-map", "0:v:0",
            "-map", "1:a:0",
            "-c:v", "libx264",
            "-preset", "ultrafast",
            "-tune", "zerolatency",
            "-pix_fmt", "yuv420p",
            "-g", str(max(cfg.video_fps * 2, 2)),
            "-b:v", f"{cfg.video_bitrate_kbps}k",
            "-c:a", "aac",
            "-b:a", "96k",
            # Exactly the flags the framer and FfmpegDecoder need. `default_base_moof`
            # keeps fragment offsets self-relative, so a fragment is interpretable without
            # having seen the ones before it.
            "-movflags", "+frag_keyframe+empty_moov+default_base_moof",
            "-frag_duration", str(cfg.fragment_ms * 1_000),
            "-flush_packets", "1",
            "-f", "mp4",
            "pipe:1",
        ]

    async def start(self) -> None:
        cfg = self._config
        self._placeholder_frame = (
            await _decode_still_image_i420(
                cfg.video_image, cfg.video_width, cfg.video_height, cfg.ffmpeg_path
            )
            if cfg.video_image
            else _make_solid_i420(cfg.video_width, cfg.video_height, cfg.video_color)
        )

        self._video_fifo_dir = tempfile.mkdtemp(prefix="avatar-gateway-video-")
        self._video_fifo_path = os.path.join(self._video_fifo_dir, "video.i420")
        os.mkfifo(self._video_fifo_path)

        command = self._command()
        logger.debug("muxer command: %s", " ".join(command))
        self._process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._stderr_task = asyncio.create_task(self._drain_stderr(), name="muxer-stderr")

        # ffmpeg is blocked right now trying to open the FIFO for reading — opening our end
        # for writing is what releases it. A real (blocking) open, so it runs off-thread.
        self._video_fd = await asyncio.to_thread(os.open, self._video_fifo_path, os.O_WRONLY)
        logger.info("muxer started (pid=%s)", self._process.pid)

    async def write(self, pcm: bytes) -> None:
        """Write one slice of the continuous audio timeline.

        Raises ``MuxerStalledError`` if ffmpeg does not drain stdin within
        ``MUXER_WRITE_TIMEOUT_S`` — see that error's docstring for why an
        unbounded ``drain()`` here is exactly what let a stuck video leg take
        the whole session (and, cascading from there, the bridge) down with it.
        """
        process = self._process
        if process is None or process.stdin is None:
            return
        try:
            process.stdin.write(pcm)
            await asyncio.wait_for(process.stdin.drain(), timeout=MUXER_WRITE_TIMEOUT_S)
            self._bytes_in += len(pcm)
        except (BrokenPipeError, ConnectionResetError):
            logger.warning("muxer stdin closed; ffmpeg has exited")
            self._process = None
        except TimeoutError:
            raise MuxerStalledError(
                f"audio write to ffmpeg stdin did not drain within {MUXER_WRITE_TIMEOUT_S}s"
            ) from None

    async def write_video(self, frame: bytes) -> None:
        """Write one frame of the continuous video timeline — real or placeholder.

        Raises ``MuxerStalledError`` if the write does not complete within
        ``MUXER_WRITE_TIMEOUT_S``. ``os.write`` to a FIFO is a genuine blocking OS
        call, run in a thread precisely so it cannot freeze the event loop by
        itself — but if ffmpeg has stopped reading, that thread hangs forever
        regardless, and giving up on *this coroutine* is what lets the pump loop
        (and the session) recover instead of waiting on a thread that may never
        return. The thread itself is abandoned, not cancelled — there is no safe
        way to interrupt a blocking syscall — which is a small, one-time leak per
        stall and strictly better than the alternative observed live: the whole
        process wedged into an uninterruptible sleep that not even ``kill -9``
        could clear.
        """
        fd = self._video_fd
        if fd is None:
            return
        try:
            await asyncio.wait_for(
                asyncio.to_thread(os.write, fd, frame), timeout=MUXER_WRITE_TIMEOUT_S
            )
        except OSError:
            logger.warning("video fifo closed; ffmpeg has exited")
            self._video_fd = None
        except TimeoutError:
            raise MuxerStalledError(
                f"video write to ffmpeg's FIFO did not complete within {MUXER_WRITE_TIMEOUT_S}s"
            ) from None

    async def read(self) -> bytes:
        """Read whatever fMP4 bytes are available. Empty bytes means end of stream.

        Box boundaries are irrelevant here — the bridge's framer reassembles them — so
        this deliberately does not try to align reads to fragments.
        """
        process = self._process
        if process is None or process.stdout is None:
            return b""
        data = await process.stdout.read(65_536)
        self._bytes_out += len(data)
        return data

    async def stop(self) -> None:
        stderr_task, self._stderr_task = self._stderr_task, None
        if stderr_task is not None:
            stderr_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await stderr_task

        fd, self._video_fd = self._video_fd, None
        if fd is not None:
            with contextlib.suppress(OSError):
                os.close(fd)

        process, self._process = self._process, None
        if process is not None:
            if process.stdin is not None and not process.stdin.is_closing():
                with contextlib.suppress(BrokenPipeError, ConnectionResetError, RuntimeError):
                    process.stdin.close()
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=5.0)
                except TimeoutError:
                    process.kill()
                    # Bounded even after SIGKILL: every stall observed live was
                    # this gateway's own write wedging, never ffmpeg refusing to
                    # die, but an unbounded wait here would let teardown itself
                    # hang if that ever stopped being true. A leaked process
                    # (reaped by init on this one's exit) beats aclose() never
                    # returning and stranding the room, the Init session, and
                    # this task forever.
                    try:
                        await asyncio.wait_for(process.wait(), timeout=5.0)
                    except TimeoutError:
                        logger.error(
                            "ffmpeg (pid=%s) did not exit after SIGKILL; abandoning it",
                            process.pid,
                        )

        fifo_dir, self._video_fifo_dir = self._video_fifo_dir, None
        if fifo_dir is not None:
            with contextlib.suppress(OSError):
                if self._video_fifo_path is not None:
                    os.remove(self._video_fifo_path)
                os.rmdir(fifo_dir)

        logger.info("muxer stopped (in=%dB out=%dB)", self._bytes_in, self._bytes_out)

    async def _drain_stderr(self) -> None:
        """Surface ffmpeg's complaints. An undrained stderr pipe eventually blocks it."""
        process = self._process
        if process is None or process.stderr is None:
            return
        async for line in process.stderr:
            text = line.decode("utf-8", errors="replace").strip()
            if text:
                logger.warning("ffmpeg: %s", text)


# ---------------------------------------------------------------------------------------
# one bridge connection
# ---------------------------------------------------------------------------------------


@dataclass
class SessionStats:
    pcm_frames_in: int = 0
    pcm_bytes_in: int = 0
    agent_frames: int = 0
    agent_bytes: int = 0
    # Measured on the samples, not on whether a chunk was available. A LiveKit track delivers
    # frames continuously whether or not the agent is talking, so counting "buffer had data"
    # reported 136s of speech for a one-sentence greeting — a metric that actively misled the
    # only question worth asking of this log: did the avatar actually say anything?
    silent_ticks: int = 0
    audible_ticks: int = 0
    fmp4_bytes_out: int = 0
    chat_in: int = 0
    meeting_context_in: int = 0
    dropped_capture: int = 0
    agent_buffer_overruns: int = 0
    real_video_ticks: int = 0
    placeholder_video_ticks: int = 0
    started_at: float = field(default_factory=time.monotonic)

    def summary(self) -> str:
        seconds = max(time.monotonic() - self.started_at, 1e-6)
        return (
            f"{seconds:.1f}s: meet→agent {self.pcm_bytes_in / 1024:.0f}KiB in "
            f"{self.pcm_frames_in} frames (dropped {self.dropped_capture}) · "
            f"agent→meet {self.agent_bytes / 1024:.0f}KiB in {self.agent_frames} frames, "
            f"{self.audible_ticks * TICK_MS / 1000:.1f}s audible / "
            f"{self.silent_ticks * TICK_MS / 1000:.1f}s silent · "
            f"fMP4 out {self.fmp4_bytes_out / 1024:.0f}KiB · "
            f"video {self.real_video_ticks} real / {self.placeholder_video_ticks} placeholder"
            + (f" · chat→agent {self.chat_in}" if self.chat_in else "")
            + (
                f" · context→agent {self.meeting_context_in}"
                if self.meeting_context_in
                else ""
            )
        )


class BridgeSession:
    """One bridge WebSocket ⇄ one LiveKit room ⇄ one agent-worker session."""

    def __init__(
        self, *, config: Config, connection: ServerConnection, init_client: InitClient
    ) -> None:
        self._config = config
        self._ws = connection
        self._init_client = init_client
        self._room = rtc.Room()
        self._muxer = Fmp4Muxer(config) if not config.passthrough else None
        self._source: rtc.AudioSource | None = None

        self._session_id = "unknown"
        self._room_name = ""
        self._init_session: InitSession | None = None
        self._agent_pcm = bytearray()
        # Latest frame only — a live feed has no backlog worth keeping, and holding one is
        # what keeps this from ever building latency the way a queue would. None until (and
        # unless) an avatar backend actually publishes video; _pump_video falls back to the
        # muxer's placeholder for as long as that stays true.
        self._agent_video_frame: rtc.VideoFrame | None = None
        self._real_video_logged = False
        self._agent_joined = asyncio.Event()
        # The most recent meeting brief, held until an agent exists to receive it. See
        # ``_deliver_meeting_context`` for why this is held where chat is dropped.
        self._pending_context: str | None = None
        self._audio_tasks: set[asyncio.Task[None]] = set()
        self.stats = SessionStats()

    # -- handshake ---------------------------------------------------------------------

    async def _handshake(self) -> None:
        """Complete the avatar handshake, or refuse in the shape the bridge understands.

        The bridge's ``check_handshake`` treats a major-version mismatch and a wrong
        container as unrecoverable and never retries them, so an explicit rejection here is
        strictly more useful than dropping the socket: the operator gets the reason in the
        bridge's own log.
        """
        try:
            raw = await asyncio.wait_for(self._ws.recv(), timeout=10.0)
        except (TimeoutError, ConnectionClosed) as exc:
            raise HandshakeError(f"no hello received: {exc}") from exc

        try:
            hello = json.loads(raw)
        except (json.JSONDecodeError, TypeError) as exc:
            raise HandshakeError("hello was not JSON") from exc

        self._session_id = str(hello.get("session_id", "unknown"))
        correlation_id = hello.get("correlation_id", "unknown")
        their_version = str(hello.get("protocol_version", "0.0"))
        audio = hello.get("audio") or {}

        logger.info(
            "hello from bridge: session=%s correlation=%s protocol=%s audio=%s expects=%s",
            self._session_id,
            correlation_id,
            their_version,
            audio,
            hello.get("expects_container"),
        )

        their_major = their_version.split(".", 1)[0]
        if their_major != PROTOCOL_VERSION.split(".", 1)[0]:
            await self._ws.send(
                json.dumps(
                    {
                        "protocol_version": PROTOCOL_VERSION,
                        "accepted": False,
                        "reason": f"gateway speaks {PROTOCOL_VERSION}, bridge speaks "
                        f"{their_version}",
                        "container": CONTAINER,
                    }
                )
            )
            raise HandshakeError(f"protocol mismatch: bridge={their_version}")

        # Not fatal, but worth shouting about: the ticker below assumes the bridge's PCM is
        # exactly this format, and a mismatch would be published into the room at the wrong
        # rate — which sounds like a chipmunk, not like a bug.
        rate = audio.get("sample_rate_hz")
        channels = audio.get("channels")
        if rate not in (None, INPUT_SAMPLE_RATE_HZ) or channels not in (None, INPUT_CHANNELS):
            logger.warning(
                "bridge offers %s Hz / %s ch; gateway assumes %s Hz / %s ch",
                rate,
                channels,
                INPUT_SAMPLE_RATE_HZ,
                INPUT_CHANNELS,
            )

        await self._ws.send(
            json.dumps(
                {
                    "protocol_version": PROTOCOL_VERSION,
                    "accepted": True,
                    "container": CONTAINER,
                }
            )
        )

    # -- LiveKit ----------------------------------------------------------------------

    def _token(self) -> str:
        """The gateway's own room-join token.

        Doubles as the ``livekit_token`` handed to Initialisation in
        ``_start_agent_session`` — this gateway is, as far as Init is concerned, a customer
        joining with its own microphone, exactly like ``devtools/local_session.py``'s
        ``customer_token``.

        Deliberately **no** ``room_create`` grant. agent-worker's own
        ``LiveKitSessionController._get_or_prepare_room`` is a check-then-act:
        ``list_rooms`` then, if none was found, ``create_room(metadata=...)``. If this
        gateway's own ``room.connect()`` implicitly creates the room first — which
        ``room_create=True`` would do, since ``run()`` used to join before asking Init for a
        session — agent-worker's ``list_rooms`` can lose that race, fall through to
        ``create_room`` on a room that already (silently) exists, and LiveKit hands back
        that already-existing room's metadata unchanged: **empty**. The job then connects to
        a room with no ``agent_id`` in it and refuses — instantly, silently, and only
        sometimes, which is exactly what made it look like a Meet-specific flake rather than
        an ordering bug. ``run()`` now always calls ``_start_agent_session()`` before
        ``_join_room()`` so agent-worker is guaranteed to create the room, with its metadata
        intact, before this gateway's join could ever race it.
        """
        return (
            api.AccessToken(self._config.livekit_api_key, self._config.livekit_api_secret)
            .with_identity(self._config.bridge_identity)
            .with_name("Meeting Bridge")
            .with_grants(
                api.VideoGrants(
                    room_join=True,
                    room=self._room_name,
                    can_publish=True,
                    can_subscribe=True,
                )
            )
            .to_jwt()
        )

    async def _join_room(self) -> None:
        """Join the room agent-worker has already created (see ``run()`` / ``_token()``)."""
        cfg = self._config

        # Handlers are registered before connecting so an agent already sitting in a reused
        # fixed room is picked up from the initial participant snapshot rather than missed.
        self._room.on("track_subscribed", self._on_track_subscribed)
        self._room.on("participant_connected", self._on_participant_connected)
        self._room.on("disconnected", lambda *_: logger.warning("room disconnected"))

        await self._room.connect(cfg.livekit_url, self._token())
        logger.info("joined room %s as %s", self._room_name, cfg.bridge_identity)

        # Publish the meeting's audio as an ordinary microphone track. This is the track
        # the agent's STT and VAD subscribe to — it is the entire input side.
        self._source = rtc.AudioSource(INPUT_SAMPLE_RATE_HZ, INPUT_CHANNELS)
        track = rtc.LocalAudioTrack.create_audio_track("meet-audio", self._source)
        await self._room.local_participant.publish_track(
            track, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)
        )
        logger.info("published meet-audio track (%s Hz mono)", INPUT_SAMPLE_RATE_HZ)

        for participant in self._room.remote_participants.values():
            self._on_participant_connected(participant)

    async def _start_agent_session(self) -> None:
        """Ask Initialisation for a session, so Worker Handling assigns agent-worker into
        this room — before this gateway itself joins it (see ``_token()`` for why the order
        matters).

        Replaces the old gateway's ``lk.agent_dispatch.create_dispatch(agent_name=...)`` —
        agent-worker is never dispatched by name, it only takes a job when WH assigns one,
        and only Init can ask WH for that. This is exactly what
        ``agent-worker/devtools/local_session.py`` does by hand; the gateway does it here
        with the room name it has already resolved and a token minted for that room.
        """
        cfg = self._config
        try:
            self._init_session = await self._init_client.create_session(
                room_name=self._room_name,
                agent_id=cfg.agent_id,
                livekit_url=cfg.livekit_url,
                livekit_token=self._token(),
            )
        except InitError as exc:
            raise HandshakeError(f"could not start an agent-worker session: {exc}") from exc
        logger.info(
            "init session %s ready (agent_id=%s) — agent-worker assigned to room %s",
            self._init_session.session_id,
            cfg.agent_id,
            self._room_name,
        )

    def _on_participant_connected(self, participant: rtc.RemoteParticipant) -> None:
        logger.info(
            "participant joined: identity=%s kind=%s", participant.identity, participant.kind
        )
        if participant.kind == rtc.ParticipantKind.PARTICIPANT_KIND_AGENT:
            self._agent_joined.set()
            # Whatever the roster looked like before the agent existed is still true now.
            task = asyncio.create_task(
                self._flush_meeting_context(), name="flush-meeting-context"
            )
            self._audio_tasks.add(task)
            task.add_done_callback(self._audio_tasks.discard)

            task = asyncio.create_task(self._signal_greeting_ready(), name="greeting-ready")
            self._audio_tasks.add(task)
            task.add_done_callback(self._audio_tasks.discard)

    async def _signal_greeting_ready(self) -> None:
        """Tell agent-worker's greeting hook there is nothing to wait for.

        See ``GREETING_READY_TOPIC``. Best-effort: a publish failure here costs a
        20s-late greeting (the feature's own timeout still fires it), never a
        silent call.
        """
        try:
            await self._room.local_participant.publish_data(
                payload=b"{}", topic=GREETING_READY_TOPIC, reliable=True
            )
        except Exception as exc:
            logger.warning("failed to signal greeting-ready: %s", exc)

    def _on_track_subscribed(
        self,
        track: rtc.Track,
        publication: rtc.RemoteTrackPublication,
        participant: rtc.RemoteParticipant,
    ) -> None:
        if participant.identity == self._config.bridge_identity:
            return  # our own track, never routed back into the muxer

        if track.kind == rtc.TrackKind.KIND_AUDIO:
            logger.info(
                "subscribed to audio from %s (sid=%s) — agent speech will now reach the meeting",
                participant.identity,
                publication.sid,
            )
            self._agent_joined.set()
            task = asyncio.create_task(self._read_agent_audio(track), name="agent-audio")
            self._audio_tasks.add(task)
            task.add_done_callback(self._audio_tasks.discard)
        elif track.kind == rtc.TrackKind.KIND_VIDEO:
            logger.info(
                "subscribed to video from %s (sid=%s) — real avatar video will now reach "
                "the meeting instead of the placeholder",
                participant.identity,
                publication.sid,
            )
            task = asyncio.create_task(self._read_agent_video(track), name="agent-video")
            self._audio_tasks.add(task)
            task.add_done_callback(self._audio_tasks.discard)

    async def _read_agent_audio(self, track: rtc.Track) -> None:
        """Drain the agent's speech track into the jitter buffer.

        A fixed ``sample_rate`` here is what lets ffmpeg's command line be static: whatever
        the TTS produces, this side of the room always hands out the same format.
        """
        max_buffered = self._config.agent_audio_rate_hz * BYTES_PER_SAMPLE  # 1s
        stream = rtc.AudioStream(
            track,
            sample_rate=self._config.agent_audio_rate_hz,
            num_channels=INPUT_CHANNELS,
            frame_size_ms=TICK_MS,
        )
        try:
            async for event in stream:
                data = bytes(event.frame.data)
                self._agent_pcm.extend(data)
                self.stats.agent_frames += 1
                self.stats.agent_bytes += len(data)

                # A buffer that only grows means the room is delivering faster than the
                # ticker drains — clock drift, not a burst. Trimming the oldest audio keeps
                # latency bounded; letting it grow would make the avatar reply later and
                # later as the meeting went on.
                if len(self._agent_pcm) > max_buffered:
                    overrun = len(self._agent_pcm) - max_buffered
                    del self._agent_pcm[:overrun]
                    self.stats.agent_buffer_overruns += 1
        finally:
            with contextlib.suppress(Exception):
                await stream.aclose()

    async def _read_agent_video(self, track: rtc.Track) -> None:
        """Keep only the most recent real avatar video frame, for ``_pump_video`` to mux in.

        No jitter buffer here the way audio has one — a live face has no "backlog" worth
        preserving, so the latest frame overwriting the previous one *is* the correct
        behaviour, not a simplification of it.
        """
        stream = rtc.VideoStream(track, format=rtc.VideoBufferType.I420)
        try:
            async for event in stream:
                self._agent_video_frame = event.frame
                if not self._real_video_logged:
                    self._real_video_logged = True
                    logger.info(
                        "first real video frame from the agent: %dx%d",
                        event.frame.width,
                        event.frame.height,
                    )
        finally:
            with contextlib.suppress(Exception):
                await stream.aclose()
            # This track is gone — fall back to the placeholder rather than freezing the
            # meeting on whatever the avatar's face last looked like.
            self._agent_video_frame = None

    # -- the five pumps ----------------------------------------------------------------

    async def _pump_meet_to_room(self) -> None:
        """Bridge PCM → the published LiveKit track. The input side, end to end.

        Text frames on the same socket are control, not media — chat, so far. They are handled
        inline here rather than on their own task because they arrive on this socket and this is
        the only reader of it; a second reader would race for frames.
        """
        source = self._source
        async for message in self._ws:
            if isinstance(message, str):
                await self._handle_control(message)
                continue
            if source is None:
                continue

            samples = len(message) // (BYTES_PER_SAMPLE * INPUT_CHANNELS)
            if samples == 0:
                continue
            frame = rtc.AudioFrame(
                data=message,
                sample_rate=INPUT_SAMPLE_RATE_HZ,
                num_channels=INPUT_CHANNELS,
                samples_per_channel=samples,
            )
            try:
                # Bounded rather than unbounded: blocking here would stop reading the
                # socket, and the bridge's send queue would then drop meeting audio for a
                # reason it could not diagnose.
                await asyncio.wait_for(source.capture_frame(frame), timeout=1.0)
            except TimeoutError:
                self.stats.dropped_capture += 1
                continue
            self.stats.pcm_frames_in += 1
            self.stats.pcm_bytes_in += len(message)

    async def _handle_control(self, raw: str) -> None:
        """Route one JSON control frame from the bridge.

        Unknown kinds are logged and ignored rather than rejected: the avatar protocol's minor
        version is additive, and a bridge one minor ahead of this gateway must not be a failure.
        """
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            logger.warning("control frame was not JSON: %s", raw[:200])
            return

        kind = payload.get("kind")
        if kind == "chat":
            await self._deliver_chat(payload)
            return
        if kind == "meeting_context":
            await self._deliver_meeting_context(payload)
            return
        logger.debug("ignoring control frame of kind %r", kind)

    async def _deliver_meeting_context(self, payload: dict[str, object]) -> None:
        """Give the agent standing facts about the meeting, without it saying anything.

        Routed to ``MEETING_CONTEXT_TOPIC`` rather than ``lk.chat`` — see that constant for why
        putting attendance on the chat topic would make the avatar read the roster aloud every
        time it changed.

        **Held and replayed when the agent is late.** Unlike chat, this is not dropped if the
        agent has not joined yet, and the difference matters: the bridge only resends when the
        roster *changes*, so a brief dropped now may be the only one this meeting ever produces —
        a two-person call whose roster never changes again would leave the agent permanently
        unable to answer. Chat can be dropped because another message is another event; this is
        current state, and current state is worth keeping until somebody can receive it.
        """
        text = str(payload.get("text") or "").strip()
        if not text:
            return

        # Newest wins: the brief is a full replacement for the previous one, not a delta, so a
        # queue would only hold rosters that are already wrong.
        self._pending_context = text

        room = self._room
        if room is None or not self._agent_joined.is_set():
            logger.info(
                "meeting context held until the agent joins: %.120s",
                text,
            )
            return

        await self._flush_meeting_context()

    async def _flush_meeting_context(self) -> None:
        """Send the held brief, if there is one. Never raises."""
        text = self._pending_context
        room = self._room
        if not text or room is None:
            return

        try:
            await room.local_participant.send_text(text, topic=MEETING_CONTEXT_TOPIC)
        except Exception as exc:
            # Kept rather than cleared, so the next brief or the next agent join retries it.
            logger.error("failed to deliver meeting context to the agent: %s", exc)
            return

        self._pending_context = None
        self.stats.meeting_context_in += 1
        logger.info(
            "meeting context → agent (%d total): %.160s",
            self.stats.meeting_context_in,
            text,
        )

    async def _deliver_chat(self, payload: dict[str, object]) -> None:
        """Hand a typed message to the agent as a user turn.

        Sent as a text stream on ``lk.chat``, which is the topic ``RoomIO`` registers a handler
        for when ``text_enabled`` is set. Its default callback does exactly what a typed
        question deserves: interrupt whatever the agent is saying, treat the text as the user's
        turn, and generate a spoken reply. So chat lands in the same conversation state as
        speech rather than in a parallel one — which is the whole point of routing it here
        instead of answering it in the bridge.

        The sender is prefixed onto the text when known. The agent has no other way to learn
        who asked, and "Priya asks: …" is something an LLM can use where a bare question from
        nobody in particular is not.
        """
        text = str(payload.get("text") or "").strip()
        if not text:
            return

        sender = payload.get("sender")
        body = f"{sender}: {text}" if isinstance(sender, str) and sender else text

        room = self._room
        if room is None or not self._agent_joined.is_set():
            logger.warning(
                "chat arrived before the agent joined; dropping: %.80s",
                body,
            )
            return

        try:
            await room.local_participant.send_text(body, topic=CHAT_TOPIC)
        except Exception as exc:
            logger.error("failed to deliver chat to the agent: %s", exc)
            return

        self.stats.chat_in += 1
        logger.info(
            "chat → agent (%d total): %.120s",
            self.stats.chat_in,
            body,
        )

    async def _pump_muxer(self) -> None:
        """Keep the muxer's audio timeline continuous, filling gaps with silence.

        The deadline advances by a fixed step rather than sleeping a fixed step, so the
        cadence does not drift by the cost of each iteration. Falling far behind resets the
        deadline instead of trying to catch up: bursting audio into the muxer would corrupt
        its timeline, which is precisely the mistake ``Pacer`` avoids downstream.
        """
        muxer = self._muxer
        if muxer is None:
            return
        tick_bytes = (
            self._config.agent_audio_rate_hz * TICK_MS // 1000
        ) * BYTES_PER_SAMPLE * INPUT_CHANNELS
        silence = bytes(tick_bytes)
        tick_s = TICK_MS / 1000
        next_tick = time.monotonic()

        while True:
            next_tick += tick_s
            delay = next_tick - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            elif delay < -tick_s * 10:
                next_tick = time.monotonic()

            available = len(self._agent_pcm)
            if available >= tick_bytes:
                chunk = bytes(self._agent_pcm[:tick_bytes])
                del self._agent_pcm[:tick_bytes]
            elif available:
                chunk = bytes(self._agent_pcm) + silence[available:]
                self._agent_pcm.clear()
            else:
                chunk = silence

            if _is_audible(chunk):
                self.stats.audible_ticks += 1
            else:
                self.stats.silent_ticks += 1
            await muxer.write(chunk)

    async def _pump_video(self) -> None:
        """Keep the muxer's video timeline continuous, on the agent's real face when there
        is one and the placeholder otherwise.

        Same deadline-stepping cadence as ``_pump_muxer``, and the same reason: a fixed step
        rather than a fixed sleep keeps the frame rate from drifting by the cost of scaling
        each frame.
        """
        muxer = self._muxer
        if muxer is None:
            return
        tick_s = 1.0 / self._config.video_fps
        next_tick = time.monotonic()

        while True:
            next_tick += tick_s
            delay = next_tick - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            elif delay < -tick_s * 10:
                next_tick = time.monotonic()

            frame = self._agent_video_frame
            if frame is not None:
                try:
                    data = _scale_i420(frame, self._config.video_width, self._config.video_height)
                    self.stats.real_video_ticks += 1
                except Exception as exc:
                    logger.warning("failed to scale the agent's video frame: %s", exc)
                    data = muxer.placeholder_frame
                    self.stats.placeholder_video_ticks += 1
            else:
                data = muxer.placeholder_frame
                self.stats.placeholder_video_ticks += 1
            await muxer.write_video(data)

    async def _pump_room_to_meet(self) -> None:
        """Muxed fMP4 → the bridge. The output side."""
        muxer = self._muxer
        if muxer is None:
            return
        first = True
        while True:
            data = await muxer.read()
            if not data:
                logger.warning("muxer produced end-of-stream")
                return
            try:
                await self._ws.send(data)
            except ConnectionClosed:
                return
            self.stats.fmp4_bytes_out += len(data)
            if first:
                first = False
                logger.info("first fMP4 bytes sent to bridge (%d bytes)", len(data))

    async def _warn_if_agent_never_joins(self) -> None:
        """Log loudly if agent-worker never actually shows up, once the session is started.

        Unlike the old dispatch-by-name gateway, there is no independent "redispatch" to
        retry here: agent-worker is already on its way in via the Init session created in
        ``_start_agent_session``, and a stuck LiveKit signalling connection on its side
        cannot be worked around by asking Init again for the same room — Init has already
        done its part. So this only reports, once, instead of retrying.
        """
        try:
            await asyncio.wait_for(
                self._agent_joined.wait(), timeout=self._config.agent_join_timeout_s
            )
        except TimeoutError:
            logger.error(
                "AGENT NEVER JOINED %s after %.0fs (init session=%s). The avatar is in the "
                "meeting and will stay silent. Init accepted the session but no AGENT "
                "participant showed up in the room — check agent-worker's own logs and "
                "`curl localhost:8080/readyz`; a worker can stay registered with Worker "
                "Handling while its LiveKit signalling connection is dead, so 'it's still "
                "running' is not evidence it is working.",
                self._room_name,
                self._config.agent_join_timeout_s,
                self._init_session.session_id if self._init_session else "none",
            )
            return
        logger.info("agent is in the room; the conversation loop is closed")

    # -- lifecycle ---------------------------------------------------------------------

    async def _setup_agent_and_room(self) -> None:
        """Agent-worker creates the room (with its assignment metadata) before this gateway
        ever joins it — see ``_token()``'s docstring for the race that ordering avoids."""
        await self._start_agent_session()
        await self._join_room()

    async def run(self) -> None:
        await self._handshake()
        cfg = self._config
        self._room_name = cfg.fixed_room or f"{cfg.room_prefix}{self._session_id}"
        if self._muxer is not None:
            await self._muxer.start()

        # First-completed rather than a TaskGroup, and the distinction is not stylistic. A
        # TaskGroup waits for *every* task, and `_pump_muxer`/`_pump_video` are infinite
        # tickers that never return on their own — so a bridge that closed the socket
        # **cleanly** left the group waiting forever on it. `websockets` ends its iterator
        # without raising on a normal close, so nothing cancelled the siblings, and the
        # session leaked: room still joined, ffmpeg still running, agent still sitting in
        # the room. Observed exactly that way — one session still logging stats fifteen
        # minutes after its bridge had gone.
        #
        # These three don't need the room or the agent, and starting them immediately keeps
        # the placeholder (silence + a static frame) flowing to the bridge from the first
        # instant — a black screen the whole time _setup_agent_and_room is in flight (Init
        # waiting on WH capacity can take real seconds) would be a worse regression than the
        # ordering fix above is a win. `_pump_meet_to_room` is the one exception: it needs
        # `self._source`, which only exists once `_join_room` has run, so it — and the
        # "agent never joined" watcher, which only makes sense once an agent could plausibly
        # have joined — are added once setup finishes, below.
        pending: set[asyncio.Task[None]] = {
            asyncio.create_task(self._pump_muxer(), name="muxer-pump"),
            asyncio.create_task(self._pump_video(), name="video-pump"),
            asyncio.create_task(self._pump_room_to_meet(), name="room-to-meet"),
            asyncio.create_task(self._setup_agent_and_room(), name="setup"),
        }
        watchers = [
            asyncio.create_task(self._log_stats(), name="stats"),
        ]
        setup_task = next(t for t in pending if t.get_name() == "setup")

        try:
            while True:
                done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                if setup_task in done:
                    try:
                        setup_task.result()
                    except Exception:
                        for task in pending:
                            task.cancel()
                        await asyncio.gather(*pending, return_exceptions=True)
                        raise
                    pending.add(
                        asyncio.create_task(self._pump_meet_to_room(), name="meet-to-room")
                    )
                    watchers.append(
                        asyncio.create_task(
                            self._warn_if_agent_never_joins(), name="agent-watch"
                        )
                    )
                    continue

                # Any other pump finishing means the session is over.
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                # Re-raise a genuine failure so the caller logs it; a pump that simply
                # returned (the bridge hung up) ends the session quietly.
                for task in done:
                    task.result()
                break
        finally:
            for task in watchers:
                task.cancel()
            await asyncio.gather(*watchers, return_exceptions=True)

    async def _log_stats(self) -> None:
        """Report every 10s, and say plainly when a direction has stopped moving.

        Diagnosing this from the counters alone meant reading three logs side by side and
        noticing that one number had stopped growing — which is exactly the kind of thing
        nobody spots while a meeting is going wrong. A stalled direction now names itself,
        because the two halves fail for entirely different reasons and the fix depends on
        which one it is.
        """
        previous: tuple[int, int, int] | None = None
        while True:
            await asyncio.sleep(10)
            current = (
                self.stats.pcm_frames_in,
                self.stats.agent_frames,
                self.stats.fmp4_bytes_out,
            )
            logger.info("session %s — %s", self._session_id, self.stats.summary())

            if previous is not None:
                if current[0] == previous[0]:
                    logger.warning(
                        "STALLED meet→agent: no PCM from the bridge in 10s. The meeting's "
                        "audio is not reaching the agent — the bridge's capture tap or its "
                        "echo gate, not this gateway.",
                    )
                if current[2] == previous[2]:
                    logger.warning(
                        "STALLED agent→meet: no fMP4 produced in 10s (agent frames %s). The "
                        "avatar cannot be heard — the muxer or the agent's track, not Meet.",
                        "also stalled" if current[1] == previous[1] else "still arriving",
                    )
            previous = current

    async def aclose(self) -> None:
        for task in list(self._audio_tasks):
            task.cancel()
        if self._muxer is not None:
            await self._muxer.stop()
        with contextlib.suppress(Exception):
            await self._room.disconnect()

        if self._init_session is not None:
            await self._init_client.end_session(self._init_session)

        # Delete the room too, so agent-worker's own participant leaves without waiting on
        # LiveKit's empty-room timeout. Belt and suspenders with the Init session-end above —
        # harmless if the room is already gone, and wrapped the same way it always was.
        if self._room_name and not self._config.fixed_room:
            cfg = self._config
            with contextlib.suppress(Exception):
                async with api.LiveKitAPI(
                    cfg.api_url, cfg.livekit_api_key, cfg.livekit_api_secret
                ) as lk:
                    await lk.room.delete_room(api.DeleteRoomRequest(room=self._room_name))
        logger.info("session %s closed — %s", self._session_id, self.stats.summary())


# ---------------------------------------------------------------------------------------
# server
# ---------------------------------------------------------------------------------------


class Gateway:
    def __init__(self, config: Config) -> None:
        self._config = config
        self._sessions = 0
        # Live sessions, so shutdown can close them. Without this, stopping the gateway left
        # every in-flight session's LiveKit room alive with the bridge still joined and its
        # ffmpeg still running — found three such rooms hours old, one per gateway I had
        # stopped. `serve()` exiting closes listeners, not the sessions already running on them.
        self._live: set[BridgeSession] = set()

        # One shared HTTP client and one shared Init credential for the whole process —
        # see InitClient's own docstring for why re-minting a credential per meeting would
        # be wasteful rather than more correct.
        self._http = httpx.AsyncClient(timeout=30.0)
        self._init_client = InitClient(
            http=self._http,
            base_url=config.init_url,
            tenant_id=config.init_tenant_id,
            username=config.init_username,
            password=config.init_password,
        )

    async def handle(self, connection: ServerConnection) -> None:
        path = connection.request.path if connection.request else ""
        if path.split("?", 1)[0] != self._config.path:
            logger.warning("rejecting connection to %r (expected %r)", path, self._config.path)
            await connection.close(code=1008, reason="unexpected path")
            return

        self._sessions += 1
        logger.info("bridge connected (#%d) from %s", self._sessions, connection.remote_address)
        session = BridgeSession(
            config=self._config, connection=connection, init_client=self._init_client
        )
        self._live.add(session)
        try:
            await session.run()
        except HandshakeError as exc:
            logger.error("handshake failed: %s", exc)
        except BaseExceptionGroup as group:
            # The pumps run in a TaskGroup, so everything from `run()` arrives grouped. A
            # closed socket is how a session normally ends — the bridge hung up — and must
            # not be logged as a failure, so it is split out rather than lumped in.
            closed, rest = group.split(ConnectionClosed)
            if closed is not None:
                logger.info("bridge disconnected")
            if rest is not None:
                logger.error("session failed: %r", rest, exc_info=rest)
        except ConnectionClosed:
            logger.info("bridge disconnected")
        except Exception:
            logger.exception("session failed")
        finally:
            self._live.discard(session)
            await session.aclose()

    async def _close_live_sessions(self) -> None:
        """Close every in-flight session, so shutdown does not leak rooms.

        Each ``aclose`` disconnects from LiveKit, stops the muxer, ends the Init session and
        deletes the room — which is what makes agent-worker leave too. Concurrently and
        tolerantly: one session failing to tear down must not strand the others.
        """
        live, self._live = list(self._live), set()
        if not live:
            return
        logger.info("closing %d live session(s) before shutdown", len(live))
        await asyncio.gather(*(s.aclose() for s in live), return_exceptions=True)

    async def serve_forever(self) -> None:
        cfg = self._config
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, stop.set)

        try:
            server = serve(
                self.handle,
                cfg.host,
                cfg.port,
                # fMP4 fragments are large and the bridge's framer already bounds them.
                max_size=None,
                ping_interval=20.0,
                ping_timeout=20.0,
                # This socket carries the avatar's fMP4 — already H.264-encoded, so
                # deflating it again buys nothing and costs real CPU. Confirmed live:
                # the bridge's matching page-server socket (meeting-connectors'
                # src/connectors/zoom_web/page/server.py) sampled at 98% of its
                # process's CPU inside zlib.deflate once real avatar video was
                # flowing through it — the same default applies here.
                compression=None,
            )
            await server.__aenter__()
        except OSError as exc:
            raise SystemExit(
                f"avatar_gateway: cannot bind {cfg.host}:{cfg.port} — {exc}.\n"
                f"Something else is already the avatar. Check with:\n"
                f"    lsof -nP -iTCP:{cfg.port} -sTCP:LISTEN\n"
                "Worker Handling owns 8100 in this architecture — move this gateway with "
                "GATEWAY_PORT and point meeting-connectors' MC_AVATAR__URL at the new one."
            ) from exc

        try:
            logger.info(
                "avatar_gateway listening on ws://%s:%d%s — agent_id=%r via init=%s "
                "livekit=%s%s",
                cfg.host,
                cfg.port,
                cfg.path,
                cfg.agent_id,
                cfg.init_url,
                cfg.livekit_url,
                " (passthrough)" if cfg.passthrough else "",
            )
            await stop.wait()
        finally:
            await self._close_live_sessions()
            await server.__aexit__(None, None, None)
            await self._http.aclose()
        logger.info("avatar_gateway stopped")


def main() -> None:
    asyncio.run(Gateway(Config.from_env()).serve_forever())


if __name__ == "__main__":
    main()
