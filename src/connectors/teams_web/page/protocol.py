"""The page channel's two wire formats — one per direction, because they carry opposite
kinds of thing.

**Bridge → page: binary.** One message type, fixed header, raw PCM payload::

    magic    4s   b'TWB1'
    version  B    1
    kind     B    1 = audio pcm
    reserved H    0
    pts_us   Q    presentation timestamp
    length   I    PCM byte count

Binary rather than JSON because this carries 50 frames a second; base64 in an envelope
would cost a third more bytes and a parse per frame, for readability nobody benefits from —
nothing here is read by a human.

**Page → bridge: two formats, one per kind.**

*Binary* is the meeting's audio, framed with the same header and a different ``kind``. It
carries 50 frames a second, which is the same argument the outbound direction makes for not
being JSON.

*Text* is everything the page **observes**: the roster, the active speaker, the chat, the
captions and a raised hand. A few messages a second at most, so the per-message cost is
irrelevant and being readable in a log is worth a great deal — every one of these depends on
selectors matching a UI Microsoft is free to change, and the first question when one stops
working is always *what did the page actually see*.

The two are told apart by WebSocket frame type rather than by a discriminator — binary is
audio, text is an event — which is a property the transport already guarantees, so nothing
has to be parsed to route it.

**Why the magic is ``TWB1`` and not ``ZWB1``.** The header layout is identical to the
Zoom-web one and the constant is not, deliberately. These are two independent codecs that
happen to agree, and a frame captured or logged in isolation should say which connector
produced it — the alternative is a debugging session spent working out which browser a
hexdump came from. ``tests/architecture/test_layering.py`` keeps each inside its own
connector for the same reason.
"""

from __future__ import annotations

import json
import struct
from typing import Any

MAGIC = b"TWB1"
VERSION = 1
KIND_AUDIO_PCM = 1
"""Bridge → page: the avatar's voice, for the synthetic microphone."""

KIND_AUDIO_CAPTURE = 2
"""Page → bridge: the meeting's audio, tapped out of the page's own playout graph.

A separate kind rather than reusing ``KIND_AUDIO_PCM``, even though the direction already
disambiguates them: the two are different things that happen to share a header."""

_HEADER = struct.Struct("!4sBBHQI")
HEADER_SIZE = _HEADER.size

MAX_EVENT_BYTES = 64 * 1024
"""Ceiling on one page→bridge event.

A guard against a page that has gone wrong rather than a product limit: the largest
legitimate message here is a chat line or a caption, both already truncated in the page.
Anything approaching this is a malfunction, and refusing it keeps a bad script from making
the bridge allocate."""

EVENT_HAND_RAISE = "handRaise"
"""A participant's hand just went up. ``{type, id, name, isSelf}``.

**Edge-triggered, and the page owns the edge.** Teams renders a raised hand as a *state* —
an icon that stays until it is lowered — and re-renders it constantly. What the avatar has
to react to is the moment it goes up, so the page holds the current set and reports
transitions; a level signal would arrive many times per raised hand and Python would have to
reconstruct the edge anyway.

The page decides nothing about whether to interrupt. It reports who, and Python decides —
see ``meeting/hand_raise.py``."""

EVENT_HAND_LOWER = "handLower"
"""The page has stopped seeing a hand it previously reported. ``{type, id}``.

Sent so that Python, rather than the page, can hold the authoritative "still up" state. The
page's own set is keyed on a name read out of a row Teams re-renders, and several frames run
the observer independently — so a hand that stays up while its row is re-rendered is retired
in the page and would otherwise be re-detected as a fresh raise. See
``TeamsMeetingObserver._on_hand``."""

EVENT_PAGE = "pageEvent"
"""Diagnostics: what each observer armed with, what it can see, whether it found anything.

Carried because every observer here fails by *finding nothing*, and finding nothing is
exactly what a quiet meeting looks like. These messages are what tell the two apart."""

EVENT_ROSTER = "roster"
"""Who the page can currently see in the meeting. ``{type, names: [...]}``.

**A level, not an edge, and that is the opposite of every other event here.** A DOM reports
a list. Deriving the edges in the page would mean the page holding the authoritative roster,
and a page reload would then re-announce the whole meeting as having just joined. So the page
reports what it sees and ``TeamsMeetingObserver`` diffs it against what it saw last."""

EVENT_SPEAKER = "speaker"
"""The page believes this participant now holds the floor. ``{type, name, isSelf}``.

Teams marks the active speaker by drawing an animated ring on their tile and a matching
indicator on their roster row. That is a rendering rather than an event, so it lags and it
flickers — which is what ``speaker_min_ms`` in the page and ``TeamsSpeakerTracker``'s hold
and merge windows in Python exist to absorb."""

EVENT_CAPTION = "caption"
"""One line of Teams' live captions, as rendered. ``{type, name, text, final}``.

**Requires captions to be switched on in the meeting**, which is a visible action and hence
a setting (``MC_TEAMS_WEB__CAPTIONS_AUTO_ENABLE``).

``final`` distinguishes a caption still being revised from one Teams has settled on. Interim
lines are what make a caption panel feel live and are worthless as a record — and worse than
worthless to an agent, which would answer half a question."""

EVENT_CHAT = "chat"
"""One message from the meeting chat panel. ``{type, id, name, text}``.

``id`` is the page's own de-duplication key, not Teams': the panel is virtualised and
re-rendered constantly, so a scan-based reader sees every message repeatedly. It encodes
*which copy* of an identical line this is, so somebody re-sending a question the avatar did
not answer is heard the second time — see ``chatSeen`` in ``js/inject.js``."""


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
    sync depends on, so ``ingest/page_audio_source.py`` stamps from ``MediaClock`` instead.
    Every transport in this repository arrived at the same rule independently.
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

    Never raises, and that is the contract rather than a convenience: this is called from the
    page server's read loop with bytes a *browser* produced, against a script running inside
    a page this service does not control. A malformed frame is a fact about the page, not an
    error condition here — so it is dropped and counted.

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
