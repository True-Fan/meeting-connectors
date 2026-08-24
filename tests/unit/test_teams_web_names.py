"""``meeting/names.py`` — turning a Teams label back into a person.

**Why a whole module and a whole test file for trimming strings.** On this connector the name
*is* the identity: the attendance ledger keys on it, the observer's "whose hand is up" latch
keys on it, and chat, caption and voice attribution all work by eliminating against it. Teams
writes a participant's state into the same accessible name as their name, so one person arrives
spelled several ways — and every one of those consumers then believes there are several people.

The two live failures that produced this file:

* a hand left up interrupted the avatar again every time its owner muted or unmuted, because
  the latch key changed and an unmoved hand looked like a new one;
* "what is my name" and "who is in the meeting" could not be answered in a two-person call,
  because the ledger held three attendees and the inference that names the only other person
  fails closed at two.

The cases below are the labels a live meeting actually produced, and the guard tests are the
ones that keep the scrub from becoming the more expensive mistake — a name reduced to somebody
else's, or to nothing.
"""

from __future__ import annotations

import pytest

from src.connectors.teams_web.meeting.names import (
    NOISE_PHRASES,
    STATUS_WORDS,
    clean_display_name,
)


class TestObservedLive:
    """All three of these were logged for one participant within five seconds."""

    @pytest.mark.parametrize(
        "label",
        [
            "Dev Choudhary muted Context menu is available",
            "Dev Choudhary Context menu is available",
            "Dev Choudhary",
            "Dev Choudhary muted",
            "Dev Choudhary unmuted Context menu is available",
        ],
    )
    def test_every_spelling_reduces_to_one_person(self, label: str) -> None:
        assert clean_display_name(label) == "Dev Choudhary"

    def test_the_stacked_forms_come_off_in_either_order(self) -> None:
        """Teams stacks a bracket and a bare word, and which is last varies."""
        assert clean_display_name("Dev Choudhary (Guest) muted") == "Dev Choudhary"
        assert clean_display_name("Dev Choudhary muted (Guest)") == "Dev Choudhary"

    def test_a_doubled_name_is_halved(self) -> None:
        """A row renders the name twice — as its accessible name and again in the name
        element — so a label read off the container arrives doubled."""
        assert clean_display_name("Dev Choudhary Dev Choudhary") == "Dev Choudhary"

    def test_a_hand_indicator_s_label_still_yields_the_name(self) -> None:
        assert clean_display_name("Dev Choudhary, raised their hand") == "Dev Choudhary"


class TestGuards:
    """The scrub must not become the more expensive mistake."""

    def test_a_real_name_is_left_alone(self) -> None:
        for name in ("Priya Menon", "Chandra Handa", "Anand Chand", "Guest Services Team"):
            assert clean_display_name(name) == name

    def test_a_repeated_word_inside_a_real_name_survives(self) -> None:
        """Only an exact doubling of the whole token sequence collapses."""
        assert clean_display_name("Ann Ann Smith") == "Ann Ann Smith"

    def test_a_status_word_inside_a_name_is_not_removed(self) -> None:
        """Only the *trailing* position is stripped, because a status word could be part of
        somebody's name anywhere else."""
        assert clean_display_name("Guest Presenter Rao") == "Guest Presenter Rao"

    def test_a_label_made_only_of_status_words_is_not_a_person(self) -> None:
        for label in ("muted", "Muted", "Guest", "presenter attendee"):
            assert clean_display_name(label) == ""

    def test_a_pronoun_row_is_left_for_the_page_to_recognise(self) -> None:
        """Teams labels the avatar's own row "You", and the page's ``isPronounSelf`` check is
        the only thing that can recognise it. Scrubbing it to empty here would work by accident
        and stop working the day Teams adds a suffix to it."""
        assert clean_display_name("You") == "You"

    @pytest.mark.parametrize("value", ["", None, "   ", "\n\n"])
    def test_nothing_in_means_nothing_out(self, value: str | None) -> None:
        assert clean_display_name(value) == ""

    def test_only_the_first_line_is_read(self) -> None:
        """A row's ``textContent`` runs the name into everything else in it, one item per
        line."""
        assert clean_display_name("Dev Choudhary\nmuted\nContext menu") == "Dev Choudhary"

    def test_the_length_is_bounded(self) -> None:
        assert len(clean_display_name("x" * 500)) == 120


class TestTheListsThePageIsGiven:
    """These are shipped into the page as configuration, so their shape is a contract.

    ``js/inject.js`` scrubs at source — which is what keeps the hand-raise key stable — and it
    is handed *these* lists. A phrase that cannot be matched case-insensitively there, or a
    "status word" that is really a phrase, would be silently dropped by the page while Python
    kept applying it, and the two would disagree about who is in the meeting.
    """

    def test_every_noise_phrase_is_lowercase_and_more_than_one_word(self) -> None:
        for phrase in NOISE_PHRASES:
            assert phrase == phrase.lower(), phrase
            # Multi-word is what makes removing it *anywhere* safe: no display name contains
            # "context menu". A single word here would eventually rename a real person.
            assert " " in phrase, phrase

    def test_every_status_word_is_lowercase_and_a_single_word(self) -> None:
        for word in STATUS_WORDS:
            assert word == word.lower(), word
            assert " " not in word, word
