"""Reducing a Teams DOM label to the person's name.

**Why this is its own module rather than a helper inside one observer.** On this connector a
participant's name is not data — it is whatever text Teams happened to render, and the *same
person* arrives spelled differently depending on which element was read and what their
microphone was doing at that instant. A live meeting produced all three of these for one
participant, within five seconds of each other:

    "Dev Choudhary muted Context menu is available"
    "Dev Choudhary Context menu is available"
    "Dev Choudhary"

Every consumer of a name keys on it: the attendance ledger identifies people by name
(``attendance._key``), the interrupt source's cooldown and the observer's "whose hand is up"
latch are keyed on it, and chat/caption attribution eliminates against it. So one person
spelled three ways is not a cosmetic problem — it is three participants in the roster, three
independent hand-raise latches, and an elimination that never finds "exactly one other
person". Both bugs this module fixes are that fact:

* **A hand that stays up interrupts repeatedly.** The latch key changed the moment the person
  muted, so an unmoved hand was retired under its old key and detected as a fresh raise under
  the new one — and the avatar stopped itself to say "ok, go ahead" again.
* **"What is my name" and "who is in the meeting" cannot be answered.** The names that reach
  the ledger are labels rather than people, so the brief pushed to the agent either names
  nobody or names a sentence.

**Data rather than code**, for the reason ``automation/selectors.py`` gives: this is a list of
things a Teams release can rename, and it is passed into the page as configuration so the
page and Python agree on what a name is. ``js/inject.js`` scrubs at source — which is what
makes the page's own hand key stable — and the observer scrubs again on the way in, because a
page running a stale script must not be able to put a status label into an answer about who
attended. That is the same belt-and-braces ``google_meet/meeting/participants.py`` uses for
Meet's ``", presenting"`` suffixes.

Conservative on purpose. A name that keeps a stray word is a cosmetic fault; a name scrubbed
down to somebody *else's* name, or to nothing, is a wrong answer delivered confidently. So
multi-word phrases — which cannot occur inside a real name — are removed anywhere in the
label, and bare status words are removed only where they trail it.
"""

from __future__ import annotations

import re

_MAX_NAME_LEN = 120

NOISE_PHRASES: tuple[str, ...] = (
    # Observed live, on every roster row: Teams appends the row's context-menu affordance to
    # the accessible name, so the label reads "Dev Choudhary Context menu is available".
    "context menu is available",
    "context menu",
    # Control labels Teams renders inside the row it belongs to. "More options for Priya
    # Menon" is a button; "Priya Menon" is the person.
    "more options for",
    "more options",
    "mute participant",
    "remove from meeting",
    "pin for me",
    "pin for everyone",
    # Status sentences, which travel in the same label as the name.
    "video is off",
    "camera is off",
    "is presenting",
    "sharing screen",
    "is speaking",
    "out of office",
    # Roster section headers, which a row selector matching a group container would pick up.
    "in this meeting",
    "also invited",
    # Belt to ``handMatch``'s braces: a hand indicator's label carries both the phrase and the
    # name, and the name is the half worth keeping.
    "raised their hand",
    "hand is raised",
    "raised hand",
    "hand raised",
)
"""Wording Teams renders in the *same* label as the name.

Removed wherever they appear, which is safe precisely because each is more than one word — no
display name contains "context menu is available". This is the list to extend when a Teams
release starts appending something new; the ``handsIdle`` diagnostic's ``handLabels`` field
reports the raw labels a live page produced, which is where the next entry comes from."""

STATUS_WORDS: tuple[str, ...] = (
    "muted",
    "unmuted",
    "pinned",
    "spotlighted",
    "organizer",
    "organiser",
    "co-organizer",
    "co-organiser",
    "presenter",
    "attendee",
    "guest",
    "unverified",
    "external",
    "offline",
)
"""Single words that are a participant's *state* rather than part of their name.

**Stripped only from the end of the label**, and that restriction is the whole reason this is
a separate list from ``NOISE_PHRASES``. Any of these could be somebody's actual name or part
of one, so removing them anywhere would eventually rename a real person; Teams appends them,
so the end is the only place they legitimately occur. Applied repeatedly, because Teams stacks
them — "Dev Choudhary muted pinned"."""

