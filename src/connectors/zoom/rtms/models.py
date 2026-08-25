"""RTMS wire models.

**These types must never leave ``connectors/zoom/rtms/``.** They are the shape Zoom
puts on the wire — ``msg_type``, ``rtms_stream_id``, base64 envelopes — and letting
them travel inward would make the whole pipeline speak RTMS. ``mapping.py``
translates them into ``src.domain`` models at the boundary, and
``tests/architecture/test_layering.py`` fails CI if this rule is broken.

Models are permissive on input (``extra="allow"``) because Zoom may add fields, and
a new field must not crash a live session.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from src.connectors.zoom.rtms.enums import (
    PROTOCOL_VERSION,
    AudioChannel,
    AudioCodec,
    AudioSampleRate,
    MediaContentType,
    MediaDataOption,
    MediaDataType,
    RtmsMessageType,
)

_WIRE_CONFIG = ConfigDict(extra="allow", populate_by_name=True)


# --------------------------------------------------------------------------- #
# Outbound: handshakes and control
# --------------------------------------------------------------------------- #


class SignalingHandshakeRequest(BaseModel):
    """``msg_type 1`` — sent on the signaling socket."""

    model_config = _WIRE_CONFIG

    msg_type: int = RtmsMessageType.SIGNALING_HAND_SHAKE_REQ
    protocol_version: int = PROTOCOL_VERSION
    meeting_uuid: str
    rtms_stream_id: str
    signature: str
    sequence: int = 0
    buffer_data: bool = False


class AudioMediaParams(BaseModel):
    """``media_params.audio`` in the data handshake."""

    model_config = _WIRE_CONFIG

    content_type: int = MediaContentType.RAW_AUDIO
    sample_rate: int = AudioSampleRate.SR_16K
    channel: int = AudioChannel.MONO
    codec: int = AudioCodec.L16
    data_opt: int = MediaDataOption.AUDIO_MULTI_STREAMS
    send_rate: int = 20


class TextMediaParams(BaseModel):
    """``media_params.transcript`` / ``media_params.chat`` in the data handshake.

    Both carry text, so both are the same shape — one field, and it is the same value
    for either. Kept as one model rather than two aliases because there is nothing to
    distinguish: what makes a subscription a transcript subscription is the key it is
    filed under and the bit set in ``media_type``, not anything inside it.
    """

    model_config = _WIRE_CONFIG

    content_type: int = MediaContentType.TEXT


class MediaParams(BaseModel):
    """``media_params`` in the data handshake.

    Audio, and optionally the two **text** streams. Video and screen share are still
    deliberately not subscribed — unrequested *media* is pure latency and bandwidth
    cost (doc 003 §3.2), and neither of them is something the avatar can act on.

    Transcript and chat are a different trade and that is why they are here. Both are
    text arriving at human speed, so they cost nothing measurable on this socket, and
    both carry the one thing the audio path structurally cannot: **a name beside the
    words**. A mixed audio stream is one voice as far as the agent is concerned; Zoom's
    transcript says who said each line, and chat says who typed it.

    Optional rather than always-on because a deployment may not have RTMS transcription
    enabled, and ``None`` here means the key is omitted from the handshake entirely —
    ``exclude_none`` in ``DataHandshakeRequest.model_dump`` is what makes the wire look
    exactly as it did before when neither is asked for.
    """

    model_config = _WIRE_CONFIG

    audio: AudioMediaParams
    transcript: TextMediaParams | None = None
    chat: TextMediaParams | None = None


class DataHandshakeRequest(BaseModel):
    """``msg_type 3`` — sent on the media socket."""

    model_config = _WIRE_CONFIG

    msg_type: int = RtmsMessageType.DATA_HAND_SHAKE_REQ
    protocol_version: int = PROTOCOL_VERSION
    meeting_uuid: str
    rtms_stream_id: str
    signature: str
    media_type: int = MediaDataType.AUDIO
    payload_encryption: bool = False
    media_params: MediaParams

    def wire(self) -> dict[str, Any]:
        """The handshake as Zoom should see it, with unrequested streams absent.

        ``model_dump()`` would put ``"transcript": null`` on the wire for a subscription
        that was not asked for. That is not the same message as one without the key, and
        the difference is not ours to gamble on: a rejected data handshake takes the whole
        RTMS connection with it, which is the avatar going deaf. Omitting is what an
        audio-only handshake looked like before this existed, byte for byte.
        """
        return self.model_dump(exclude_none=True)


class EventSubscriptionItem(BaseModel):
    """One entry in ``EVENT_SUBSCRIPTION.events``."""

    model_config = _WIRE_CONFIG

    event_type: int
    subscribe: bool = True


class EventSubscriptionRequest(BaseModel):
    """``msg_type 5`` — ask the signaling socket for participant and speaker events.

    **This is where "who is in the meeting" and "who is talking" come from on Zoom.**
    Neither is scraped from anything: Zoom raises ``PARTICIPANT_JOIN``,
    ``PARTICIPANT_LEAVE`` and ``ACTIVE_SPEAKER_CHANGE`` itself, with the user id and
    display name attached, which is the whole reason this connector needs none of the
    DOM machinery the Google Meet connector had to build.

    Best-effort by design — see ``RtmsService._subscribe_events``. Some accounts deliver
    these events unsolicited, so a subscription that is rejected is not proof they will
    not arrive, and it must never be allowed to fail an attach that has already succeeded.
    """

    model_config = _WIRE_CONFIG

    msg_type: int = RtmsMessageType.EVENT_SUBSCRIPTION
    rtms_stream_id: str
    events: list[EventSubscriptionItem]


class ClientReadyAck(BaseModel):
    """``msg_type 7`` — sent on the signaling socket once media is handshaken."""

    model_config = _WIRE_CONFIG

    msg_type: int = RtmsMessageType.CLIENT_READY_ACK
    rtms_stream_id: str


class KeepAliveResponse(BaseModel):
    """``msg_type 13`` — echoes the server's timestamp verbatim."""

    model_config = _WIRE_CONFIG

    msg_type: int = RtmsMessageType.KEEP_ALIVE_RESP
    timestamp: int


