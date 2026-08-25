"""The Teams anti-corruption boundary, inbound.

Everything to the left of this module speaks the sidecar wire protocol and Graph.
Everything to the right speaks ``src.domain``. This is the only place both vocabularies
exist, and it is why the router, decoder, pacer and publisher work identically for
Teams and Zoom without knowing either one exists.

``tests/architecture/test_layering.py`` asserts that ``graph/models.py`` and
``sidecar/protocol.py`` are imported nowhere outside ``connectors/teams/`` — so this
translation cannot be bypassed.
"""

from __future__ import annotations

from src.connectors.teams.exceptions import SidecarProtocolError
from src.connectors.teams.graph.models import ParticipantInfo
from src.connectors.teams.sidecar.protocol import (
    MIXED_SOURCE,
    AudioWireHeader,
    TeamsFlags,
    TeamsMessage,
)
from src.domain.avatar import AVATAR_INPUT_FORMAT
from src.domain.context import FrameContext
from src.domain.media import AudioFrame
from src.domain.meeting import ParticipantRef
from src.services.media.clock import MediaClock


def to_participant_ref(info: ParticipantInfo) -> ParticipantRef:
    """Translate a roster entry.

    The media source id becomes ``ParticipantRef.user_id``. It is the identifier
    unmixed audio buffers are tagged with, which makes it the only one that can be
    matched against an inbound frame — an AAD object id could not be, so it stays
    behind the boundary as diagnostic detail.
    """
    return ParticipantRef(user_id=info.msi, display_name=info.display_name)


def to_audio_frame(
    message: TeamsMessage,
    *,
    ctx: FrameContext,
    clock: MediaClock,
    roster: dict[int, ParticipantRef] | None = None,
) -> AudioFrame:
    """Translate an ``AUDIO_PCM`` message into a canonical ``AudioFrame``.

    The PTS comes from **our** media clock, not from the sidecar's timestamp. The media
    platform's timestamps are on the Windows host's timeline; mixing two clocks would
    corrupt the single-clock invariant that A/V sync depends on, and across two
    machines it would also fold in their clock offset. The wire ``pts_us`` is retained
    for latency attribution, never for presentation.

    Raises:
        SidecarProtocolError: the payload is truncated, the format is not what the
            avatar contract requires, or the byte count is not a whole number of
            samples.
    """
    header, pcm = message.audio()
    _require_avatar_format(header)

    if len(pcm) % header.to_format().bytes_per_frame:
        raise SidecarProtocolError(
            f"audio payload of {len(pcm)} bytes is not a whole number of samples for "
            f"{header.to_format()}"
        )

    return AudioFrame(
        pcm=pcm,
        pts_us=clock.now_us(),
        format=header.to_format(),
        ctx=ctx,
        participant=_attribute(header, message.flags, roster),
    )


def _require_avatar_format(header: AudioWireHeader) -> None:
    """Assert the sidecar is sending exactly what the avatar accepts.

    The media platform is configured for ``Pcm16K`` mono, which *is*
    ``AVATAR_INPUT_FORMAT`` — so the ingest path needs no resampler, exactly as on
    Zoom. Doc 002 §3.3 assumed Teams would need one; app-hosted media removed the
    need. Checking it here rather than assuming it is what makes the zero-resample
    property provable: a sidecar that ever sends 48 kHz fails loudly at the boundary
    instead of feeding the avatar audio it cannot use.
    """
    actual = header.to_format()
    if actual != AVATAR_INPUT_FORMAT:
        raise SidecarProtocolError(
            f"sidecar sent {actual} but the avatar contract requires {AVATAR_INPUT_FORMAT}; "
            "check the sidecar's audio socket configuration"
        )


def _attribute(
    header: AudioWireHeader,
    flags: TeamsFlags,
    roster: dict[int, ParticipantRef] | None,
) -> ParticipantRef | None:
    """Resolve which participant a frame came from, if it is attributable.

    ``None`` means "mixed stream" and is a legitimate outcome, not a failure: it is
    what the media platform delivers when unmixed audio is unavailable, and
    ``EchoGuard`` is built to fall back to its speaking gate in exactly that case.

    An unknown source id also yields a bare ``ParticipantRef`` rather than ``None``.
    Rosters and audio race — a speaker can be heard a beat before their roster update
    lands — and dropping the id would make the frame look mixed, disabling the
    own-participant filter for as long as the race lasts.
    """
    if TeamsFlags.UNMIXED not in flags or header.source_msi == MIXED_SOURCE:
        return None
    if roster is not None:
        known = roster.get(header.source_msi)
        if known is not None:
            return known
    return ParticipantRef(user_id=header.source_msi)
