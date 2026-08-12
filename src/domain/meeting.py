"""Meeting and participant models.

Deliberately platform-shaped-but-not-platform-typed: ``meeting_number`` and
``passcode`` are what a join needs on every platform we support, and no RTMS, Graph,
or SDK type appears here. ``platform_data`` holds the connector-private payload (Zoom:
RTMS stream id and server urls; Teams: the resolved Graph join descriptor) as opaque
data — only the owning connector may interpret it, and it validates it into a typed
model at its own boundary.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class MeetingPlatform(StrEnum):
    """The meeting platform a session runs against.

    Added when Teams arrived: with one connector the platform was a constant folded
    into the code (doc 003 §0.1), and with two it is data. It is a *domain* enum
    rather than a connector concept precisely so that ``api/`` and ``services/`` can
    route on it without importing a connector.

    A member carries **identity only** — no urls, no ports, no SDK hints — which is what
    keeps "route on the enum" cheap and "reach for a connector" expensive.
    ``tests/architecture/test_layering.py`` asserts that property, so adding a member is
    the whole of what a new connector needs from the domain.
    """

    ZOOM = "zoom"
    TEAMS = "teams"
    GOOGLE_MEET = "google_meet"


@dataclass(frozen=True, slots=True)
class ParticipantRef:
    """A reference to a meeting participant.

    ``user_id`` is the platform's numeric participant identifier. It is the key
    ``EchoGuard`` uses to recognise the avatar's own audio arriving back through
    ingest (doc 003 §5.3).
    """

    user_id: int
    display_name: str | None = None


@dataclass(frozen=True, slots=True)
class ChatMessage:
    """One text message from a meeting's chat.

    Platform-neutral on purpose: every platform this service targets has a chat, and none of
    them agree on its shape. Meet renders a DOM list with display names, Zoom delivers chat
    over its own event channel, Teams over Graph. What is common is a line of text, who
    appeared to send it, and when we learned of it — so that is what crosses the boundary,
    and each connector's adapter is responsible for reducing its platform to this.

    ``sender`` is the display name rather than an id because that is what a conversational
    agent can use: "Priya asks…" is meaningful to an LLM in a way a participant id is not.
    Attribution is optional because a platform may not offer it, and a message with unknown
    provenance is still worth answering.
    """

    text: str
    sender: str | None = None
    received_at_us: int = 0
    is_self: bool = False
    """True when the avatar's own account sent it. Never forwarded to the agent — an avatar
    reacting to its own chat message is the text-channel version of an echo loop."""


@dataclass(frozen=True, slots=True)
class HandRaise:
    """One participant asking for the floor.

    Platform-neutral for the same reason ``ChatMessage`` is: Meet renders a hand indicator in
    the DOM, Zoom raises an SDK event, Teams reports it over Graph, and none of them agree on
    the shape. What is common is *who* wants to speak and *when* we learned of it.

    **Why this is not a ``ChatMessage`` with different text.** A chat message is a question
    waiting its turn; a raised hand is a request to take the turn *now*, and the two produce
    opposite behaviour in the router — one is forwarded and answered when the avatar next
    pauses, the other interrupts whatever the avatar is saying. Modelling them as one type
    would mean a boolean on ``ChatMessage`` that every consumer has to branch on, which is the
    same thing with the distinction hidden.

    ``prompt`` is what the agent is told, rendered by the connector rather than built here,
    because the wording is deployment policy — see ``GoogleMeetSettings.hand_raise_prompt``.
    """

    participant: str | None = None
    """Display name of whoever raised their hand, when the platform attributes it."""

    prompt: str = ""
    """What the agent receives. Empty is not forwarded — see ``AvatarClient.send_interrupt``."""

    raised_at_us: int = 0
    """Bridge-side receipt time, on the session's media clock."""

    is_self: bool = False
    """True when the avatar's own account raised it. Never forwarded: an avatar interrupting
    itself is the barge-in version of an echo loop."""


@dataclass(frozen=True, slots=True)
class MeetingContext:
    """Everything needed to attach to one meeting."""

    meeting_number: str
    display_name: str
    passcode: str | None = None
    meeting_uuid: str | None = None
    platform_data: dict[str, Any] = field(default_factory=dict)
    platform: MeetingPlatform = MeetingPlatform.ZOOM
    """Which connector owns this meeting. Defaults to ``ZOOM`` so every existing
    construction site — and every Zoom code path — behaves exactly as before."""

    def with_uuid(self, meeting_uuid: str) -> MeetingContext:
        """Return a copy carrying the meeting UUID learned from a webhook.

        The UUID is not known when an operator creates a session by meeting number;
        it arrives with ``meeting.rtms_started``. It is the key used to bind an
        inbound RTMS stream to an existing session (doc 003 §3.1).
        """
        return MeetingContext(
            meeting_number=self.meeting_number,
            display_name=self.display_name,
            passcode=self.passcode,
            meeting_uuid=meeting_uuid,
            platform_data=dict(self.platform_data),
            platform=self.platform,
        )
