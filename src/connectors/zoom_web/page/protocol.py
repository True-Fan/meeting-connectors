"""The page channel's two wire formats — one per direction, because they carry opposite
kinds of thing.

**Bridge → page: binary.** One message type, fixed header, raw PCM payload::

    magic    4s   b'ZWB1'
    version  B    1
    kind     B    1 = audio pcm
    reserved H    0
    pts_us   Q    presentation timestamp
    length   I    PCM byte count

Binary rather than JSON because this carries 50 frames a second; base64 in an
envelope would cost a third more bytes and a parse per frame, for readability nobody
benefits from — nothing here is read by a human.

**Page → bridge: two formats, one per kind.**

*Binary* is the meeting's audio, framed with the same header as the outbound direction and
a different ``kind``. It exists for ``ingest_mode="browser"``, where the page's own audio
tap replaces RTMS as the ingest leg — see ``js/inject.js`` and ``ingest/page_audio_source.py``.
It carries 50 frames a second, which is the same argument the outbound direction makes for
not being JSON.

*Text* is everything the page **observes**: a raised hand, and — again only in browser
ingest mode — the roster, the active speaker, the chat and the captions. A few messages a
second at most, so the per-message cost is irrelevant and being readable in a log is worth
a great deal: every one of these depends on selectors matching a UI Zoom is free to change,
and the first question when one stops working is always *what did the page actually see*.

The two are told apart by WebSocket frame type rather than by a discriminator — binary is
audio, text is an event — which is a property the transport already guarantees, so nothing
has to be parsed to route it.

**Why this grew.** It was, deliberately, the smallest page codec in the repository: RTMS
supplied everything except a raised hand, so the page was asked for exactly one signal.
Browser ingest removes RTMS from the picture entirely — the point of it is that the meeting
need not be hosted on an RTMS-enabled account — and every signal RTMS used to carry has to
come from somewhere. The browser is the only thing left that can see them. So this codec
converges on the Google Meet one, because in that mode the two connectors have the same
problem.

Both modes share this file. An RTMS deployment simply never sees the events below
``EVENT_HAND_RAISE``: the page does not emit them unless its config switches them on.
"""

from __future__ import annotations

import json
import struct
from typing import Any

MAGIC = b"ZWB1"
VERSION = 1
KIND_AUDIO_PCM = 1
"""Bridge → page: the avatar's voice, for the synthetic microphone."""

KIND_AUDIO_CAPTURE = 2
"""Page → bridge: the meeting's audio, tapped out of Zoom's own playout graph.

A separate kind rather than reusing ``KIND_AUDIO_PCM``, even though the direction already
disambiguates them. The two are different things that happen to share a header, and a frame
logged or captured in isolation should say which it is — the alternative is a debugging
session spent working out which way a hexdump was travelling."""

_HEADER = struct.Struct("!4sBBHQI")
HEADER_SIZE = _HEADER.size

MAX_EVENT_BYTES = 64 * 1024
"""Ceiling on one page→bridge event.

A guard against a page that has gone wrong rather than a product limit: the largest
legitimate message here is a hand raise carrying a display name. Anything approaching this
is a malfunction, and refusing it keeps a bad script from making the bridge allocate."""

EVENT_HAND_RAISE = "handRaise"
"""A participant's hand just went up. ``{type, id, name, isSelf}``.

**Edge-triggered, and the page owns the edge.** Zoom renders a raised hand as a *state* —
an indicator that stays until it is lowered — and re-renders it constantly. What the avatar
has to react to is the moment it goes up, so the page holds the current set and reports
transitions; a level signal would arrive many times per raised hand and Python would have to
reconstruct the edge anyway. The same contract Meet's ``HAND_RAISE`` message carries, for
the same reason.

The page decides nothing about whether to interrupt. It reports who, and Python decides —
see ``connectors/zoom_web/meeting/hand_raise.py``."""

EVENT_PAGE = "pageEvent"
"""Diagnostics: what the observer armed with, what it can see, whether it found anything.

Carried because this is the one feature in the connector whose failure mode is *silence* —
a selector that stopped matching produces exactly the same observable behaviour as a meeting
where nobody raised a hand. These messages are what tell the two apart."""

# -- browser-ingest events -------------------------------------------------
#
# Emitted only when the page is configured for browser ingest. Each replaces an RTMS
# stream, and each is deliberately shaped like the RTMS observation it stands in for
# (``connectors/zoom/rtms/observations.py``) so that the ledgers consuming them did not
# have to change: ``ZoomAttendanceLedger``, ``ZoomSpeakerTracker``, ``ZoomTranscript`` and
# ``ZoomChatSource`` were already written against those types rather than against RTMS.
# That is what makes this a new *source* rather than a second implementation of the
# connector.

