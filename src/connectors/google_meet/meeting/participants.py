"""The roster, as the page observes it.

**What this is for, and what it is deliberately not for.** On Zoom and Teams the roster is
load-bearing: it supplies the bot's own participant id, which ``EchoGuard`` uses to drop
the avatar's own audio arriving back through ingest. Here it is *observability only*, and
the reason is structural rather than a shortcut.

``js/bridge.js`` taps audio from ``RTCPeerConnection``'s ``track`` event, which fires for
inbound transceivers exclusively. The avatar's own synthetic microphone is an *outbound*
track, so it can never appear in the tap — echo is impossible at the WebRTC layer, not
merely filtered. And the tap sums every remote participant into one mono node before the
worklet sees it, so an inbound frame has no per-speaker attribution to match a roster entry
against even in principle.

So this module answers operational questions — is anyone else here, did the candidate
leave, how many people is the avatar talking to — and not media ones. Saying so plainly
matters, because the natural assumption from the other two connectors is that a roster
implies identity-based echo suppression, and here it does not.

The DOM this parses is machine-generated and lossy: names come from ``aria-label``
attributes that also carry status text ("Alice Smith, presenting"), and ids are absent
whenever Meet renders a tile without one. Everything here therefore treats missing and
malformed data as normal rather than exceptional.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from src.domain.meeting import ParticipantRef
from src.infrastructure.logging import get_logger

logger = get_logger(__name__)

_MAX_NAME_LEN = 120
_STATUS_SUFFIXES: tuple[str, ...] = (
    ", presenting",
    ", pinned",
    ", muted",
    " is presenting",
    " (you)",
)
"""Status text Meet appends inside the same ``aria-label`` as the name. Stripped so that
one person does not appear as two entries when they start presenting."""


@dataclass(frozen=True, slots=True)
class MeetParticipant:
    """One roster entry.

    ``ParticipantRef.user_id`` is an ``int`` across the domain, because Zoom and Teams both
    identify participants numerically. Meet's ids are opaque strings, so
    ``to_domain`` hashes them into that space — see there for why that is safe *here* and
    would not be on either other connector.
    """

    page_id: str
    display_name: str
    is_self: bool = False

    def to_domain(self) -> ParticipantRef:
        """Translate to the canonical model.

        The id is a stable hash of Meet's string id, which is enough for the only thing
        this ref is used for: telling entries apart between two scans. It is explicitly
        *not* an identity that can be matched against an audio frame — nothing in this
        connector attributes audio to a participant, because the capture graph mixes every
        remote track before it is sampled. On Zoom or Teams a synthetic id like this would
        be dangerous, since ``EchoGuard`` compares ids against inbound frames and a
        collision would silently suppress a real speaker. Here there is nothing to
        collide with.
        """
        return ParticipantRef(
            user_id=_stable_id(self.page_id or self.display_name),
            display_name=self.display_name or None,
        )


@dataclass(frozen=True, slots=True)
class MeetRoster:
    """A point-in-time view of who is in the meeting."""

    participants: tuple[MeetParticipant, ...] = field(default_factory=tuple)
    self_name: str | None = None

    @property
    def count(self) -> int:
        return len(self.participants)

    @property
    def others(self) -> tuple[MeetParticipant, ...]:
        """Everyone except the avatar.

        The number that actually matters operationally: an avatar alone in a meeting is
        talking to nobody, which is worth surfacing but is not a failure — it happens
        legitimately whenever the avatar arrives before the candidate.
        """
        return tuple(p for p in self.participants if not p.is_self)

    def to_domain(self) -> tuple[ParticipantRef, ...]:
        return tuple(p.to_domain() for p in self.participants)


def parse_roster(body: dict[str, Any]) -> MeetRoster:
    """Build a roster from a ``PARTICIPANTS`` payload.

    Never raises. The page is reporting on a DOM it does not control, so a malformed entry
    is dropped and the rest is kept — losing the roster entirely because one tile was odd
    would trade a complete answer for no answer.
    """
    self_name = _clean(str(body.get("selfName") or "")) or None
    raw_entries = body.get("participants")
    if not isinstance(raw_entries, list):
        return MeetRoster(self_name=self_name)

    seen: set[str] = set()
    parsed: list[MeetParticipant] = []

    for raw in raw_entries:
        if not isinstance(raw, dict):
            continue
        page_id = str(raw.get("id") or "").strip()
        name = _clean(str(raw.get("name") or ""))
        if not page_id and not name:
            continue

        # Meet renders the same person in several places at once — a tile, a roster row, a
        # presentation banner — and the selector set matches more than one of them.
        # Deduplicating on the strongest available key keeps the count honest.
        key = page_id or name.lower()
        if key in seen:
            continue
        seen.add(key)

        parsed.append(
            MeetParticipant(
                page_id=page_id,
                display_name=name,
                is_self=bool(self_name) and name.lower() == (self_name or "").lower(),
            )
        )

    return MeetRoster(participants=tuple(parsed), self_name=self_name)


def _clean(label: str) -> str:
    """Strip status text and truncate a name pulled out of an ARIA label."""
    value = " ".join(label.split())
    lowered = value.lower()
    for suffix in _STATUS_SUFFIXES:
        index = lowered.find(suffix)
        if index > 0:
            value = value[:index]
            break
    return value.strip().strip(",").strip()[:_MAX_NAME_LEN]


def _stable_id(value: str) -> int:
    """A deterministic non-negative int for a Meet string id.

    ``hash()`` is not usable: Python randomises string hashing per process, so the same
    participant would get a different id after a restart and roster diffs across a
    reconnect would show everyone leaving and rejoining. FNV-1a is a few lines, is stable
    across processes and versions, and needs no import.
    """
    digest = 0x811C9DC5
    for byte in value.encode("utf-8"):
        digest ^= byte
        digest = (digest * 0x01000193) & 0xFFFFFFFF
    return digest
