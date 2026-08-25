"""Graph join wire models. **These never leave this package.**

The anti-corruption boundary for Teams, mirroring what ``connectors/zoom/rtms/models.py``
is for RTMS. Graph vocabulary — ``chatInfo``, ``threadId``, ``organizerId``,
``joinMeetingIdSettings`` — stops here and at ``join_url.py``; everything inward speaks
``domain.MeetingContext`` and ``domain.AudioFrame``. Enforced by
``tests/architecture/test_layering.py``.

Field names are ``camelCase`` on the wire because that is what Microsoft Graph and the
sidecar's ``CONTROL_JOIN`` payload use. The aliases keep Python callers snake_case
without a translation step in between.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class JoinMode(StrEnum):
    """How the sidecar should ask Graph to join the meeting.

    Graph offers two routes and they need different payloads. Which one is available
    depends on what the operator supplied, so the mode is resolved once, here, rather
    than being re-derived on the Windows side.
    """

    MEETING_ID = "meeting_id"
    """``joinMeetingIdMeetingInfo`` — the numeric "Meeting ID" and passcode printed in
    a Teams invite. The route that maps onto the bridge's existing
    ``meeting_number`` + ``passcode`` fields, so an operator drives Teams and Zoom
    through the identical request shape."""

    CHAT_INFO = "chat_info"
    """``chatInfo`` + ``organizerMeetingInfo``, extracted from a
    ``teams.microsoft.com/l/meetup-join/...`` URL. Needed when only the join link is
    to hand, which is the common case for a calendar invite."""


class ChatInfo(BaseModel):
    """Graph ``chatInfo`` — identifies the meeting's conversation."""

    model_config = ConfigDict(frozen=True, populate_by_name=True)

    thread_id: str = Field(alias="threadId")
    message_id: str = Field(default="0", alias="messageId")
    """``"0"`` is Graph's documented sentinel for "the meeting itself" rather than a
    specific message in the thread."""
    reply_chain_message_id: str | None = Field(default=None, alias="replyChainMessageId")


class OrganizerIdentity(BaseModel):
    """Graph ``organizerMeetingInfo.organizer`` — an AAD user reference."""

    model_config = ConfigDict(frozen=True, populate_by_name=True)

    id: str
    tenant_id: str = Field(alias="tenantId")


class TeamsJoinDescriptor(BaseModel):
    """Everything the sidecar needs to create the Graph call.

    Produced by ``join_url.resolve_join_descriptor`` from a ``MeetingContext`` and
    serialised straight into the ``CONTROL_JOIN`` payload (doc 005 §5.2). It is the
    single point where "which meeting" is decided, which is why the validator below
    refuses a half-populated instance rather than letting the Windows side discover
    the gap.
    """

    model_config = ConfigDict(frozen=True, populate_by_name=True)

    mode: JoinMode
    tenant_id: str = Field(alias="tenantId")
    display_name: str = Field(alias="displayName")

    # MEETING_ID route
    join_meeting_id: str | None = Field(default=None, alias="joinMeetingId")
    passcode: str | None = None

    # CHAT_INFO route
    chat_info: ChatInfo | None = Field(default=None, alias="chatInfo")
    organizer: OrganizerIdentity | None = None

    @model_validator(mode="after")
    def _require_route_fields(self) -> TeamsJoinDescriptor:
        if self.mode is JoinMode.MEETING_ID:
            if not self.join_meeting_id:
                raise ValueError("mode=meeting_id requires joinMeetingId")
        elif self.chat_info is None or self.organizer is None:
            raise ValueError("mode=chat_info requires both chatInfo and organizer")
        return self

    def to_wire(self) -> dict[str, Any]:
        """The ``CONTROL_JOIN`` sub-object, camelCase and without empty keys.

        ``exclude_none`` matters: the sidecar switches on key *presence* for the
        optional passcode, and an explicit ``null`` would be sent to Graph as an
        empty passcode rather than as "no passcode".
        """
        return self.model_dump(by_alias=True, exclude_none=True)


class ParticipantInfo(BaseModel):
    """One roster entry as reported by the sidecar.

    Translated to ``domain.ParticipantRef`` in ``ingest/mapping.py``; this shape stays
    behind the boundary because ``msi`` is a media-platform concept.
    """

    model_config = ConfigDict(frozen=True, populate_by_name=True)

    msi: int
    """Media Source Id — the media platform's per-stream speaker identifier, and the
    key that unmixed audio buffers are tagged with."""
    display_name: str | None = Field(default=None, alias="displayName")
    aad_object_id: str | None = Field(default=None, alias="aadObjectId")
    is_self: bool = Field(default=False, alias="isSelf")
    """True for the bot's own entry. This is how ``EchoGuard`` learns the identity to
    filter — the Teams equivalent of the Zoom publisher reporting its user id."""


class SidecarReady(BaseModel):
    """The sidecar's ``READY`` payload: the call is up and media is negotiated."""

    model_config = ConfigDict(frozen=True, populate_by_name=True)

    call_id: str = Field(alias="callId")
    wire_version: int = Field(alias="wireVersion")
    audio_sample_rate_hz: int = Field(alias="audioSampleRateHz")
    audio_channels: int = Field(default=1, alias="audioChannels")
    unmixed_audio: bool = Field(default=False, alias="unmixedAudio")
    video_width: int = Field(alias="videoWidth")
    video_height: int = Field(alias="videoHeight")
    video_fps: int = Field(alias="videoFps")
    self_msi: int | None = Field(default=None, alias="selfMsi")
    sdk_version: str | None = Field(default=None, alias="sdkVersion")


class SidecarError(BaseModel):
    """The sidecar's ``ERROR`` payload."""

    model_config = ConfigDict(frozen=True, populate_by_name=True)

    code: str = "UNKNOWN"
    message: str = ""
    fatal: bool = False