_PARENTHETICAL = re.compile(
    r"\s*\((?:[^)]*\b(?:you|me|guest|external|organizer|organiser|presenter|attendee"
    r"|co-organizer|co-organiser|unverified|out of office)\b[^)]*)\)\s*$",
    re.IGNORECASE,
)
"""The bracketed form of the same thing: "Dev Choudhary (Guest)".

Kept beside ``STATUS_WORDS`` rather than folded into it because the brackets make it
unambiguous — anything inside them is decoration, so this can match on a word boundary
without the trailing-only restriction."""


def clean_display_name(value: str | None) -> str:
    """Reduce one raw Teams label to a display name, or to ``""`` if nothing survives.

    Never raises: every caller is on the page server's read loop, where the cost of being
    wrong is the avatar going silent. An input this cannot make sense of comes back empty and
    the caller drops it — a missing name is a gap, and a participant called "Context menu is
    available" is a wrong answer in every brief for the rest of the meeting.
    """
    try:
        return _clean(value)
    except Exception:  # pragma: no cover - defensive
        return " ".join(str(value or "").split())[:_MAX_NAME_LEN]


def _clean(value: str | None) -> str:
    # First line only. A row's ``textContent`` runs the name into everything else in it, and
    # Teams puts each of those on its own line.
    name = " ".join(str(value or "").splitlines()[0].split()) if value else ""
    if not name:
        return ""

    lowered = name.lower()
    for phrase in NOISE_PHRASES:
        index = lowered.find(phrase)
        while index >= 0:
            name = f"{name[:index]} {name[index + len(phrase) :]}"
            name = " ".join(name.split())
            lowered = name.lower()
            index = lowered.find(phrase)

    # Brackets and bare words alternately, because Teams stacks them in either order: "Dev
    # (Guest) muted" needs the word off before the bracket is at the end to be seen.
    for _ in range(4):
        before = name
        name = _PARENTHETICAL.sub("", name).strip()
        words = name.split()
        if len(words) > 1 and words[-1].strip(",.;:").casefold() in STATUS_WORDS:
            name = " ".join(words[:-1])
        if name == before:
            break

    name = " ".join(name.split()).strip(" ,.;:-–—")  # noqa: RUF001 — people type both dashes

    # **A label made only of status words is not a person.** A name element that failed to
    # resolve leaves the row's status pill as the whole label, and admitting "muted" to the
    # roster puts a participant in the meeting who does not exist — which breaks the "exactly
    # one other person here" inference that answers "what is my name" just as surely as the
    # duplicate spellings this function exists to remove.
    #
    # The trade is stated rather than hidden: if Teams ever labels a real participant with
    # nothing but one of these words, they are dropped from the roster and elimination fails
    # *closed* — the avatar says it does not know rather than naming the wrong person.
    if name and all(word.casefold() in STATUS_WORDS for word in name.split()):
        return ""

    return _collapse_repeat(name)[:_MAX_NAME_LEN]


def _collapse_repeat(value: str) -> str:
    """Halve a name that is its own first half repeated.

    Teams renders the name twice inside one roster row — once as the row's accessible name and
    again in the name element — so a label read off the container arrives as "Dev Choudhary
    Dev Choudhary". Word-wise rather than by string halves, so a genuine repeated *word* ("Ann
    Ann Smith") is left alone: only an exact doubling of the whole token sequence collapses,
    which a real name does not produce by accident.

    The same fix ``google_meet/meeting/participants._collapse_repeat`` makes, for the same
    reason and against the same shape of markup.
    """
    words = value.split()
    if len(words) >= 2 and len(words) % 2 == 0:
        half = len(words) // 2
        if [w.casefold() for w in words[:half]] == [w.casefold() for w in words[half:]]:
            return " ".join(words[:half])
    return value
