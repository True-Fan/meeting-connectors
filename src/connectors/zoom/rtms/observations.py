"""What RTMS observes about a meeting, once the wire vocabulary has been stripped off.

**Why these exist as their own types rather than as domain models.** ``domain/meeting.py``
holds what every connector must agree on — a meeting, a participant reference, a chat
message, a request for the floor. These four are narrower than that: they are *observations
one platform happens to make*, and only Zoom makes them this way, because only Zoom hands
them over as events with a name attached. Putting them in the domain would oblige Teams and
Meet to have an opinion about a shape neither produces, which is the same mistake doc 003 §0
names about protocols: a type earns its place in shared code when a second producer exists.

**Why they are not the RTMS models either.** ``models.py`` is the wire — ``msg_type``,
``content`` envelopes, base64 — and ``tests/architecture/test_layering.py`` keeps it inside
the Zoom connector for exactly that reason. Something has to cross from ``RtmsService`` to
the code that keeps a ledger, and if that something were a wire model then every ledger
would be reading ``msg_type``. So these are the translated form: plain values, no encoding,
no protocol, nothing that has to be decoded twice.

The observer is a **synchronous, total** interface, and that is a hard requirement rather
than a style. Every method here is called from the RTMS media or signaling pump — the loop
that also carries the meeting's audio — so a method that blocks stalls ingest and a method
that raises tears the connection down. The same rule the Google Meet connector writes its
``offer``/``observe_roster`` methods to, arrived at from the same place.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from src.domain.meeting import ChatMessage


@dataclass(frozen=True, slots=True)
class ParticipantEvent:
    """Somebody joined or left the meeting.

    ``user_id`` is Zoom's own participant id, which is stable for one person's stay and
    minted afresh when they rejoin — so it identifies a *presence*, not a person. Anything
    accumulating history across a reconnect has to key on the name instead; see
    ``connectors/zoom_web/meeting/attendance.py``, which does.
    """

    user_id: int | None
    display_name: str | None
    joined: bool
    at_us: int = 0
    """Monotonic media-clock time the event was received. Not Zoom's ``timestamp``, which
    is on an unrelated timeline — the same reason ``to_audio_frame`` refuses to use it as a
    presentation timestamp."""


@dataclass(frozen=True, slots=True)
class SpeakerEvent:
    """Zoom says this participant now holds the floor.

    **A level, not an edge, and the difference matters to every consumer.** Zoom raises
    ``ACTIVE_SPEAKER_CHANGE`` when the active speaker *becomes* somebody else; it does not
    raise a matching "stopped". So this says who is talking now and never says that anybody
    stopped, and a tracker built on it has to close the previous turn itself when the floor
    moves — which is what ``ZoomSpeakerTracker`` does.

    ``display_name`` may be absent on an event that only carried an id. That is not a
    failure: the roster resolves it, retroactively if need be.
    """

    user_id: int | None
    display_name: str | None
    at_us: int = 0


@dataclass(frozen=True, slots=True)
class TranscriptLine:
    """One line of Zoom's own live transcription, with the speaker beside it.

    **This is the answer to "what did they ask you?", and nothing else could be.** The
    avatar's transcription lives upstream in the agent, which receives one mixed stream and
    therefore knows the words without knowing whose they are. Zoom transcribes per
    participant, so its transcript is the only place in the meeting where a name and the
    words that person said arrive together.

    Approximate wording, exact attribution — the same trade Meet's captions make, and worth
    stating wherever this is consumed so the agent does not quote it as verbatim fact.
    """

    user_id: int | None
    display_name: str | None
    text: str
    at_us: int = 0


@runtime_checkable
class MeetingObserver(Protocol):
    """Where ``RtmsService`` sends everything that is not audio.

    Four methods, all synchronous, all obliged to return rather than raise. See the module
    docstring for why that is a requirement of the media pump and not a preference.

    Optional to the service: ``RtmsService`` takes ``observer=None`` and then behaves exactly
    as it did before this interface existed — which is what keeps the SDK-based Zoom
    connector, that wants none of this, byte-for-byte unchanged.
    """

    def on_participant(self, event: ParticipantEvent) -> None:
        """Somebody joined or left."""
        ...

    def on_speaker(self, event: SpeakerEvent) -> None:
        """The floor changed hands."""
        ...

    def on_transcript(self, line: TranscriptLine) -> None:
        """Zoom transcribed a line of speech."""
        ...

    def on_chat(self, message: ChatMessage) -> None:
        """Somebody typed in the meeting chat."""
        ...
