"""What the injected script looks at, as data rather than as JavaScript.

**Injected as configuration**, for the reason ``TeamsWebSelectors`` in ``meeting/join.py``
is: a Teams UI change then costs a settings edit rather than an asset edit and a redeploy.
The script treats every one of these as optional and an unparseable one as a miss, so a
selector that stops matching costs the signal it carried and nothing else.

**Why ``data-tid`` leads every list.** Teams' web client is a Fluent UI application, so its
class names are build-generated hashes that change between releases *by design* — writing a
selector against one is writing it against a compiler artefact. ``data-tid`` is the hook
Microsoft's own test suites use, so it is the closest thing here to a stable name. Class and
``aria-label`` patterns sit behind it as fallbacks, because a label is what the product shows
a human and is the next most durable thing on the page.

**These are candidates, not measurements, and that distinction is deliberate.** The
Zoom-web connector's selector lists carry annotations from live meetings — "observed live",
"a live run reported ``rows: 0``" — because they were corrected against real Zoom builds.
Nothing here has been through that yet. The lists are drawn from the hooks the Teams web
client is known to use, ordered most-specific-first with deliberately loose fallbacks
behind them, and every observer that finds nothing reports what the page *does* contain:
run a session with ``MC_TEAMS_WEB__HEADLESS=false``, read the ``observerIdle`` and
``handsIdle`` diagnostics, and correct the list that is wrong. That loop is the intended
way to use this file, which is why it is data.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TeamsHandSelectors:
    """Selectors for the raised-hand observer. Ordering is most-specific first."""

    hand_indicator: tuple[str, ...] = (
        "[data-tid='raised-hand-icon']",
        "[data-tid='roster-raised-hand']",
        # Loose, and excluded from buttons on purpose: the reactions control in the toolbar
        # carries a ``raisehands`` hook of its own, and matching it would report the avatar's
        # own control as somebody's raised hand on every scan. The page also drops any
        # candidate it cannot attribute to a participant row, so this is the second of two
        # guards rather than the only one.
        "[data-tid*='raisehand' i]:not(button):not([role='button'])",
        "[data-tid*='raised-hand' i]:not(button):not([role='button'])",
        "[aria-label*='raised their hand' i]",
        "[aria-label*='hand raised' i]",
        "[title*='raised hand' i]",
    )

    participant_row: tuple[str, ...] = (
        # Teams renders its roster as a tree, one ``treeitem`` per person, grouped into
        # "In this meeting" and "Presenters"/"Attendees" sections.
        "[data-tid='roster-participant']",
        "[data-tid='participantsInCall'] [role='treeitem']",
        "[role='treeitem'][data-tid]",
        "li[data-tid^='participant']",
        # The tile grid, which exists in every layout and needs no panel open. Wider than the
        # roster and correspondingly less precise, which is why it is last.
        "[data-tid='participant-stream']",
        "[role='listitem']",
    )
    """One element per person the observer might find an indicator inside.

    Deliberately **wider than ``TeamsObserverSelectors.roster_row``**. A hand observer wants
    every place an indicator might appear and pays nothing for a false row with no hand in
    it. A roster reader pays a great deal: a false row becomes a participant who is not in
    the meeting, and the ledger reports them as present for the rest of the session."""

    participant_name: tuple[str, ...] = (
        "[data-tid='roster-participant-name']",
        "[data-tid='displayName']",
        "[data-tid='display-name']",
        "[class*='displayName' i]",
        "[class*='participantName' i]",
    )

    participants_panel_button: tuple[str, ...] = (
        # Teams has called this control both "People" and "Participants" across releases, and
        # appends the count to its label — so every text match here is a substring.
        "button[data-tid='roster-button']",
        "button[data-tid='toggle-participants']",
        "button[id='roster-button']",
        # **Scoped to the calling toolbar, and that is not tidiness.** An unscoped
        # ``button[aria-label*='people' i]`` also matches the **app rail** — the vertical
        # navigation strip down the left of the Teams window, which has "Chat" and "People"
        # buttons of its own. Clicking one of those navigates the whole SPA *out of the
        # meeting* to the contacts page. A live run did exactly that: the avatar joined, the
        # observer opened "People", and the page left the call. See
        # ``TeamsObserverSelectors.app_rail`` for the second guard.
        "[data-tid='calling-toolbar'] button[aria-label*='people' i]",
        "[role='toolbar'] button[aria-label*='people' i]",
        "[data-tid='calling-toolbar'] button[aria-label*='participants' i]",
        "[role='toolbar'] button[aria-label*='participants' i]",
    )
    """The control that opens the participants panel.

    **The roster indicator does not exist in a panel nobody opened**, which is the single most
    likely reason for a correctly-configured hand observer to see nothing. Teams also draws a
    hand on the participant's tile, but a tile is rendered only while that person is on
    screen."""


DEFAULT_HAND_SELECTORS = TeamsHandSelectors()


@dataclass(frozen=True, slots=True)
class TeamsObserverSelectors:
    """Selectors for the observers that stand in for an API this connector cannot reach.

    **This is where the cost of not needing a tenant's consent is concentrated.** The Graph
    connector receives who joined, who is speaking and what each person said as data with a
    source id attached; this connector reads all of it off markup. These are therefore the
    fields to edit when a Teams release makes the avatar go quiet, and they are data rather
    than code for exactly that reason.

    Ordering is most-specific first, and the page uses **the first list that matches
    anything** rather than the union — a generic fallback like ``[role='listitem']`` would
    otherwise pull in half the page alongside the precise match and make every name wrong.
    """

    roster_row: tuple[str, ...] = (
        "[data-tid='roster-participant']",
        "[data-tid='participantsInCall'] [role='treeitem']",
        "[role='treeitem'][data-tid]",
        # The tile grid, behind the roster rather than in front of it: it needs no panel open,
        # and it is the people *on screen* rather than the people in the meeting. Teams
        # paginates and virtualises the grid, so past roughly one screenful the roster becomes
        # "whoever is currently rendered" and participants appear to join and leave as the
        # avatar's view scrolls. Correct for the small meetings this connector is aimed at,
        # wrong for a town hall.
        "[data-tid='participant-stream']",
    )
    """One element per person in the meeting.

    Narrower than ``TeamsHandSelectors.participant_row`` — see that field for why the two
    differ rather than being shared."""

    roster_name: tuple[str, ...] = (
        "[data-tid='roster-participant-name']",
        "[data-tid='displayName']",
        "[data-tid='display-name']",
        "[class*='displayName' i]",
        "[class*='participantName' i]",
        # The tile's accessible name, which is where the name lives when the tile is showing
        # video and no text element is rendered at all.
        "[aria-label]",
    )

    speaker_row: tuple[str, ...] = (
        # The tile is the element that carries the state: Teams draws the speaking indicator
        # as a ring on the participant's video frame.
        "[data-tid='participant-stream']",
        "[data-tid*='stream-container' i]",
        "[data-tid='roster-participant']",
        "[role='treeitem'][data-tid]",
    )

    speaker_marker: tuple[str, ...] = (
        # Teams' animated ring, which is the state that comes and goes.
        "[data-tid='voice-level-stream-outline']",
        "[data-tid='participant-speaking']",
        "[class*='voiceLevel' i]",
        "[class*='speakingRing' i]",
        "[class*='dominantSpeaker' i]",
        "[aria-label*='is speaking' i]",
        "[aria-label*='speaking' i]",
    )
    """What marks a row or tile as the one currently talking.

    **The least certain list in this file, and the one to correct first.** A marker has to be
    a hook that *toggles*, and every plausible-looking hook that turns out to be layout is
    permanently present — which makes it not a marker at all, and reports whoever the layout
    happens to wrap as speaking for the whole meeting. The Zoom-web connector shipped exactly
    that mistake twice (``speaker-active-container__wrap`` is layout, not state).

    ``speakerChurn`` in the page exists for this: it records which hooks appear and disappear
    between scans, so the marker can be identified from a log without having to catch a
    snapshot at the instant somebody was talking. Read the ``churn`` field of an
    ``observerIdle`` diagnostic naming ``speaker``.

    It degrades to no active speaker being reported, which costs attribution and not the
    meeting: the avatar still hears everyone, barge-in still fires from audio energy, and the
    interrupt source still names the only other person by elimination."""

    chat_panel_button: tuple[str, ...] = (
        "button[data-tid='chat-button']",
        "button[data-tid='toggle-chat']",
        "button[id='chat-button']",
        # Scoped to the calling toolbar for the reason ``participants_panel_button`` is: the
        # app rail's own "Chat" button navigates out of the meeting.
        "[data-tid='calling-toolbar'] button[aria-label*='chat' i]",
        "[role='toolbar'] button[aria-label*='chat' i]",
    )

    app_rail: tuple[str, ...] = (
        # Teams' left-hand navigation strip: Chat, Calendar, People, Communities, Calls.
        "[data-tid='app-bar']",
        "[data-tid='left-rail']",
        "[role='navigation']",
        "nav",
        "[class*='appBar' i]",
        "[class*='app-rail' i]",
    )
    """Containers whose buttons must **never** be clicked, whatever else matches them.

    **This is the guard that stops the connector walking itself out of the meeting**, and it
    exists because a live run did exactly that. The avatar joined, the page's panel observer
    looked for a "People" control, matched the *app rail's* navigation button rather than the
    calling toolbar's, clicked it — and Teams navigated the whole single-page app to the
    contacts page. The meeting was still live behind it (two presses of Back returned to it),
    but every observer was now reading a page with no meeting in it, and the tap had nothing
    to hear.

    An **exclusion** rather than a tighter positive match, because the thing worth being
    precise about is what must not be clicked. Which container holds the real toolbar is a
    guess that changes between builds; that app navigation is off-limits is permanent.

    ``openPanelOnce`` in the page skips any candidate whose ``closest()`` matches one of
    these, so an over-broad selector costs a miss rather than the session."""

    chat_container: tuple[str, ...] = (
        "[data-tid='chat-pane-list']",
        "[data-tid='chat-pane-message-list']",
        "[data-tid='chat-pane']",
        "[class*='chatPane' i]",
        "[aria-label*='chat message list' i]",
    )
    """The chat list element, which exists whether or not it holds any messages.

    **This is what tells "open and empty" apart from "not rendered yet", and getting it wrong
    swallows a real question.** An observer that arms on the first pass finding *content*
    leaves a panel that opened empty unarmed until somebody types — and then records that
    person's message as backlog and answers nothing. The observer reports itself armed, the
    avatar stays silent, and the log says a message was seen. The Zoom-web connector shipped
    that and had to fix it; ``panel_ready_timeout_ms`` is the fallback if every selector here
    is renamed, so a miss costs a delay rather than the feature."""

    caption_container: tuple[str, ...] = (
        "[data-tid='closed-caption-v2-window']",
        "[data-tid='closed-captions-renderer']",
        "[class*='closedCaption' i]",
        "[class*='captionsContainer' i]",
    )
    """The live-captions region, on the same terms as ``chat_container``.

    The empty case is more common here than for chat: when ``captions_auto_enable`` is on the
    avatar switches captions on itself, so the panel is empty at that exact moment and the
    first line transcribed is always genuinely new."""

    chat_item: tuple[str, ...] = (
        "[data-tid='chat-pane-message']",
        "[data-tid='chat-pane-item']",
        "[data-tid='messageBodyContainer']",
        "[class*='chatMessage' i]",
        "[aria-label*='chat message' i]",
    )

    chat_name: tuple[str, ...] = (
        "[data-tid='message-author-name']",
        "[data-tid='messageAuthorName']",
        "[class*='authorName' i]",
        "[class*='messageAuthor' i]",
    )

    chat_text: tuple[str, ...] = (
        "[data-tid='messageBodyContent']",
        "[data-tid='message-body-content']",
        "[class*='messageBody' i]",
        "div[id^='content-']",
    )

    captions_button: tuple[str, ...] = (
        # Teams has labelled this "Turn on live captions" and "Show live captions", and in
        # several builds it lives behind the "More" menu rather than on the toolbar — in which
        # case none of these match and the transcript is limited to captions somebody else
        # switched on. That is a known limit rather than a bug to hunt: reaching a control
        # inside a menu means opening the menu, which is a second visible action, and the
        # connector does not take one uninvited.
        "button[data-tid='closed-caption-button']",
        "button[data-tid='toggle-captions']",
        "button[aria-label*='live captions' i]",
        "button[aria-label*='captions' i]",
        "[role='menuitem'][aria-label*='captions' i]",
    )
    """The control that turns Teams' live captions on.

    **Clicking this is visible to everybody in the meeting**, which is why
    ``captions_auto_enable`` defaults to off and this is only consulted when it is on. It is
    also the only way this connector can answer "what did they say" at all."""

    caption_item: tuple[str, ...] = (
        "[data-tid='closed-caption-message']",
        "[data-tid='closed-caption-v2-window'] [data-tid='closed-caption-text']",
        "[class*='captionMessage' i]",
        "[class*='closedCaption' i] [class*='message' i]",
    )

    caption_name: tuple[str, ...] = (
        "[data-tid='author']",
        "[data-tid='caption-author']",
        "[class*='captionAuthor' i]",
        "[class*='author' i]",
    )

    caption_text: tuple[str, ...] = (
        "[data-tid='closed-caption-text']",
        "[class*='captionText' i]",
        "[class*='caption' i] [class*='text' i]",
    )


DEFAULT_OBSERVER_SELECTORS = TeamsObserverSelectors()