# --------------------------------------------------------------------------- #
# Inbound
# --------------------------------------------------------------------------- #


class MediaServerUrls(BaseModel):
    """Where to connect for each kind of media.

    **A map rather than one url, and that shape is the contract.** Zoom returns a separate
    entry per media type because a data connection carries *one* media type — ``media_type``
    in the handshake is validated as a single enum member, not as a bitmask. Asking one
    socket for audio, transcript and chat together is rejected with status 14, "Media type
    invalid value", and the rejection ends that connection. See
    ``RtmsService._media_handshake`` for what that cost when it was learned the hard way.
    """

    model_config = _WIRE_CONFIG

    all: str | None = None
    audio: str | None = None
    video: str | None = None
    transcript: str | None = None
    chat: str | None = None

    def resolve(self) -> str | None:
        """The URL to use for an audio-only subscription."""
        return self.all or self.audio

    def for_media(self, name: str) -> str | None:
        """The URL for one named stream, falling back to ``all``.

        The named entry wins where Zoom supplied one. ``all`` is the fallback rather than
        an error because it is documented as serving any media type, and a deployment where
        Zoom names only some streams should still be able to subscribe to the rest — the
        connection is per media *type*, not per url, so two connections to ``all`` asking
        for different types is a valid subscription and not a duplicate one.
        """
        named = getattr(self, name, None)
        return (named if isinstance(named, str) and named else None) or self.all


class MediaServer(BaseModel):
    model_config = _WIRE_CONFIG

    server_urls: MediaServerUrls = Field(default_factory=MediaServerUrls)


class SignalingHandshakeResponse(BaseModel):
    """``msg_type 2`` — carries the media socket URL."""

    model_config = _WIRE_CONFIG

    msg_type: int
    status_code: int = 0
    media_server: MediaServer | None = None
    reason: str | None = None

    def media_url(self) -> str | None:
        return self.media_server.server_urls.resolve() if self.media_server else None

    def server_urls(self) -> MediaServerUrls:
        """Every url Zoom offered, so an optional stream can find its own connection."""
        return self.media_server.server_urls if self.media_server else MediaServerUrls()


class DataHandshakeResponse(BaseModel):
    """``msg_type 4``."""

    model_config = _WIRE_CONFIG

    msg_type: int
    status_code: int = 0
    reason: str | None = None


class KeepAliveRequest(BaseModel):
    """``msg_type 12`` — must be answered inside the server's window or Zoom
    drops the connection."""

    model_config = _WIRE_CONFIG

    msg_type: int
    timestamp: int = 0


class AudioContentEnvelope(BaseModel):
    """The per-participant form of ``content``.

    Subscribing ``AUDIO_MULTI_STREAMS`` changes the shape of every audio message:
    Zoom stops sending ``content`` as a bare base64 string and sends an object
    carrying the PCM *and* the speaker it came from. The attribution is the reason
    to ask for multi-stream at all, so it arrives attached to the audio.
    """

    model_config = _WIRE_CONFIG

    data: str = ""
    user_id: int | None = None
    user_name: str | None = None


