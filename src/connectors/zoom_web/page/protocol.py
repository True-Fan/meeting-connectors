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

**Page → bridge: JSON text frames.** The opposite trade, made for the opposite traffic.
This direction carries what the page can see and RTMS cannot — a raised hand, and the
diagnostics that say whether the observer looking for one is running at all. That is a
handful of messages a minute at the very most, so the per-message cost is irrelevant and
being readable in a log is worth a great deal: the whole feature depends on selectors
matching a UI Zoom is free to change, and the first question when it stops working is
always *what did the page actually see*.

The two are told apart by WebSocket frame type rather than by a discriminator — binary is
audio, text is an event — which is a property the transport already guarantees, so nothing
has to be parsed to route it.

**Why this is still much smaller than the Google Meet page codec.** That one is the whole
connector: video, audio in both directions, roster, chat, captions and hand raises all
cross it, because a browser is the only thing that can see any of them in Meet. Here the
browser is asked for exactly one signal, because Zoom's own API supplies the rest — and a
codec that carried more would be paying Meet's price for Zoom's problem.
"""

from __future__ import annotations

import json
import struct
from typing import Any

MAGIC = b"ZWB1"
VERSION = 1
KIND_AUDIO_PCM = 1

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


def encode_audio(pcm: bytes, *, pts_us: int) -> bytes:
    """Frame one PCM buffer for the page."""
    return _HEADER.pack(MAGIC, VERSION, KIND_AUDIO_PCM, 0, pts_us, len(pcm)) + pcm


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
