"""Every Google Meet DOM selector and UI string, in exactly one place.

**This module is the connector's single most fragile surface, and it is isolated for
that reason.** Meet's markup is machine-generated: class names are hashed and change
without notice, so no selector here can be trusted to survive a Meet release. What *can*
be relied on is a much smaller set of things — ARIA labels, which exist because Meet has
accessibility obligations, and the visible English strings in its dialogs.

So the strategy is deliberate rather than incidental:

1. **Prefer ARIA over structure.** ``[aria-label="Leave call"]`` outlives
   ``.uArJ5e.UQuaGc.kCyAyd``, because the former is a commitment to screen readers and
   the latter is a build artefact.
2. **Try several candidates per concept, in order.** Each field is a tuple, and callers
   take the first that matches. When Meet renames something, the old and new selectors
   coexist here for a release rather than the connector breaking the moment either one
   moves.
3. **Fall back to visible text for terminal states.** Being denied entry or removed from
   a call has no stable ARIA hook, but it always shows a sentence. Text matching is the
   weakest tool here and is used only where nothing better exists.

**Why these live in Python and are pushed into the page.** ``js/bridge.js`` receives this
set in its ``CONFIG`` and does nothing but apply it. That keeps the browser layer free of
judgement — it reports what matched, never what it means — and it makes adapting to a
Meet UI change a Python edit reviewable in a diff, rather than a change to an asset that
also happens to contain the media path.

Language note: ``text`` fields are matched case-insensitively against the page's rendered
text, so they only work when the profile's Google account is set to English.
``LOCALE_NOTE`` records the consequence, and ``browser/launcher.py`` pins the browser
locale so the assumption is enforced rather than hoped for.
"""

from __future__ import annotations

from dataclasses import dataclass, field

LOCALE_NOTE = (
    "Terminal-state detection matches English text, so the browser locale is pinned to "
    "en-US in browser/launcher.py. A profile whose Google account renders Meet in another "
    "language would still join and carry media, but 'denied' and 'ejected' would be "
    "misread as 'still joining' and the session would retry instead of failing."
)