EVENT_ROSTER = "roster"
"""Who the page can currently see in the meeting. ``{type, names: [...]}``.

**A level, not an edge, and that is the opposite of every other event here.** RTMS reports
joins and leaves; a DOM reports a list. Deriving the edges in the page would mean the page
holding the authoritative roster, and a page reload would then re-announce the whole meeting
as having just joined. So the page reports what it sees and ``ZoomMeetingObserver`` diffs it
against the ledger, which survives the page and is where "who was here" already lives."""

EVENT_SPEAKER = "speaker"
"""The page believes this participant now holds the floor. ``{type, name, isSelf}``.

Zoom marks the active speaker in the DOM — a highlighted tile, an animated indicator in the
participants panel. That is a poorer signal than ``ACTIVE_SPEAKER_CHANGE``: it is a rendering
rather than an event, so it can lag and it can flicker. ``ZoomSpeakerTracker``'s hold and
merge windows already exist to absorb exactly that, because Meet's equivalent has the same
property."""

EVENT_CAPTION = "caption"
"""One line of Zoom's live transcription, as rendered. ``{type, name, text, final}``.

**Requires captions to be switched on in the meeting**, which is a visible action and hence
a setting (``MC_ZOOM_WEB__CAPTIONS_AUTO_ENABLE``). Where RTMS transcription is an invisible
subscription, this is the avatar pressing a button everybody can see — the same trade the
Google Meet connector makes, and the reason it is off by default here for anyone who still
has RTMS.

``final`` distinguishes a caption still being revised from one Zoom has settled on. Interim
lines are what make a caption panel feel live and are worthless as a record, so only final
ones reach the transcript."""

EVENT_CHAT = "chat"
"""One message from the meeting chat panel. ``{type, id, name, text}``.

``id`` is the page's own de-duplication key, not Zoom's: the panel is re-rendered constantly
and a scan-based reader sees every message repeatedly. The page holds the set it has already
reported, for the reason ``EVENT_HAND_RAISE`` is edge-triggered there too."""


def encode_audio(pcm: bytes, *, pts_us: int) -> bytes:
    """Frame one PCM buffer for the page."""
    return _HEADER.pack(MAGIC, VERSION, KIND_AUDIO_PCM, 0, pts_us, len(pcm)) + pcm


def decode_audio(data: bytes) -> bytes | None:
    """Extract the PCM from one page→bridge audio frame, or ``None`` if it is not one.

    Never raises, for the reason ``decode_event`` does not: this is called from the page
    server's read loop against bytes produced by a script running inside a page this service
    does not control. A malformed frame is a fact about the page.

    **The header's ``pts_us`` is read and discarded**, which is deliberate rather than an
    oversight. The page stamps from ``AudioContext.currentTime``, which runs on the audio
    device's timeline — an arbitrary origin that drifts against the monotonic clock the rest
    of the pipeline is paced on. Mixing the two would corrupt the single-clock invariant A/V
    sync depends on, so ``ingest/mapping.py`` stamps from ``MediaClock`` instead. This is the
    same rule ``google_meet/audio_capture/mapping.py`` and ``teams/ingest/mapping.py``
    already follow, arrived at independently for each transport.
    """
    if len(data) < HEADER_SIZE:
        return None
    magic, version, kind, _reserved, _pts_us, length = _HEADER.unpack_from(data)
    if magic != MAGIC or version != VERSION or kind != KIND_AUDIO_CAPTURE:
        return None
    pcm = data[HEADER_SIZE : HEADER_SIZE + length]
    # Short rather than absent: a truncated frame is a page that was cut off mid-send, and
    # forwarding a partial sample would make ``AudioFrame`` raise on a boundary the page is
    # responsible for. Dropped and counted by the caller.
    if len(pcm) != length or not pcm:
        return None
    return pcm


def decode_event(data: str | bytes) -> dict[str, Any] | None:
    """Parse one page→bridge text frame, or ``None`` when it is not a usable event.

    Never raises, and that is the contract rather than a convenience: this is called from
    the page server's read loop with bytes a *browser* produced, against a script running
    inside a page this service does not control. A malformed frame is a fact about the page,
    not an error condition here — so it is dropped and counted, exactly as a malformed RTMS
    audio frame is.

    ``None`` covers everything unusable in one answer: oversized, not UTF-8, not JSON, not an
    object, or an object with no ``type``. The caller has nothing different to do about any of
    them, so distinguishing them would be detail with no decision behind it.
    """
    raw = data.encode("utf-8", errors="replace") if isinstance(data, str) else bytes(data)
    if not raw or len(raw) > MAX_EVENT_BYTES:
        return None
    try:
        decoded = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(decoded, dict):
        return None
    event_type = decoded.get("type")
    if not isinstance(event_type, str) or not event_type:
        return None
    return decoded
