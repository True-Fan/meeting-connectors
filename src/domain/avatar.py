"""The avatar agent contract.

The avatar service is already implemented and its interface is fixed:

* **input** — PCM, 16 kHz, mono
* **output** — a continuously streamed fragmented MP4

Because it never changes, it is expressed here as domain constants rather than
configuration. That makes it an architectural invariant: the ingest path asserts
equality against ``AVATAR_INPUT_FORMAT`` instead of resampling, so Zoom RTMS's
native ``L16 / SR_16K / MONO`` being an exact match is a *checked* property rather
than an incidental convenience (doc 003 §1.2).

The handshake models live here too — they are part of the fixed contract, not a
transport detail. ``avatar/ws_transport.py`` (M3) sends them; this module owns
their shape and the compatibility rule.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Self

from pydantic import BaseModel, ConfigDict, Field

from src.domain.exceptions import AvatarProtocolMismatchError
from src.domain.ids import CorrelationId, SessionId
from src.domain.media import AudioFormat, ContainerFormat, SampleFormat

_VERSION_RE = re.compile(r"^(\d+)\.(\d+)$")


@dataclass(frozen=True, slots=True, order=True)
class AvatarProtocolVersion:
    """Semantic version of the bridge↔avatar wire protocol.

    Compatibility rule: **the major version must match exactly.** Minor versions
    are additive, so either side may run ahead of the other on minor and still
    interoperate — a newer peer must ignore fields it does not recognise.
    """

    major: int
    minor: int

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}"

    @classmethod
    def parse(cls, raw: str) -> Self:
        match = _VERSION_RE.match(raw.strip())
        if match is None:
            raise ValueError(f"malformed avatar protocol version: {raw!r}")
        return cls(major=int(match.group(1)), minor=int(match.group(2)))

    def is_compatible_with(self, other: AvatarProtocolVersion) -> bool:
        """True when both sides can interoperate."""
        return self.major == other.major


# ---------------------------------------------------------------------------
# Fixed contract constants
# ---------------------------------------------------------------------------

AVATAR_PROTOCOL_VERSION = AvatarProtocolVersion(major=1, minor=0)
"""The protocol version this bridge speaks."""

AVATAR_INPUT_FORMAT = AudioFormat(
    sample_rate_hz=16_000,
    channels=1,
    sample_format=SampleFormat.S16LE,
)
"""What the avatar agent accepts. Never changes."""

AVATAR_OUTPUT_CONTAINER = ContainerFormat.FRAGMENTED_MP4
"""What the avatar agent streams back. Never changes."""


# ---------------------------------------------------------------------------
# Handshake wire models
# ---------------------------------------------------------------------------


class AvatarAudioSpec(BaseModel):
    """Audio format declaration exchanged during the handshake."""

    model_config = ConfigDict(frozen=True)

    sample_rate_hz: int
    channels: int
    sample_format: SampleFormat

    @classmethod
    def from_domain(cls, fmt: AudioFormat) -> AvatarAudioSpec:
        return cls(
            sample_rate_hz=fmt.sample_rate_hz,
            channels=fmt.channels,
            sample_format=fmt.sample_format,
        )

    def to_domain(self) -> AudioFormat:
        return AudioFormat(
            sample_rate_hz=self.sample_rate_hz,
            channels=self.channels,
            sample_format=self.sample_format,
        )


class AvatarClientHello(BaseModel):
    """First message the bridge sends after the WebSocket opens.

    Carries the session and correlation ids so the avatar agent's own logs can be
    correlated with the bridge's for the same conversation.
    """

    model_config = ConfigDict(frozen=True)

    protocol_version: str = Field(default_factory=lambda: str(AVATAR_PROTOCOL_VERSION))
    session_id: SessionId
    correlation_id: CorrelationId
    audio: AvatarAudioSpec = Field(
        default_factory=lambda: AvatarAudioSpec.from_domain(AVATAR_INPUT_FORMAT)
    )
    expects_container: ContainerFormat = AVATAR_OUTPUT_CONTAINER

    def version(self) -> AvatarProtocolVersion:
        return AvatarProtocolVersion.parse(self.protocol_version)


class AvatarServerHello(BaseModel):
    """The avatar agent's handshake reply."""

    model_config = ConfigDict(frozen=True)

    protocol_version: str
    accepted: bool = True
    reason: str | None = None
    container: ContainerFormat = AVATAR_OUTPUT_CONTAINER

    def version(self) -> AvatarProtocolVersion:
        return AvatarProtocolVersion.parse(self.protocol_version)


def check_handshake(client: AvatarClientHello, server: AvatarServerHello) -> AvatarProtocolVersion:
    """Validate a completed handshake and return the negotiated version.

    Raises:
        AvatarProtocolMismatchError: the agent rejected us, speaks an
            incompatible major version, or offers a container we cannot decode.
    """
    ours = client.version()
    theirs = server.version()

    if not server.accepted:
        raise AvatarProtocolMismatchError(str(ours), server.reason or str(theirs))

    if not ours.is_compatible_with(theirs):
        raise AvatarProtocolMismatchError(str(ours), str(theirs))

    if server.container is not AVATAR_OUTPUT_CONTAINER:
        raise AvatarProtocolMismatchError(
            f"{ours} expecting {AVATAR_OUTPUT_CONTAINER}",
            f"{theirs} offering {server.container}",
        )

    # Negotiated version is the lower minor of the two — neither side may assume
    # a feature the other has not shipped.
    return min(ours, theirs)