class MediaDataAudio(BaseModel):
    """``msg_type 14`` — one audio frame.

    ``content`` is base64-encoded PCM in one of two shapes, and **which one depends
    on the subscription**: a bare string for a mixed stream, an
    ``AudioContentEnvelope`` when ``AUDIO_MULTI_STREAMS`` is negotiated. Both are
    described rather than normalised, so the model stays a faithful description of
    the wire; ``audio_base64`` and ``speaker`` are the boundary ``mapping.py`` reads
    through.

    Declaring only the string form is not a cosmetic error. This connector requests
    multi-stream by default, so **every** frame fails validation — and because that
    failure escapes the media pump it takes the whole RTMS connection down with it.
    The observable result is a session that attaches, reports healthy, and hears
    nothing, having negotiated the very option that broke it.
    """

    model_config = _WIRE_CONFIG

    msg_type: int
    content: str | AudioContentEnvelope
    user_id: int | None = None
    user_name: str | None = None
    timestamp: int | None = None

    def audio_base64(self) -> str:
        """The base64 PCM, whichever shape carried it."""
        if isinstance(self.content, AudioContentEnvelope):
            return self.content.data
        return self.content

    def speaker(self) -> tuple[int | None, str | None]:
        """``(user_id, user_name)``, preferring the envelope's attribution.

        Multi-stream puts the speaker inside ``content``; the top-level fields are
        the mixed stream's way of saying the same thing. Falling back keeps one call
        site correct for both.
        """
        if isinstance(self.content, AudioContentEnvelope) and (
            self.content.user_id is not None or self.content.user_name is not None
        ):
            return self.content.user_id, self.content.user_name
        return self.user_id, self.user_name


class TextContentEnvelope(BaseModel):
    """The ``content`` object on a transcript or chat message.

    Same shape for both, and the same shape ``AudioContentEnvelope`` has minus the
    audio: Zoom puts the words in ``data`` and the person who produced them beside it.
    That pairing is the entire value of these two streams — it is the only place in a
    Zoom meeting where a name and what that person said arrive together, exactly as
    Meet's caption panel is on the other connector, and here it arrives over an API
    rather than out of a DOM.
    """

    model_config = _WIRE_CONFIG

    data: str = ""
    user_id: int | None = None
    user_name: str | None = None
    timestamp: int | None = None


class MediaDataText(BaseModel):
    """``msg_type 17`` (transcript) and ``msg_type 18`` (chat) — one line of text.

    One model for both, because the wire shape is identical and only ``msg_type``
    distinguishes them. ``content`` is permissive in the same way ``MediaDataAudio``'s
    is, and for the same reason: Zoom sends a bare string on some streams and an
    envelope on others, and a model that admits only one of them fails validation on
    every message — a failure that, before ``_enqueue_audio`` learned to contain it,
    took the whole connection down.
    """

    model_config = _WIRE_CONFIG

    msg_type: int
    content: str | TextContentEnvelope = ""
    user_id: int | None = None
    user_name: str | None = None
    timestamp: int | None = None

    def text(self) -> str:
        """The words, whichever shape carried them."""
        if isinstance(self.content, TextContentEnvelope):
            return self.content.data
        return self.content

    def speaker(self) -> tuple[int | None, str | None]:
        """``(user_id, user_name)``, preferring the envelope's attribution.

        Mirrors ``MediaDataAudio.speaker`` so one reading of "who is this from" serves
        every stream RTMS carries.
        """
        if isinstance(self.content, TextContentEnvelope) and (
            self.content.user_id is not None or self.content.user_name is not None
        ):
            return self.content.user_id, self.content.user_name
        return self.user_id, self.user_name


