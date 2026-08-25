"""What Google's official APIs can and cannot do — the premise for the browser bridge.

**Why this is code and not a comment in a design doc.** Every other connector's
transport is self-evidently the right one: Zoom has a Meeting SDK, Teams has app-hosted
media, and nobody asks why they were used. This connector drives a *browser*, which
looks like a shortcut unless you already know that Google ships no server-side way to
put media into a conference. So the finding lives here, as data, with citations — and
``tests/unit/test_google_meet_capabilities.py`` asserts the two ``UNSUPPORTED`` entries
still say ``UNSUPPORTED``. Someone who believes they have found an official publish path
has to delete a test that names the Google sentence contradicting them, which is the
right amount of friction.

Findings are current as of the August 2026 documentation review recorded in
``docs/design/007-google-meet-connector-architecture.md``. They are not consulted at
runtime to make decisions — the architecture already encodes them — so a stale entry
cannot change behaviour, only mislead a reader.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class SupportLevel(StrEnum):
    """How available a Google capability is to a production deployment."""

    GENERALLY_AVAILABLE = "generally_available"
    DEVELOPER_PREVIEW = "developer_preview"
    """Gated behind the Google Workspace Developer Preview Program. Not usable in
    production here for a specific and disqualifying reason: enrolment is required of
    the Cloud project, the OAuth principal, *and every participant in the conference*.
    An external candidate joining an interview breaks the session."""

    UNSUPPORTED = "unsupported"
    """Google publishes no API for this. Not "undocumented" and not "coming" — the
    docs state the negative explicitly."""

    @property
    def is_usable_in_production(self) -> bool:
        return self is SupportLevel.GENERALLY_AVAILABLE


@dataclass(frozen=True, slots=True)
class Capability:
    """One capability this connector needed, and what Google offers for it."""

    name: str
    level: SupportLevel
    api: str
    """The Google product that provides it, or the one that explicitly does not."""
    reference: str
    """A URL on developers.google.com. The claim must be checkable."""
    note: str
    """What this means for the connector — the consequence, not a restatement."""

    @property
    def blocks_avatar(self) -> bool:
        """True when the avatar contract cannot be served through this capability."""
        return not self.level.is_usable_in_production


_MEDIA_API_SEND = (
    "All conference media streams are \"receive-only\". Currently, the Meet Media API "
    "does not support sending of media from MediaApiClientInterface into a conference."
)
"""Verbatim, from the Meet Media API C++ reference. This single sentence is the reason
this connector exists in the shape it does."""

VERBATIM_NO_SEND_MEDIA = _MEDIA_API_SEND


PUBLISH_VIDEO = Capability(
    name="publish custom video into a conference",
    level=SupportLevel.UNSUPPORTED,
    api="none — Meet Media API is receive-only",
    reference=(
        "https://developers.google.com/workspace/meet/media-api/reference/cpp/"
        "namespace/meet"
    ),
    note=(
        f"{_MEDIA_API_SEND} At the protocol level the client offers a=recvonly and Meet "
        "answers a=sendonly, so there is no transceiver direction that could carry our "
        "video. The avatar therefore cannot be a server-side sender; it has to be a "
        "browser publishing through getUserMedia."
    ),
)

PUBLISH_AUDIO = Capability(
    name="publish custom microphone audio into a conference",
    level=SupportLevel.UNSUPPORTED,
    api="none — Meet Media API is receive-only",
    reference=(
        "https://developers.google.com/workspace/meet/media-api/reference/cpp/"
        "namespace/meet"
    ),
    note=(
        "Same sentence, which covers media generically. The client-to-server request "
        "union is session control, video *assignment* (a receive preference), and "
        "stats — no media-send message type exists. Neither the Meet REST API's "
        "SpaceConfig nor the Meet add-ons SDK accepts inbound media either: the add-on "
        "type definitions contain no MediaStream, track, or capture API at all."
    ),
)

MEETING_SDK = Capability(
    name="official server-side SDK that joins and both sends and receives media",
    level=SupportLevel.UNSUPPORTED,
    api="none — there is no Meet equivalent of the Zoom Meeting SDK",
    reference="https://developers.google.com/workspace/meet/overview",
    note=(
        "Google ships exactly three Meet developer surfaces: the add-ons SDK for Web "
        "(GA, an iframe in the Meet UI), the Meet REST API (GA, spaces and post-hoc "
        "conference records), and the Meet Media API (Developer Preview, receive-only). "
        "None of the three is a Meeting SDK. The C++ and TypeScript Media API clients "
        "are labelled by Google as reference clients \"not intended to be a complete "
        "SDK\"."
    ),
)

RECEIVE_AUDIO = Capability(
    name="receive participant audio in real time",
    level=SupportLevel.DEVELOPER_PREVIEW,
    api="Meet Media API (WebRTC; Opus 48 kHz on the wire, exactly 3 virtual streams)",
    reference="https://developers.google.com/workspace/meet/media-api/guides/overview",
    note=(
        "Technically a fit for ingest, and rejected for a non-technical reason: every "
        "participant in the conference must be enrolled in the Developer Preview "
        "Program, only one Media API client may connect per conference, and the meeting "
        "is blocked outright if it is encrypted, watermarked, or has a minor's account "
        "present. Since egress needs a browser regardless, taking audio from the same "
        "browser costs nothing extra and removes every one of those gates."
    ),
)

JOIN_CONFERENCE = Capability(
    name="programmatically join a conference",
    level=SupportLevel.DEVELOPER_PREVIEW,
    api="Meet Media API spaces.connectActiveConference (v2beta)",
    reference="https://developers.google.com/workspace/meet/media-api/guides/overview",
    note=(
        "Requires a human already present in the call to consent, and cannot publish "
        "once connected. A signed-in Chromium instance joins the way a person does, "
        "which is both what the avatar needs and what Meet's own admission controls are "
        "designed to gate."
    ),
)

CONFERENCE_ARTIFACTS = Capability(
    name="post-conference recordings, transcripts, and participant records",
    level=SupportLevel.GENERALLY_AVAILABLE,
    api="Meet REST API v2 + Google Workspace Events over Pub/Sub",
    reference="https://developers.google.com/workspace/meet/api/guides/artifacts",
    note=(
        "GA and genuinely useful, but post-hoc: artifacts land in the organiser's Drive "
        "after the conference ends. Nothing real-time, so nothing the avatar contract "
        "can consume. Recorded here so a future requirement for transcripts is known to "
        "have an official home rather than being bolted onto this connector."
    ),
)


MEET_CAPABILITIES: tuple[Capability, ...] = (
    JOIN_CONFERENCE,
    RECEIVE_AUDIO,
    PUBLISH_AUDIO,
    PUBLISH_VIDEO,
    MEETING_SDK,
    CONFERENCE_ARTIFACTS,
)

OFFICIAL_PUBLISH_CAPABILITIES: tuple[Capability, ...] = (PUBLISH_AUDIO, PUBLISH_VIDEO)
"""The two that forced the browser. Both ``UNSUPPORTED``, both citing the same
sentence."""


def official_media_egress_available() -> bool:
    """True if Google ever ships a server-side way to publish media into a conference.

    Permanently ``False`` today. It exists as a function rather than a constant because
    it names the condition under which this connector should be rewritten: the day this
    returns ``True``, the Chromium bridge becomes optional and a sidecar in the shape of
    ``connectors/teams`` becomes possible.
    """
    return any(c.level.is_usable_in_production for c in OFFICIAL_PUBLISH_CAPABILITIES)


def describe() -> str:
    """A human-readable capability matrix, for logs and design review."""
    rows = [f"{c.name}: {c.level} via {c.api}" for c in MEET_CAPABILITIES]
    return "\n".join(rows)
