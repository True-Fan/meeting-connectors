"""Meeting and participant models.

Deliberately platform-shaped-but-not-platform-typed: ``meeting_number`` and
``passcode`` are what a join needs on every platform we support, and no DOM, selector
or browser type appears here. ``platform_data`` holds the connector-private payload
(currently a join URL, where the platform offers one) as opaque data — only the owning
connector may interpret it, and it validates it into a typed model at its own boundary.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class MeetingPlatform(StrEnum):
    """The meeting platform a session runs against.

    Added when the second connector arrived: with one, the platform was a constant folded
    into the code (doc 003 §0.1); with more than one it is data. It is a *domain* enum
    rather than a connector concept precisely so that ``api/`` and ``services/`` can
    route on it without importing a connector.

    **Every member is browser-based**, which is not a coincidence but the conclusion two
    removed connectors led to: on all three platforms, the officially supported way to put
    media into somebody else's meeting either does not exist or requires something of that
    meeting's host that a deployment cannot obtain.

    A member carries **identity only** — no urls, no ports, no SDK hints — which is what
    keeps "route on the enum" cheap and "reach for a connector" expensive.
    ``tests/architecture/test_layering.py`` asserts that property, so adding a member is
    the whole of what a new connector needs from the domain.
    """

    GOOGLE_MEET = "google_meet"
    """Google Meet, joined with a browser. Google ships no way to publish media into a
    conference, so a browser is the only way in — see
    ``connectors/google_meet/capabilities.py`` for the evidence."""

    ZOOM_WEB = "zoom_web"
    """Zoom, joined with a browser.

    **There were once two Zoom members.** The other published through a native C++ sidecar
    built against the Meeting SDK and ingested over RTMS; it has been removed, because the
    SDK is a licensed download buildable only on Linux and RTMS requires the meeting to be
    hosted on an account with RTMS enabled for the app — neither of which a deployment can
    arrange for meetings other people book. This one drives Chromium, publishes through a
    virtual microphone and reads the meeting off the page."""

    TEAMS_WEB = "teams_web"
    """Microsoft Teams, joined with a browser as an anonymous guest.

    **There were once two Teams members**, and the gap between them was even wider than
    Zoom's. The removed one needed an Azure AD app with admin-consented
    ``Calls.AccessMedia.All``, a tenant that would grant it, and a **Windows** host running
    the .NET media SDK — three requirements a deployment often cannot satisfy for meetings
    other people booked. This one drives Chromium, joins through the ordinary web client,
    publishes through a synthetic microphone and hears the meeting by tapping the page. It
    needs nothing from the tenant."""


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
    them agree on its shape — each renders a different panel, with different markup, and
    attributes a message differently. What is common is a line of text, who appeared to send
    it, and when we learned of it — so that is what crosses the boundary, and each
    connector's adapter is responsible for reducing its platform to this.

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

    Platform-neutral for the same reason ``ChatMessage`` is: each platform draws a hand
    indicator its own way, in its own markup, in its own panel. What is common is *who* wants
    to speak and *when* we learned of it.

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
    platform_data: dict[str, Any] = field(default_factory=dict)
    platform: MeetingPlatform = MeetingPlatform.ZOOM_WEB
    """Which connector owns this meeting."""
