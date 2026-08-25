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
from typing import Literal, Self

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

AVATAR_PROTOCOL_VERSION = AvatarProtocolVersion(major=1, minor=2)
"""The protocol version this bridge speaks.

Raised from ``1.0`` to add inbound chat (``AvatarChatMessage``), then to ``1.2`` for
``AvatarMeetingContext``. Both are *additive*: the frames are new, nothing existing changed
shape. That is precisely what the minor version is for, and why the compatibility rule above
only pins the major. A ``1.0`` agent negotiates down, never receives either frame, and behaves
exactly as it did before.

**Raised hands did not raise this again, deliberately.** A dedicated ``interrupt`` frame was
written first and reverted: it required the agent to learn a second frame kind before a raised
hand could do anything at all, and the agent that exists today understands ``chat``. Delivering
the hand as a chat message means the feature works against an unmodified agent — see
``AvatarClient.send_hand_raise``."""

AVATAR_MEETING_CONTEXT_MIN_VERSION = AvatarProtocolVersion(major=1, minor=2)
"""The version that introduced ``AvatarMeetingContext``. Below this it is withheld.

**Withheld rather than delivered as chat, and this is the one place that rule is inverted.**
A raised hand travels on the chat channel precisely *because* everything on that channel is
something the avatar says out loud — "Priya raised their hand" should produce "of course, go
ahead". Attendance must not: an avatar that announces "Aarav Sharma is in the meeting" every
time somebody's wifi hiccups is a worse feature than one that says nothing. So this needs a
frame kind the agent handles silently, which means an agent that does not know the kind must
receive nothing at all rather than be handed something it will read aloud."""

AVATAR_CHAT_MIN_VERSION = AvatarProtocolVersion(major=1, minor=1)
"""The version that introduced chat. Below this, chat is withheld rather than sent and
ignored — silently dropping frames a peer cannot parse is how a protocol becomes
untrustworthy, and an old agent has no way to tell us it did not understand."""

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


class AvatarChatMessage(BaseModel):
    """One chat message from the meeting, sent to the agent as a text frame.

    **Why this is a frame and not synthesised audio.** The obvious alternative — speak the
    chat aloud into the avatar's PCM input — would make the bridge do text-to-speech, and the
    bridge contains no AI by construction (doc 003 §0). It would also be lossy in the way that
    matters: the agent could no longer tell a typed question from a spoken one, and could not
    attribute it to a sender. Text stays text until something whose job is speech decides
    otherwise.

    ``kind`` is a discriminator rather than an implied schema, so later control frames can
    share the same channel without either side guessing.
    """

    model_config = ConfigDict(frozen=True)

    kind: Literal["chat"] = "chat"
    text: str
    sender: str | None = None
    """Display name as the meeting rendered it. Absent when the platform does not attribute."""
    sent_at_us: int = 0
    """Bridge-side receipt time. Not the platform's own timestamp, which Meet does not expose
    to the page in a machine-readable form."""

    def version(self) -> AvatarProtocolVersion:
        """The minimum protocol version an agent must speak to receive this."""
        return AVATAR_CHAT_MIN_VERSION


class AvatarMeetingContext(BaseModel):
    """Standing facts about the meeting, for the agent to know rather than to say.

    **The distinction from ``AvatarChatMessage`` is the entire design.** A chat frame is a turn:
    somebody said something and the avatar is expected to answer. This is not a turn — it is
    background the agent should have *in case* it is asked, and an agent that speaks on receiving
    it is misbehaving. The two cannot share a kind for that reason, and an agent too old to know
    this kind is sent nothing rather than a chat frame it would read aloud.

    Consumed by updating the agent's system/context state, not by appending a user message. In a
    LiveKit agent that is a ``chat_ctx`` update; the shape of that call is the agent's business.

    ``text`` is prose rather than a participant array, because its destination is a context
    window. "Aarav Sharma and Priya Menon are in the meeting; Rahul Verma was invited and never
    joined" is what lets an LLM answer "who's here?", "who left?" and "who's missing?" from one
    string, and it states what is *unknown* too — which a list of names cannot.
    """

    model_config = ConfigDict(frozen=True)

    kind: Literal["meeting_context"] = "meeting_context"
    text: str
    """The brief. Replaces any previous one for this session rather than appending — it is a
    current-state description, so accumulating them would leave the agent holding a history of
    contradictory rosters."""
    topic: str = "attendance"
    """What this brief is about, so later briefs on other subjects can share the kind and the
    agent can replace the right one."""
    observed_at_us: int = 0

    def version(self) -> AvatarProtocolVersion:
        """The minimum protocol version an agent must speak to receive this."""
        return AVATAR_MEETING_CONTEXT_MIN_VERSION


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
