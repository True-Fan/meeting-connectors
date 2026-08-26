"""The Google Meet anti-corruption boundary, inbound.

Everything to the left of this module speaks the page wire protocol and the browser.
Everything to the right speaks ``src.domain``. This is the only place both vocabularies
exist, and it is why the router, the avatar client, the decoder and the pacer work
identically for Meet, Teams and Zoom without knowing any of them exists.

``tests/architecture/test_layering.py`` asserts that ``websocket/protocol.py`` is imported
nowhere outside ``connectors/google_meet/``, so this translation cannot be bypassed.

**The one assertion that carries the most weight here** is ``_require_avatar_format``. The
capture ``AudioContext`` is constructed with ``{sampleRate: 16000}``, so Web Audio
downsamples the conference's 48 kHz audio inside the browser's own graph and the worklet's
render quantum is already at exactly ``AVATAR_INPUT_FORMAT``. That gives this connector the
same zero-resample property Zoom gets from RTMS being natively ``L16 / 16 kHz / mono`` and
Teams gets from the media platform's ``Pcm16K``. Checking it rather than trusting it is
what makes the property *provable*: a page running a stale ``bridge.js`` that still builds
a 48 kHz context fails loudly here, instead of feeding the avatar audio it cannot use and
producing a chipmunk voice three services downstream.
"""

from __future__ import annotations

from src.connectors.google_meet.exceptions import BridgeProtocolError
from src.connectors.google_meet.websocket.protocol import (
    MIXED_SOURCE,
    AudioWireHeader,
    MeetFlags,
    MeetMessage,
)
from src.domain.avatar import AVATAR_INPUT_FORMAT
from src.domain.context import FrameContext
from src.domain.media import AudioFrame
from src.domain.meeting import ParticipantRef
from src.services.media.clock import MediaClock


def to_audio_frame(
    message: MeetMessage,
    *,
    ctx: FrameContext,
    clock: MediaClock,
) -> AudioFrame:
    """Translate an ``AUDIO_PCM`` message into a canonical ``AudioFrame``.

    The PTS comes from **our** media clock, not from the page's timestamp. The browser's
    ``AudioContext.currentTime`` is on the audio device's timeline, which starts at an
    arbitrary origin and drifts against the monotonic clock the rest of the pipeline is
    paced on. Mixing two timelines would corrupt the single-clock invariant that A/V sync
    depends on. The wire ``pts_us`` is retained on the message for latency attribution,
    never for presentation — the same rule every other connector's ingest follows
    for the sidecar's timestamps, and for the same reason.

    Raises:
        BridgeProtocolError: the payload is truncated, the format is not what the avatar
            contract requires, or the byte count is not a whole number of samples.
    """
    header, pcm = message.audio()
    _require_avatar_format(header)

    if len(pcm) % header.to_format().bytes_per_frame:
        raise BridgeProtocolError(
            f"audio payload of {len(pcm)} bytes is not a whole number of samples for "
            f"{header.to_format()}"
        )

    return AudioFrame(
        pcm=pcm,
        pts_us=clock.now_us(),
        format=header.to_format(),
        ctx=ctx,
        participant=_attribute(header, message.flags),
    )


def _require_avatar_format(header: AudioWireHeader) -> None:
    """Assert the page is sending exactly what the avatar accepts.

    Raises:
        BridgeProtocolError: the format differs from ``AVATAR_INPUT_FORMAT``.
    """
    actual = header.to_format()
    if actual != AVATAR_INPUT_FORMAT:
        raise BridgeProtocolError(
            f"the page sent {actual} but the avatar contract requires "
            f"{AVATAR_INPUT_FORMAT}; the capture AudioContext must be constructed with "
            "{sampleRate: 16000} — check that the injected js/bridge.js matches this "
            "build"
        )


def _attribute(header: AudioWireHeader, flags: MeetFlags) -> ParticipantRef | None:
    """Resolve which participant a frame came from.

    **Always ``None`` on this connector**, and that is a property of the capture design
    rather than a gap. ``js/bridge.js`` sums every remote track into one mono node before
    the worklet samples it, so an inbound frame is a mix of everyone speaking and there is
    no single participant it could be attributed to.

    ``None`` is a legitimate, expected value that ``EchoGuard`` is explicitly built to
    handle: with no per-participant attribution it falls back to its speaking gate, which
    ``session/google_meet_session.py`` configures it to do by passing
    ``per_participant_audio=False``. That gate is also the *only* echo defence this
    connector needs, because the WebRTC tap is inbound-only and the avatar's own audio can
    never enter it — the gate covers the acoustic path on a host that has speakers, not a
    software loop.

    The check is written out rather than hardcoded to ``None`` so that if a future page
    build ever sends per-track audio, this boundary starts attributing it instead of
    silently discarding the source id.
    """
    if MeetFlags.MIXED in flags or header.source_id == MIXED_SOURCE:
        return None
    return ParticipantRef(user_id=header.source_id)
