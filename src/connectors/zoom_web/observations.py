"""What the page observes about a meeting, once the DOM has been stripped off.

**Why these exist as their own types rather than as domain models.** ``domain/meeting.py``
holds what every connector must agree on — a meeting, a participant reference, a chat
message, a request for the floor. These three are narrower: they are *observations this
connector happens to make*, and they exist so the ledgers below them can be written against
plain values instead of against ``dict`` payloads lifted out of a WebSocket frame.

**Why they are not the page wire format either.** ``page/protocol.py`` is the wire —
magic bytes, kinds, JSON envelopes — and keeping it inside this connector is what stops
every ledger from reading ``event.get("type")``. Something has to cross from the page server
to the code that keeps a ledger, and these are that something: plain values, no encoding, no
protocol, nothing that has to be decoded twice.

**Why they are a copy of ``connectors/teams_web/observations`` rather than an import of it.**
Because a connector that imports another connector's types is coupled to its release cycle,
and because the docstrings would then be lying: the fields are the same three values and the
reasons they hold what they hold are not. This module previously *was* such an import — it
read its types from the Meeting-SDK Zoom connector's RTMS package — and when that connector
was removed the types came here, which is where a connector's own observations belong.

Every consumer of these is **synchronous and total**, and that is a hard requirement rather
than a style. They are called from the page server's read loop — the loop that also carries
the avatar's voice *into* the page — so a method that blocks makes the avatar stutter and a
method that raises drops the socket.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ParticipantEvent:
    """Somebody joined or left the meeting, derived from a change in the page's roster.

    ``user_id`` is always ``None`` on this connector and the field is kept anyway, because
    the ledgers that consume it already handle both cases and a type that omitted it would
    have to be widened the day a Zoom build starts exposing one. Its absence is a real loss
    and worth naming: an id identifies a *presence*, which is what lets two people sharing a
    display name be told apart. Here they cannot be, so
    ``connectors/zoom_web/meeting/attendance.py`` keys on the name instead.

    **Edges, not a level.** The page reports the list it can see; the observer diffs that
    against what it saw last and produces these. See ``meeting/observer.py`` for why the
    diff belongs in Python rather than in the page.
    """

    user_id: int | None
    display_name: str | None
    joined: bool
    at_us: int = 0
    """Monotonic media-clock time the event was derived. Not a timestamp from the page,
    which runs on the browser's own timeline."""


@dataclass(frozen=True, slots=True)
class SpeakerEvent:
    """The page believes this participant now holds the floor.

    **A level, not an edge, and the difference matters to every consumer.** It says who is
    talking now and never says that anybody stopped, so a tracker built on it has to close
    the previous turn itself when the floor moves — which is what ``ZoomSpeakerTracker``
    does.

    Zoom draws this rather than reporting it — a highlighted tile and a matching indicator
    on the roster row — so it lags and it flickers. The page holds a candidate for
    ``speaker_min_ms`` before reporting it, and the tracker's hold and merge windows absorb
    the rest.

    ``display_name`` may be absent on an event the page could not attribute. That is not a
    failure: the roster resolves it, retroactively if need be.
    """

    user_id: int | None
    display_name: str | None
    at_us: int = 0


@dataclass(frozen=True, slots=True)
class TranscriptLine:
    """One line of Zoom's live captions, with the speaker beside it.

    **This is the answer to "what did they ask you?", and nothing else on this connector
    could be.** The avatar's own transcription lives upstream in the agent, which receives
    one mixed stream and therefore knows the words without knowing whose they are; the
    speaker observer knows who is talking without knowing the words. Zoom's captions are the
    only place in the meeting where a name and the words that person said arrive together.

    Approximate wording, exact-ish attribution — worth restating wherever this is consumed,
    so the agent does not quote it as verbatim fact.
    """

    user_id: int | None
    display_name: str | None
    text: str
    at_us: int = 0
