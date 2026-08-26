"""What the injected script looks at to find a raised hand.

**Injected as data, not written into the JavaScript**, for the reason
``ZoomWebSelectors`` in ``meeting/join.py`` is: a Zoom UI change then costs a settings
edit rather than an asset edit and a redeploy. The script treats every one of these as
optional and unparseable ones as a miss, so a selector that stops matching costs the
signal it carried and nothing else.

**Why there are three lists rather than one.** They answer three different questions,
and conflating them would make each worse:

* ``hand_indicator`` — the element that *is* a raised hand. Precise, and the most
  likely of the three to be renamed by a Zoom release.
* ``participant_row`` — the container that element sits in, which is how an icon with
  no text of its own becomes a person. Far more stable, because it is the panel's
  fundamental unit.
* ``participant_name`` — where the name lives inside that row.

The script also runs a label sweep that depends on none of them, matching the sentence
Zoom shows a human ("… raised hand") wherever it appears. That is the pass that keeps
working when every class name here has changed, and it is why these being wrong
degrades the feature instead of disabling it.

Nothing here is a guess dressed as a fact: these are the class names Zoom's web client
has used, listed most-specific first, with generic fallbacks behind them. Verify
against a live meeting with ``MC_ZOOM_WEB__HEADLESS=false`` and the ``handsIdle``
diagnostics, which report what the page actually contains when nothing matched.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ZoomHandSelectors:
    """CSS selectors for the raised-hand observer. Ordering is most-specific first."""

    hand_indicator: tuple[str, ...] = (
        # Zoom's participants panel marks the row with a nonverbal-feedback icon.
        ".participants-icon__participants-raisehand",
        ".participants-item__buttons .participants-icon-raisehand",
        "[class*='raisehand' i]",
        "[class*='raise-hand' i]",
        # The video tile's footer badge, for a layout with the panel closed.
        ".video-avatar__avatar-footer [class*='hand' i]",
        "[aria-label*='raised hand' i]",
        "[title*='raised hand' i]",
    )

    participant_row: tuple[str, ...] = (
        ".participants-item__item-inner",
        ".participants-li",
        "[class*='participants-item' i]",
        # Zoom's newer participants panel is a virtualised list; these are what it renders
        # rows as. Added after a live run reported ``rows: 0`` with the panel open, which
        # means every selector above it missed — the list exists, under other names.
        "#participants-ul > li",
        "[class*='participants-list' i] li",
        "[role='listitem']",
        ".video-avatar__avatar",
        "[data-participant-id]",
    )

    participant_name: tuple[str, ...] = (
        ".participants-item__display-name",
        "[class*='display-name' i]",
        ".video-avatar__avatar-name",
        "[class*='avatar-name' i]",
        # The tile's title, which is where the name lives when the tile is showing video
        # — and a tile showing video is exactly the one a raised hand appears on, because
        # ``video-avatar__avatar-img`` replaces ``…-name`` there. Observed in a live run:
        # the row that gained the hand indicator was the only row with no name element.
        ".video-avatar__avatar-title",
        "[class*='avatar-title' i]",
    )

    participants_panel_button: tuple[str, ...] = (
        # Zoom labels the toggle by what it opens, and the count is appended to it —
        # "Open the participants list panel, 3" — so every match here is a substring.
        "button[aria-label*='participants' i]",
        "[role='button'][aria-label*='participants' i]",
        ".footer-button__participants-icon",
        "#participant",
    )
    """The control that opens the participants panel.

    **The indicator does not exist in a DOM nobody opened**, which is the single most
    likely reason for a correctly-configured observer to see nothing. Zoom renders a
    raised hand as a transient toast with the panel closed, and the panel is where the
    persistent indicator lives."""


DEFAULT_HAND_SELECTORS = ZoomHandSelectors()


@dataclass(frozen=True, slots=True)
class ZoomObserverSelectors:
    """CSS selectors for the observers that read the meeting off the page.

    **This is the honest cost of not requiring an RTMS-enabled account.** The connector once
    got who joined, who is speaking, what was said and what was typed from Zoom's own event
    streams, with a name on each and no markup involved; that required the meeting to be
    hosted on an account a deployment usually does not control, so it is read from the DOM
    instead and a Zoom release can rename every class below.

    That cost is concentrated here on purpose: these are the fields to edit when a Zoom
    update makes the avatar go quiet, and they are data rather than code for that reason.

    Ordering is most-specific first, and the page uses **the first list that matches
    anything** rather than the union — a generic fallback like ``[role='listitem']`` would
    otherwise pull in half the page alongside the precise match and make every name wrong.
    """

    roster_row: tuple[str, ...] = (
        # **The video tiles first, and that ordering is what a live meeting corrected.**
        # The participants-panel selectors below were listed first on the assumption that a
        # panel is the roster. In a real meeting — panel confirmed open, ``panelOpened``
        # logged — every one of them matched nothing, while the tiles matched exactly the
        # two people present:
        #
        #   handsIdle rows=2 sample=[
        #     {classes: [video-avatar__avatar, …-title, …-name,  …-footer]},
        #     {classes: [video-avatar__avatar, …-title, …-img,   …-footer]}]
        #
        # Zoom's participants panel in this build does not render the class names its older
        # builds did. The tile grid does, it is present in every layout, and it does not
        # depend on a panel anybody has to open.
        ".video-avatar__avatar",
        "[class*='video-avatar__avatar']:not([class*='__avatar-'])",
        # Retained behind the tiles rather than deleted: they are what other Zoom builds
        # render, they cost nothing when they match nothing, and the page takes the first
        # list that matches anything — so these are reached only where the tiles are absent.
        ".participants-item__item-inner",
        ".participants-li",
        "[class*='participants-item' i]",
        "#participants-ul > li",
        "[class*='participants-list' i] li",
    )
    """One element per person in the meeting.

    Deliberately **narrower than ``ZoomHandSelectors.participant_row``**, which also lists
    ``[role='listitem']`` and ``[data-participant-id]``. A hand observer wants every place an
    indicator might appear and pays nothing for a false row with no hand in it. A roster
    reader pays a great deal: a false row becomes a participant who is not in the meeting, and
    the ledger reports them as present for the rest of the session.

    The ``:not([class*='__avatar-'])`` on the second entry is what keeps that promise while
    still being loose: without it the pattern also matches ``video-avatar__avatar-title`` and
    ``…-footer``, so one person becomes four participants named after fragments of their own
    tile.

    **Known limit of reading the tiles: they are the people on screen, not the people in the
    meeting.** Zoom paginates and virtualises the grid, so past roughly one screenful the
    roster becomes "whoever is currently rendered" and participants appear to join and leave
    as the avatar's view scrolls. That is correct for the small meetings this connector is
    aimed at and wrong for a webinar. The participants-panel entries below are the right
    answer at that size — they need a build whose class names they match, which is what
    ``observerIdle`` diagnostics exist to supply."""

    roster_name: tuple[str, ...] = (
        # **``-title`` first, because it is the only one present on every tile.** The live
        # sample shows why: the camera-off tile carries ``…-name``, the camera-on tile
        # carries ``…-img`` instead — and both carry ``…-title``. Leading with ``-name``
        # would read the roster correctly right up until somebody turned their camera on,
        # and then silently drop them.
        ".video-avatar__avatar-title",
        "[class*='avatar-title' i]",
        ".video-avatar__avatar-name",
        "[class*='avatar-name' i]",
        ".participants-item__display-name",
        "[class*='display-name' i]",
    )

    speaker_row: tuple[str, ...] = (
        # **The filmstrip tile, because that is the element carrying the state.** A live
        # meeting reported ``speaker-bar-container__video-frame--active`` appearing and
        # disappearing while people talked — the modifier is on the *frame*, and the
        # ``video-avatar__avatar`` sits inside it. Leading with the avatar would leave the
        # marker on an ancestor, which is why ``scanSpeaker`` also walks upward.
        "[class*='speaker-bar-container__video-frame' i]",
        ".video-avatar__avatar",
        "[class*='video-avatar__avatar']:not([class*='__avatar-'])",
        ".participants-item__item-inner",
        "[class*='participants-item' i]",
    )

    speaker_marker: tuple[str, ...] = (
        # **Observed live**: the `--active` modifier on the filmstrip's video frame.
        "[class*='speaker-bar-container__video-frame--active' i]",
        # The same shape, in case Zoom renames the container but keeps the convention.
        "[class*='__video-frame--active' i]",
        "[class*='active' i][class*='speaker-bar' i]",
        "[class*='active-speaker' i]",
        "[class*='is-speaking' i]",
        "[class*='talking' i]",
        "[aria-label*='is speaking' i]",
    )
    """What marks a row or tile as the one currently talking.

    **``[class*='speaker-active']`` was here and was actively harmful**, which a live run
    settled. It matched two elements permanently, because ``speaker-active-container__wrap``
    and ``…__video-frame`` are the *layout* of Zoom's active-speaker view — they exist
    whether or not anybody is talking, and in every layout. A marker that is always present
    is not a marker. The same objection retires the old ``[class*='speaking' i]`` catch-all:
    it matches ``speaker-…`` prefixes, so it too was permanently true.

    The tokens the same run reported, with the state ones marked:

        speaker-active-container__wrap             layout, always present
        speaker-active-container__video-frame      layout, always present
        speaker-bar-container__video-frame         layout, always present
        speaker-bar-container__video-frame--active **state**, comes and goes
        audio-voip-active-icon                     the avatar's own audio button
        SvgSpeakerView                             an icon component

    Still the least certain list in the file, because a modifier class is a styling detail
    Zoom is free to rename. It degrades to no active speaker being reported, which costs
    attribution and not the meeting: the avatar still hears everyone, barge-in still fires
    from audio energy, and ``_speaker_provider`` still names the only other person by
    elimination."""

    chat_panel_button: tuple[str, ...] = (
        "button[aria-label*='chat' i]",
        "[role='button'][aria-label*='chat' i]",
        ".footer-button__chat-icon",
        "#chat",
    )

    chat_container: tuple[str, ...] = (
        "#chat-list-content",
        "[class*='chat-list' i]",
        "[class*='chat-container' i]",
        "[aria-label*='chat message list' i]",
        "[class*='chat-virtualized-list' i]",
    )
    """The chat list element, which exists whether or not it holds any messages.

    **This is what tells "open and empty" apart from "not rendered yet", and getting that
    wrong swallowed a real question.** The observer used to take its baseline on the first
    pass that found *content*, so a panel that opened empty stayed unarmed until somebody
    typed — and then recorded that person's message as backlog and answered nothing. The
    observer reported itself armed, the avatar stayed silent, and the log said a message had
    been seen.

    The backlog worth suppressing is whatever is in the panel *when it opens*. If that is
    nothing, nothing should be suppressed. A container match is what makes that statement
    possible; ``panel_ready_timeout_ms`` is the fallback if every selector here is renamed,
    so a miss costs a delay rather than the feature."""

    caption_container: tuple[str, ...] = (
        "#live-transcript-subtitle",
        "[class*='live-transcript' i]",
        "[class*='subtitle-container' i]",
        "[class*='closed-caption' i]",
    )
    """The live-transcript region, on the same terms as ``chat_container``.

    The empty case is more common here than for chat: when ``captions_auto_enable`` is on the
    avatar switches captions on itself, so the panel is empty at that exact moment and the
    first line transcribed is always genuinely new."""

    chat_item: tuple[str, ...] = (
        ".new-chat-message__container",
        "[class*='chat-message__container' i]",
        "[class*='chat-item' i]",
        "#chat-list-content > div",
        "[aria-label*='chat message' i]",
    )

    chat_name: tuple[str, ...] = (
        ".chat-message__sender",
        "[class*='sender' i]",
        "[class*='chat-item__sender' i]",
    )

    chat_text: tuple[str, ...] = (
        ".new-chat-message__text",
        "[class*='chat-message__text' i]",
        "[class*='chat-item__text' i]",
        "[id^='chat-message-content']",
    )

    captions_button: tuple[str, ...] = (
        # Zoom labels the control by what it does, and the wording differs by account:
        # "Show Captions", "Live Transcript", "Closed Caption".
        "button[aria-label*='show captions' i]",
        "button[aria-label*='live transcript' i]",
        "button[aria-label*='closed caption' i]",
        "button[aria-label*='captions' i]",
        ".footer-button__closed-caption-icon",
    )
    """The control that turns Zoom's live transcription on.

    **Clicking this is visible to everybody in the meeting**, which is why
    ``captions_auto_enable`` defaults to off and this is only consulted when it is on. It is
    also the only way this connector can answer "what did they say" at all: there is no
    invisible per-participant transcription available to it."""

    caption_item: tuple[str, ...] = (
        ".live-transcript-subtitle__item",
        "[class*='live-transcript' i] [class*='item' i]",
        "[class*='subtitle-item' i]",
        "#live-transcript-subtitle > div",
    )

    caption_name: tuple[str, ...] = (
        "[class*='transcript' i] [class*='name' i]",
        "[class*='subtitle' i] [class*='name' i]",
        "[class*='speaker' i]",
    )

    caption_text: tuple[str, ...] = (
        "[class*='transcript' i] [class*='text' i]",
        "[class*='subtitle' i] [class*='text' i]",
        "[class*='content' i]",
    )


DEFAULT_OBSERVER_SELECTORS = ZoomObserverSelectors()
