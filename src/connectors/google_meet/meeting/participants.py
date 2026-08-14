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

_SELF_MARKERS: tuple[str, ...] = ("(you)", "(You)")
"""Text Meet appends to *your own* roster entry.

**Read before ``_STATUS_SUFFIXES`` strips it, which is the whole point.** ``" (you)"`` is in
both lists: it has to come off the name so one person is not two entries, and it is also the
only self signal the page can offer that does not depend on configuration. Stripping it first
threw the signal away, and the observable consequence was the avatar counting *itself* as a
participant — because ``selfName`` carries the configured ``display_name`` ("AI Avatar"), which
a signed-in profile never uses; Meet renders the Google account's own name instead."""

_UI_TOKENS: tuple[str, ...] = (
    "frame_person",
    "visual_effects",
    "more_vert",
    "present_to_all",
    "devices",
    "closed_caption",
    "keep",
    "push_pin",
)
"""Material icon-font names that appear as *text* inside a participant tile.

An icon font glyph is a text node holding the glyph's name, so any of these in a name means the
label is a container's full text rather than a person — the same fact ``handSignal`` in
``bridge.js`` relies on. ``js/bridge.js`` now takes only the first line, which removes these at
source; this is the second line of defence, because a page running a stale script must not be
able to put "frame_person Reframe visual_effects" into an answer about who attended."""

_CONTROL_PHRASES: tuple[str, ...] = (
    "more options for",
    "backgrounds and effects",
    "reframe",
    "pin to screen",
    "remove from meeting",
)
"""Control labels Meet renders alongside a name. Removed before the name is judged, because
"More options for Priya Menon" is a button's label and "Priya Menon" is the person."""


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
        raw_name = str(raw.get("name") or "")
        # Before cleaning, because cleaning removes the marker this reads.
        marked_self = _is_self_label(raw_name)
        name = _clean(raw_name)

        # A label that *was* text and cleaned to nothing is a rejection, not an anonymous
        # person: it means the node matched a container rather than a participant, so the id
        # attached to it does not identify somebody to count. Distinguished from an id with no
        # label at all, which is a real participant Meet declined to attribute and is kept.
        if raw_name.strip() and not name:
            continue
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
                # Three independent signals, any of which is sufficient. The configured name
                # is the weakest and used to be the only one — see ``_SELF_MARKERS``.
                is_self=(
                    marked_self
                    or bool(raw.get("isSelf"))
                    or (bool(self_name) and name.lower() == (self_name or "").lower())
                ),
            )
        )

    return MeetRoster(participants=tuple(parsed), self_name=self_name)


def _is_self_label(label: str) -> bool:
    """Whether a raw label is Meet's marking of our own entry.

    Takes the *uncleaned* label, because ``_clean`` removes the marker.
    """
    lowered = " ".join(str(label or "").split()).lower()
    return any(marker in lowered for marker in _SELF_MARKERS)


def _clean(label: str) -> str:
    """Reduce a raw label to a person's name, or to empty when it is not one.

    Four passes, each answering a failure seen in a live meeting rather than an imagined one:

    1. **First line only.** ``js/bridge.js`` now does this at source, but a page running a stale
       script still sends a tile's whole text.
    2. **Control phrases out.** "More options for Priya Menon" is a button, not a person.
    3. **Icon tokens reject the label.** Their presence means this is a container's text, and
       nothing salvaged from it can be trusted to be a name.
    4. **Collapse a doubled name.** Meet renders the name twice inside one tile — as the label
       and again in the roster row — which arrived as "dev Choudhary dev Choudhary".

    Returns ``""`` for anything that does not survive, and the caller drops it. A missing
    participant is a gap; a participant called "frame_person Reframe visual_effects" is a wrong
    answer delivered confidently, which is worse.
    """
    value = " ".join(str(label or "").splitlines()[0].split()) if label else ""

    lowered = value.lower()
    for phrase in _CONTROL_PHRASES:
        index = lowered.find(phrase)
        if index >= 0:
            value = (value[:index] + " " + value[index + len(phrase) :]).strip()
            lowered = value.lower()

    if any(token in lowered for token in _UI_TOKENS):
        return ""

    lowered = value.lower()
    for suffix in _STATUS_SUFFIXES:
        index = lowered.find(suffix)
        if index > 0:
            value = value[:index]
            break

    value = " ".join(value.split()).strip().strip(",").strip()
    return _collapse_repeat(value)[:_MAX_NAME_LEN]


def _collapse_repeat(value: str) -> str:
    """Halve a name that is its own first half repeated.

    "dev Choudhary dev Choudhary" is one person. Word-wise rather than by string halves so that
    a genuine repeated *word* ("Ann Ann Smith") is left alone — only an exact doubling of the
    whole token sequence collapses, which a real name does not produce by accident.
    """
    words = value.split()
    if len(words) >= 2 and len(words) % 2 == 0:
        half = len(words) // 2
        if [w.casefold() for w in words[:half]] == [w.casefold() for w in words[half:]]:
            return " ".join(words[:half])
    return value


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