class EventUpdate(BaseModel):
    """``msg_type 6`` — participant and speaker events."""

    model_config = _WIRE_CONFIG

    msg_type: int
    event_type: int | None = None
    timestamp: int | None = None
    user_id: int | None = None
    user_name: str | None = None
    event: dict[str, Any] | None = None

    def _body(self) -> dict[str, Any]:
        return self.event if isinstance(self.event, dict) else {}

    def resolved_event_type(self) -> int | None:
        """Which event this is, read from wherever Zoom put it.

        **Inside ``event``, and reading only the top level made every one of these
        undecodable.** Captured from a live meeting::

            {'msg_type': 6, 'event': {'event_type': 3, 'participants': [...]}}
            {'msg_type': 6, 'event': {'event_type': 2, 'user_id': …, 'user_name': …}}

        There is no ``event_type`` at the top level at all. The declared top-level field
        is kept as a fallback rather than removed, because it costs nothing and this
        model has no way to know which shape a given account or Zoom version will send —
        which is exactly the assumption that produced the bug.
        """
        body = self._body()
        raw = body.get("event_type", body.get("eventType"))
        return raw if isinstance(raw, int) else self.event_type

    def participant(self) -> tuple[int | None, str | None]:
        """``(user_id, user_name)`` for whoever this event is about.

        The nested ``event`` object wins, which is where ``ACTIVE_SPEAKER_CHANGE`` puts
        it. The top level is the fallback, for the reason ``resolved_event_type`` keeps
        one.
        """
        nested = self._body()
        raw_id = nested.get("user_id", nested.get("userId"))
        user_id = raw_id if isinstance(raw_id, int) else self.user_id
        raw_name = nested.get("user_name", nested.get("userName"))
        user_name = raw_name if isinstance(raw_name, str) and raw_name else self.user_name
        return user_id, user_name

    def participants(self) -> tuple[tuple[int | None, str | None], ...]:
        """Everybody this event is about, which for a join or leave may be several.

        **Zoom sends ``PARTICIPANT_JOIN`` with a ``participants`` array**, not with one
        user on it — and the first one after attaching carries the *whole roster*::

            {'event_type': 3, 'participants': [{'user_id': …, 'user_name': 'AI Avatar'},
                                               {'user_id': …, 'user_name': 'Dev Choudhary'}]}

        That is a better starting point than waiting for individual joins, because RTMS
        attaches after the meeting has already begun and the joins that happened before
        it were never going to arrive. Reading only a single ``user_id`` here meant an
        attendance ledger that stayed permanently empty.

        Falls back to the single-participant reading, so an event that carries one person
        the flat way still yields that person.
        """
        raw = self._body().get("participants")
        if isinstance(raw, list):
            people: list[tuple[int | None, str | None]] = []
            for entry in raw:
                if not isinstance(entry, dict):
                    continue
                user_id = entry.get("user_id", entry.get("userId"))
                user_name = entry.get("user_name", entry.get("userName"))
                people.append(
                    (
                        user_id if isinstance(user_id, int) else None,
                        user_name if isinstance(user_name, str) and user_name else None,
                    )
                )
            if people:
                return tuple(people)

        single = self.participant()
        return (single,) if single[0] is not None or single[1] else ()


class StreamStateUpdate(BaseModel):
    """``msg_type 8`` / ``9`` — stream or session state changed."""

    model_config = _WIRE_CONFIG

    msg_type: int
    state: int | None = None
    reason: str | None = None
    rtms_stream_id: str | None = None


# --------------------------------------------------------------------------- #
# Webhooks
# --------------------------------------------------------------------------- #


class UrlValidationPayload(BaseModel):
    model_config = _WIRE_CONFIG

    plain_token: str = Field(alias="plainToken")


class UrlValidationEvent(BaseModel):
    """``endpoint.url_validation`` — Zoom's endpoint challenge."""

    model_config = _WIRE_CONFIG

    event: str
    payload: UrlValidationPayload


class RtmsStartedPayload(BaseModel):
    model_config = _WIRE_CONFIG

    meeting_uuid: str
    rtms_stream_id: str
    server_urls: str | list[str] | dict[str, Any]
    operator_id: str | None = None

    def signaling_url(self) -> str:
        """Normalise ``server_urls``, which Zoom may send as a string, list or map."""
        raw = self.server_urls
        if isinstance(raw, str):
            return raw
        if isinstance(raw, list):
            if not raw:
                raise ValueError("server_urls list is empty")
            return str(raw[0])
        for key in ("all", "signaling", "signalling"):
            if raw.get(key):
                return str(raw[key])
        first = next((v for v in raw.values() if v), None)
        if first is None:
            raise ValueError(f"no usable url in server_urls: {raw!r}")
        return str(first)


class RtmsStoppedPayload(BaseModel):
    model_config = _WIRE_CONFIG

    meeting_uuid: str
    rtms_stream_id: str | None = None
    stop_reason: int | str | None = None


class RtmsStartedEvent(BaseModel):
    """``meeting.rtms_started``."""

    model_config = _WIRE_CONFIG

    event: str
    event_ts: int | None = None
    payload: RtmsStartedPayload


class RtmsStoppedEvent(BaseModel):
    """``meeting.rtms_stopped``."""

    model_config = _WIRE_CONFIG

    event: str
    event_ts: int | None = None
    payload: RtmsStoppedPayload