@dataclass(frozen=True, slots=True)
class MeetSelectors:
    """Selector candidates for each concept the bridge needs to recognise."""

    # -- pre-join ----------------------------------------------------------
    name_input: tuple[str, ...] = (
        'input[aria-label="Your name"]',
        'input[placeholder="Your name"]',
        'input[type="text"][aria-label*="name" i]',
    )
    """Only shown when the profile is *not* signed in to Google. A signed-in profile
    joins under the account's own name, which is the supported deployment — so if this
    ever matches, it is a signal that the profile lost its session, not a field we
    expect to fill."""

    join_button: tuple[str, ...] = (
        '//button[contains(., "Join now")]',
        '//button[contains(., "Ask to join")]',
        '//button[contains(., "Join here too")]',
        '//button[contains(., "Switch here")]',
        'button[aria-label*="Join now" i]',
        'button[aria-label*="Ask to join" i]',
        'button[jsname="Qx7uuf"]',
    )
    """Harvested from the live pre-join screen, and every entry earned its place.

    **Text matches first, ``jsname`` last** — the reverse of how this was first written, and the
    reordering matters. ``Qx7uuf`` was observed on the *same-account* pre-join screen's "Join here
    too" button, and there is no guarantee Meet uses it for the primary button on every variant of
    that screen. A jsname that resolves to the wrong control gets **clicked**, which fails silently
    and confusingly; a text match that misses simply falls through to the next candidate and
    eventually produces a named error. Prefer the failure mode you can diagnose.

    ``contains()``, **not** exact text — which is the bug this replaced. Meet renders the
    button as a Material icon ligature immediately followed by the label, with no separator and
    no wrapping element, so its text reads ``"add_to_queueJoin here too"``. The previous
    selectors required ``span[text()="Join now"]`` to match exactly, so they could never fire:
    the connector reached the pre-join screen, failed to find a join button, and gave up.

    Four wordings, because Meet picks one from context: "Join now" when we may enter directly,
    "Ask to join" when the meeting gates admission, and "Join here too" / "Switch here" when the
    same account is already present in the call from another session."""

    dismiss_buttons: tuple[str, ...] = (
        'button[aria-label="Close"]',
        'button[aria-label="Dismiss"]',
        '//button[.//span[text()="Got it"]]',
        '//button[.//span[text()="Continue without microphone and camera"]]',
        '//button[.//span[text()="Dismiss"]]',
    )
    """Interstitials Meet shows before the join screen. Cleared best-effort: none is
    required to be present, and a missing one is not an error."""

    # -- in call -----------------------------------------------------------
    in_call: tuple[str, ...] = (
        'button[aria-label*="Leave call" i]',
        '[aria-label*="Leave call" i]',
        'button[aria-label*="Chat with everyone" i]',
        'button[aria-label*="Meeting tools" i]',
    )
    """Presence of any of these means we are in the conference.

    **``div[data-meeting-code]`` used to be in this list and was actively harmful.** It is
    present on the *pre-join* screen too, so ``MeetJoiner._press_join`` took its
    "already in the call" branch, never clicked Join, and then reported a successful join — a
    browser parked on the pre-join screen while the session claimed to be active.

    Every candidate here is a control that exists only once admitted: you cannot leave, chat, or
    open meeting tools in a call you have not entered. Verified against a live pre-join screen
    and a live in-call screen — the leave button matches in the second and not the first, which
    is the property that makes it usable."""

    leave: tuple[str, ...] = (
        'button[aria-label*="Leave call" i]',
        'button[aria-label*="End call" i]',
    )

    mute_toggle: tuple[str, ...] = (
        'button[aria-label*="Turn off microphone" i]',
        'button[data-is-muted="false"][aria-label*="microphone" i]',
    )
    """Matches only while *unmuted*, because Meet's label states the action rather than
    the state. That asymmetry is load-bearing: the selector matching *is* the answer to
    "are we currently unmuted", so no separate state read is needed."""

    unmute_toggle: tuple[str, ...] = (
        'button[aria-label*="Turn on microphone" i]',
        'button[data-is-muted="true"][aria-label*="microphone" i]',
    )

    camera_on_toggle: tuple[str, ...] = (
        'button[aria-label*="Turn on camera" i]',
        'button[data-is-muted="true"][aria-label*="camera" i]',
    )

    camera_off_toggle: tuple[str, ...] = (
        'button[aria-label*="Turn off camera" i]',
        'button[data-is-muted="false"][aria-label*="camera" i]',
    )

    lobby: tuple[str, ...] = ('[aria-label*="Asking to join" i]',)

    participant: tuple[str, ...] = (
        "[data-participant-id]",
        "[data-requested-participant-id]",
        'div[role="listitem"][aria-label]',
    )

    # -- chat --------------------------------------------------------------
    chat_open_button: tuple[str, ...] = (
        'button[aria-label*="Chat with everyone" i]',
        'button[aria-label="Chat" i]',
        'button[aria-label*="Open chat" i]',
        'button[jsname="A5il2e"]',
    )
    """Opens the chat panel, which has to be open for messages to exist in the DOM at all.

    Meet renders chat history only inside the panel; with it closed a message appears as a
    transient popup and is gone. So this is not a convenience — without clicking it the chat
    feature reads nothing, and the avatar would silently ignore every typed question.

    The ``jsname`` candidate last, and knowingly: it is a build artefact and the least durable
    thing in this file. It is here because the ARIA label for this control has changed more
    than once, so a structural fallback buys a release of grace."""

    chat_panel: tuple[str, ...] = (
        'textarea[aria-label*="Send a message" i]',
        'textarea[aria-label*="Send a message to everyone" i]',
        'div[aria-label*="Chat with everyone" i]',
        'div[role="region"][aria-label*="chat" i]',
    )
    """The open panel. Matching it is how the page knows the click worked, rather than assuming
    it did and then scraping nothing.

    **The message-entry box first, and deliberately.** It exists only while the chat panel is
    open, which makes it the strongest available proof. A generic ``div[data-panel-id]`` was
    tried here and removed: Meet uses the same attribute for the people and activities panels, so
    it matched with chat *closed* — the page would report the panel open, scan, find no messages,
    and report nothing wrong. A false positive here is worse than a miss, because a miss is now
    reported as ``chatOpenGaveUp``."""

    chat_message: tuple[str, ...] = (
        "div[data-message-id]",
        'div[jsname="dTKtvb"]',
        'div[role="listitem"] div[jsname="dTKtvb"]',
    )
    """One rendered message. ``data-message-id`` first because it is both a selector *and* the
    dedupe key — Meet re-renders the list constantly, and identity is what stops one message
    being answered five times."""

    chat_sender: tuple[str, ...] = (
        "div[data-sender-name]",
        "[data-sender-name]",
        'div[jsname="dTKtvb"] + div',
    )
    """Attribution, read from the message's own subtree. Optional: an unattributed question is
    still worth answering, so a miss here degrades to ``sender=None`` rather than dropping."""

    # -- terminal states, by visible text ---------------------------------
    lobby_text: tuple[str, ...] = ("asking to join", "waiting for someone to let you in")
    denied_text: tuple[str, ...] = (
        "you can't join this",
        "no one responded to your request",
        "denied your request",
        "your request to join was denied",
    )
    ejected_text: tuple[str, ...] = ("you've been removed", "you were removed from the meeting")
    ended_text: tuple[str, ...] = (
        "the meeting has ended",
        "this meeting has ended",
        "you left the meeting",
        "return to home screen",
    )
    sign_in_text: tuple[str, ...] = ("sign in to join", "sign in with your google account")
    """Distinguishes "the profile lost its Google session" from "this meeting rejected
    us". Both leave us outside the call, but the first is fatal and fixed by
    re-authenticating the profile, while the second is about this meeting alone."""

    def to_page_config(self) -> dict[str, list[str]]:
        """The subset ``js/bridge.js`` needs, as plain JSON-serialisable lists.

        Only the observation and command selectors cross over. The pre-join ones stay in
        Python because Playwright drives that flow, and it can wait for a selector to
        appear — which is the whole difficulty of joining, and something the injected
        script has no good way to do.
        """
        return {
            "inCall": list(self.in_call),
            "lobby": list(self.lobby),
            "leave": list(self.leave),
            "participant": list(self.participant),
            "chatOpenButton": list(self.chat_open_button),
            "chatPanel": list(self.chat_panel),
            "chatMessage": list(self.chat_message),
            "chatSender": list(self.chat_sender),
            "lobbyText": list(self.lobby_text),
            "deniedText": list(self.denied_text),
            "ejectedText": list(self.ejected_text),
            "endedText": list(self.ended_text),
        }


DEFAULT_SELECTORS = MeetSelectors()
"""The set the connector uses. A field rather than a constant on the config so that a
Meet UI change can be patched by constructing a different ``MeetSelectors`` and passing
it to the driver — no fork, no release."""


@dataclass(frozen=True, slots=True)
class SelectorCandidates:
    """A named group of selector candidates, for error messages worth reading."""

    label: str
    candidates: tuple[str, ...] = field(default_factory=tuple)

    def describe(self) -> str:
        return f"{self.label} (tried: {', '.join(self.candidates) or 'nothing'})"
