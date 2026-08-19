"""The bridge→page wire format for the avatar's microphone.

One message type, fixed header, raw PCM payload::

    magic    4s   b'ZWB1'
    version  B    1
    kind     B    1 = audio pcm
    reserved H    0
    pts_us   Q    presentation timestamp
    length   I    PCM byte count

Binary rather than JSON because this carries 50 frames a second; base64 in an
envelope would cost a third more bytes and a parse per frame, for readability nobody
benefits from — nothing here is read by a human.

Deliberately much smaller than the Google Meet page codec, which is bidirectional and
carries video, roster, chat and hand raises. This direction is audio only, and a
smaller protocol is the honest shape of that rather than a shortcut.
"""

from __future__ import annotations

import struct

MAGIC = b"ZWB1"
VERSION = 1
KIND_AUDIO_PCM = 1

_HEADER = struct.Struct("!4sBBHQI")
HEADER_SIZE = _HEADER.size


def encode_audio(pcm: bytes, *, pts_us: int) -> bytes:
    """Frame one PCM buffer for the page."""
    return _HEADER.pack(MAGIC, VERSION, KIND_AUDIO_PCM, 0, pts_us, len(pcm)) + pcm
