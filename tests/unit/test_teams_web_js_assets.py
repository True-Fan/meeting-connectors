"""The injected script, and its agreement with the Python side.

**This is the highest-value test file for the connector**, for the reason its Zoom-web and
Google Meet equivalents are: ``inject.js`` implements the audio wire header independently of
``page/protocol.py``, in a different language, and the two must agree byte for byte. A wrong
magic constant or a flipped field offset produces a session that joins successfully, reports
healthy, and carries silence — with no error anywhere. The only place that can fail loudly is
here.

The configuration surface is checked the same way and for the same reason. Every observer in
the page is switched on by a key in ``_page_bootstrap``, and a key the page reads under a name
Python does not send reads as ``undefined``, which is falsy, which is *the feature silently
off*. That failure is indistinguishable from a quiet meeting.

Constants are parsed out of the JavaScript rather than matched as text, so the checks survive
the file being reformatted.
"""

from __future__ import annotations

import json
import re
import struct

import pytest

from src.config.settings import Settings
from src.connectors.teams_web.config import TeamsWebConnectorConfig
from src.connectors.teams_web.js import capture_worklet, inject_script, playout_worklet
from src.connectors.teams_web.page.protocol import (
    HEADER_SIZE,
    KIND_AUDIO_CAPTURE,
    MAGIC,
    VERSION,
)
from src.domain.ids import CorrelationId, SessionId
from src.domain.meeting import MeetingContext, MeetingPlatform
from src.domain.session import SessionContext


@pytest.fixture(scope="module")
def inject_js() -> str:
    return inject_script()


@pytest.fixture(scope="module")
def inject_code(inject_js: str) -> str:
    """``inject.js`` with its comments stripped.

    The file explains itself at length and several explanations mention the very things the code
    must not contain — the media-element commentary discusses ``createMediaElementSource`` in
    order to reject it, which a naive text search reads as a violation. Checking the code rather
    than the prose is the distinction that matters.

    ``://`` is preserved so protocol-relative URLs are not mistaken for line comments.
    """
    without_blocks = re.sub(r"/\*.*?\*/", "", inject_js, flags=re.DOTALL)
    return re.sub(r"(?<!:)//[^\n]*", "", without_blocks)


def _bootstrap(**env: object) -> str:
    from src.connectors.teams_web.session.teams_web_session import TeamsWebSessionFactory
    from tests.fakes.meet_page import FakeBrowserDriver

    settings = Settings(
        teams_web={"enabled": True, **env},  # type: ignore[arg-type]
        _env_file=None,  # type: ignore[call-arg]
    )
    session = SessionContext(
        session_id=SessionId("ses_teamsweb00000000000000000000"),
        correlation_id=CorrelationId("cor_teamsweb00000000000000000000"),
        meeting=MeetingContext(
            platform=MeetingPlatform.TEAMS_WEB,
            meeting_number="281442953617",
            passcode="abc123",
            display_name="AI Avatar",
        ),
    )
    factory = TeamsWebSessionFactory(
        config=TeamsWebConnectorConfig.from_settings(settings),
        driver_override=FakeBrowserDriver(),
    )
    return factory.build(session)._page_bootstrap()


def _bootstrap_keys(**env: object) -> set[str]:
    """The keys ``_page_bootstrap`` actually puts on ``window.__mcTeamsConfig``."""
    source = _bootstrap(**env)
    match = re.search(r"window\.__mcTeamsConfig = (\{.*?\});\n", source, re.DOTALL)
    assert match is not None, "the bootstrap no longer assigns __mcTeamsConfig"
    return set(json.loads(match.group(1)))


# --------------------------------------------------------------------------- #
# Assets
# --------------------------------------------------------------------------- #


def test_every_asset_loads() -> None:
    assert inject_script()
    assert playout_worklet()
    assert capture_worklet()


def test_the_worklets_register_the_names_the_script_constructs(inject_js: str) -> None:
    """A rename on one side produces a page that builds no node and reports nothing."""
    assert "registerProcessor('mc-teams-capture'" in capture_worklet()
    assert "registerProcessor('mc-playout'" in playout_worklet()
    assert "'mc-teams-capture'" in inject_js
    assert "'mc-playout'" in inject_js


def test_the_capture_processor_is_named_apart_from_the_zoom_one() -> None:
    """Two connectors, two page contracts.

    Both scripts run in their own browser, so a collision is impossible in practice — but the
    names are how a stale asset is diagnosed from a log, and a shared one would make "which
    connector built this node" unanswerable.
    """
    from src.connectors.zoom_web.js import capture_worklet as zoom_capture

    assert "mc-teams-capture" in capture_worklet()
    assert "mc-teams-capture" not in zoom_capture()


# --------------------------------------------------------------------------- #
# The audio wire, implemented twice
# --------------------------------------------------------------------------- #


def test_the_page_writes_the_header_python_reads(inject_code: str) -> None:
    """Parsed out of the JavaScript, so a reformat cannot break the check.

    The page writes the four magic bytes one at a time because a ``DataView`` has no string
    setter; this asserts those four literals really are ``TWB1``, in order.
    """
    setters = re.findall(r"view\.setUint8\((\d+), (0x[0-9a-fA-F]+|\d+)\)", inject_code)
    written = {int(offset): int(value, 0) for offset, value in setters}

    assert bytes(written[i] for i in range(4)) == MAGIC
    assert written[4] == VERSION
    assert written[5] == KIND_AUDIO_CAPTURE

    header_bytes = re.search(r"const CAPTURE_HEADER_BYTES = (\d+);", inject_code)
    assert header_bytes is not None
    assert int(header_bytes.group(1)) == HEADER_SIZE


def test_the_page_writes_the_length_and_timestamp_at_python_s_offsets(
    inject_code: str,
) -> None:
    """``!4sBBHQI`` — reserved at 6, pts_us at 8, length at 16."""
    assert re.search(r"view\.setUint16\(6, 0\)", inject_code)
    assert re.search(r"view\.setBigUint64\(8,", inject_code)
    assert re.search(r"view\.setUint32\(16, bytes\)", inject_code)
    assert struct.calcsize("!4sBBHQI") == HEADER_SIZE


def test_the_magic_differs_from_the_zoom_web_one() -> None:
    """Independent codecs that happen to agree on a layout.

    A frame captured or logged in isolation has to say which connector produced it, and the
    magic is the only field that can.
    """
    from src.connectors.zoom_web.page.protocol import MAGIC as ZOOM_MAGIC

    assert MAGIC == b"TWB1"
    assert MAGIC != ZOOM_MAGIC


def test_the_capture_context_is_built_at_the_avatar_s_rate(inject_code: str) -> None:
    """16 kHz on the constructor is the entire resampling story.

    Web Audio downsamples in native code before the worklet sees a sample, which is why
    ``capture_worklet.js`` has no resampler and ``PageAudioSource`` asserts the format rather
    than converting it. A page that built a 48 kHz context would produce a chipmunk voice three
    services downstream.
    """
    assert re.search(r"new Ctx\(\{ sampleRate: 16000 \}\)", inject_code)


# --------------------------------------------------------------------------- #
# The tap
# --------------------------------------------------------------------------- #


def test_all_three_playout_paths_are_patched(inject_code: str) -> None:
    """Teams is expected to carry audio over WebRTC; all three are patched anyway.

    The property that holds across every transport a browser can use is that audio which will
    be *heard* must reach an ``AudioContext`` destination or a media element. Tapping there is
    what makes this indifferent to Teams changing its transport between releases — which is the
    failure the Zoom-web connector spent live meetings discovering.
    """
    assert "AudioNode" in inject_code and "prototype" in inject_code
    assert "HTMLMediaElement" in inject_code
    assert "RTCPeerConnection" in inject_code
    for name in (
        "installWebAudioTap",
        "installMediaElementTap",
        "installPeerConnectionTap",
    ):
        assert f"function {name}(" in inject_code


def test_the_tap_does_not_reroute_the_page_s_own_audio(inject_code: str) -> None:
    """``captureStream`` observes; ``createMediaElementSource`` reroutes.

    Routing an element through a Web Audio source node disconnects it from the speakers unless
    it is reconnected to a destination, and getting that wrong makes the meeting inaudible to
    anyone watching the avatar's browser.
    """
    assert "captureStream" in inject_code
    assert "createMediaElementSource" not in inject_code


def test_the_page_s_own_graph_is_left_intact(inject_code: str) -> None:
    """The patched ``connect`` must always perform the connection it intercepted.

    Adding a tap edge instead of the original one would silence the page.
    """
    assert re.search(r"const result = original\.apply\(this, arguments\);", inject_code)
    assert re.search(r"return result;", inject_code)


def test_the_capture_graph_terminates_at_a_destination(inject_code: str) -> None:
    """**A worklet whose output goes nowhere is not guaranteed to be pulled.**

    A capture node built with no outputs and left dangling gives the render loop no reason to
    run it: ``process`` is never called, no frame is ever posted, and *every other part of the
    connector reports healthy* — the avatar joins, is audible, and is deaf. Both sibling
    connectors found this independently.

    Zero gain because there is nothing to play it to, and on a host with speakers playing the
    conference aloud would create an acoustic loop.
    """
    build = re.search(
        r"async function buildCapture\(\) \{(.*?)\n  \}", inject_code, re.DOTALL
    )
    assert build is not None
    body = build.group(1)

    assert "numberOfOutputs: 1" in body, "a node with no outputs may never be pulled"
    assert "silence.gain.value = 0" in body
    assert "node.connect(silence)" in body
    assert "silence.connect(context.destination)" in body


def test_the_scripts_own_audio_graphs_are_excluded_from_the_tap(inject_code: str) -> None:
    """The capture context terminates at its own destination, which the tap watches.

    Without the exclusion the tap wires into itself: harmless to the sound, because the
    termination runs through a zero gain, and ruinous to the diagnostics — ``audioTapped`` is
    the line an operator is told to check first when the avatar is deaf, and it would then
    always be present.
    """
    assert inject_code.count("__mcOwn = true") == 2, "both own contexts must be marked"
    assert re.search(r"!context\.__mcOwn && destination === context\.destination", inject_code)


def test_the_avatar_s_microphone_never_reaches_the_tap(inject_code: str) -> None:
    """**The structural property the whole barge-in design rests on.**

    The tap fires on edges into ``context.destination``. The microphone graph connects only to a
    ``MediaStreamDestination``, so the avatar's own voice cannot enter the tap — which is what
    lets the echo gate stay open and energy barge-in work at all. A ``node.connect`` onto a real
    destination anywhere in the microphone path would create a feedback loop in which the agent
    answers its own sentences.
    """
    build = re.search(r"async function build\(\) \{(.*?)\n  \}", inject_code, re.DOTALL)
    assert build is not None
    body = build.group(1)
    assert "createMediaStreamDestination()" in body
    assert "connect(destination)" in body
    assert "context.destination" not in body


# --------------------------------------------------------------------------- #
# Observer shapes that were bugs elsewhere
# --------------------------------------------------------------------------- #


def test_chat_de_duplication_counts_copies_rather_than_remembering_content(
    inject_code: str,
) -> None:
    """**Two live bugs on the Zoom-web connector, opposite in effect, same root cause: a set.**

    Keyed on ``index + name + text``, a virtualised chat list renumbered untouched messages and
    the avatar re-answered the backlog aloud. Keyed on ``name + text`` in a set, re-sending an
    identical message did nothing at all — pasting the same question was ignored while retyping
    a different one worked.

    "Is this message new?" is not a boolean question. The panel shows N copies of a line and the
    avatar has answered M; anything past M is new. This pins the shape — a per-message count
    compared against a high-water mark — because the failure mode of getting it wrong is
    silence, and silence is what a chat observer looks like when it is working and nobody has
    typed.

    A shape assertion rather than a behavioural one: the logic lives in a page script driven by
    a DOM this suite has no way to build. What it can guarantee is that the structure which
    caused both bugs does not come back.
    """
    scan = re.search(r"function scanChat\(\) \{(.*?)\n  \}", inject_code, re.DOTALL)
    assert scan is not None
    body = scan.group(1)

    assert "occurrences" in body, "copies of a message must be counted, not merely seen"
    assert re.search(r"occurrences\.get\(key\)\s*\|\|\s*0\)\s*\+\s*1", body)
    assert "chatSeen(occurrence, key)" in body

    seen = re.search(r"function chatSeen\((.*?)\n  \}", inject_code, re.DOTALL)
    assert seen is not None
    assert "occurrence <= emitted" in seen.group(1), "the mark must be a count, not a flag"
    assert "state.chatEmitted" in seen.group(1)


def test_observers_take_their_baseline_when_the_panel_opens_not_on_first_message(
    inject_code: str,
) -> None:
    """**The bug that ate a participant's first question, elsewhere.**

    The baseline exists to stop the avatar reading a meeting's chat backlog aloud when it joins.
    Arming on the first pass that finds *content* leaves a panel that opened empty unarmed until
    somebody types — and then records that person's message as backlog and answers nothing. The
    observer reports itself armed, the avatar is silent, and the log says a message was seen.

    An empty result means either "open and nobody has typed" or "not rendered yet", and those
    demand opposite treatment. The container element separates them: it exists whether or not it
    holds children.
    """
    assert "function armWhenReady" in inject_code
    assert "function panelReady" in inject_code

    for scan, container in (
        ("scanChat", "chatContainerSelectors"),
        ("scanCaptions", "captionContainerSelectors"),
    ):
        body = re.search(rf"function {scan}\(\) \{{(.*?)\n  \}}", inject_code, re.DOTALL)
        assert body is not None, scan
        assert f"CONFIG.{container}" in body.group(1), f"{scan} must detect its container"
        # Nothing is emitted or recorded before the panel can be read: a message seen while the
        # panel is still rendering cannot be classified either way.
        assert re.search(r"if \(!state\.\w+Armed && !ready\) return;", body.group(1)), scan


def test_a_lowered_hand_is_reported_so_python_can_hold_the_state(inject_code: str) -> None:
    """**Without this, an unmoved hand is re-detected as a fresh raise.**

    The page's ``handsUp`` set is keyed on a name read out of a row Teams re-renders, and
    several frames run the observer independently — so a hand that stays up while its row
    disappears for longer than the grace window is retired in the page and detected as new on
    the next scan. The person has not moved and the avatar interrupts itself to say "ok, go
    ahead" again.

    The lower event is what lets ``TeamsMeetingObserver`` hold the authoritative state across
    every re-render, reload and frame.
    """
    assert re.search(r"send\(\{ type: 'handLower', id: key \}\)", inject_code)
    assert "handDownGraceMs" in inject_code


def test_a_bare_hand_token_is_never_matched_in_markup(inject_code: str) -> None:
    """**A correctness requirement, not tidiness.**

    The markup pass reads a participant row's ``innerHTML``, which contains the participant's
    *name* — and plenty of real names contain those four letters ("Chandra", "Handa", "Chand").
    Matching ``hand`` alone would raise a permanent false hand for anybody so named, and
    interrupt the avatar every cooldown for the whole meeting.
    """
    markup = re.search(r"const HAND_MARKUP = \[(.*?)\];", inject_code, re.DOTALL)
    assert markup is not None
    tokens = re.findall(r"'([^']+)'", markup.group(1))
    assert tokens, "the markup pass must have something to match"
    for token in tokens:
        assert token != "hand"
        # Every token has to carry a separator or a second word, so it cannot occur inside a
        # name.
        assert re.search(r"[-_/]", token) or len(token) > len("hand"), token


def test_the_local_raise_hand_control_is_excluded_by_label(inject_code: str) -> None:
    """The toolbar renders "Raise hand" and then "Lower hand" continuously.

    Without the exclusion list either would match a trigger phrase on every single scan, and the
    avatar would interrupt itself forever at its own control.
    """
    exclude = re.search(r"const HAND_EXCLUDE = \[(.*?)\];", inject_code, re.DOTALL)
    assert exclude is not None
    phrases = set(re.findall(r"'([^']+)'", exclude.group(1)))
    assert {"raise hand", "lower hand"} <= phrases

    # …and the exclusion must not swallow the real trigger, which is a superstring of one of
    # them. "hand raise" would, which is why it is not on the list.
    assert "hand raise" not in phrases
    triggers = re.search(r"const HAND_TRIGGERS = \[(.*?)\];", inject_code, re.DOTALL)
    assert triggers is not None
    for trigger in re.findall(r"'([^']+)'", triggers.group(1)):
        assert not any(phrase in trigger for phrase in phrases), trigger


# --------------------------------------------------------------------------- #
# Configuration parity
# --------------------------------------------------------------------------- #


def test_no_config_key_is_read_with_neither_a_sender_nor_a_default(
    inject_code: str,
) -> None:
    """The check that catches a silently disabled observer.

    A key the page reads under a name Python does not send is ``undefined`` — falsy — so the
    observer simply never runs, and a meeting with a broken key looks exactly like a meeting
    where nothing happened. Nothing else in the system fails, which is why this has to.

    **Two legitimate shapes, and the test allows both.** A key can be *sent* from
    ``_page_bootstrap``, or it can be read with an inline default (``CONFIG.handScanMs || 500``)
    — a page-side tuning knob deliberately not exposed as a setting. What must not exist is a
    key with neither, because that is a typo that reads as "switched off".
    """
    read = set(re.findall(r"CONFIG\.(\w+)", inject_code))
    defaulted = set(re.findall(r"CONFIG\.(\w+)\s*\|\|", inject_code))
    sent = _bootstrap_keys()

    orphans = sorted(read - defaulted - sent)
    assert not orphans, (
        f"these page config keys are neither sent by _page_bootstrap nor given an inline "
        f"default, so they silently read as undefined: {orphans}"
    )


def test_every_key_python_sends_is_read_under_that_exact_name(inject_code: str) -> None:
    """The reverse direction.

    A selector list Python sends and the page ignores is dead weight. A *switch* Python sends and
    the page ignores means the two disagree about whether a feature is running, and the operator
    believes Python.
    """
    read = set(re.findall(r"CONFIG\.(\w+)", inject_code))
    unread = sorted(key for key in _bootstrap_keys() if key not in read)
    assert not unread, f"_page_bootstrap sends keys the page never reads: {unread}"


def test_the_page_is_told_about_every_observer_the_session_can_run(
    inject_code: str,
) -> None:
    """An unread *selector* list is harmless; an unread *enable* flag means Python believes a
    feature is on and the page never runs it."""
    read = set(re.findall(r"CONFIG\.(\w+)", inject_code))
    for switch in (
        "rosterEnabled",
        "speakerEnabled",
        "chatEnabled",
        "captionsEnabled",
        "handRaiseEnabled",
        "captureWorkletSource",
    ):
        assert switch in read, f"{switch} is sent but never read by the page"


def test_the_bootstrap_is_valid_javascript_with_the_config_first() -> None:
    """The script reads ``window.__mcTeamsConfig`` at its first statement.

    Appending the config instead would leave every observer reading ``undefined`` on the one
    pass that matters — the patches run before Teams' own scripts, which is the whole reason
    they see anything.
    """
    source = _bootstrap()
    assert source.startswith("window.__mcTeamsConfig = {")
    assert source.index("window.__mcTeamsConfig") < source.index("(() => {")


def test_selectors_reach_the_page_as_data() -> None:
    """A Teams UI change is then an edit to one Python file, not an asset change.

    Every list is optional in the page and an unparseable entry is a miss, so a stale selector
    costs the signal it carried and nothing else.
    """
    source = _bootstrap()
    for key in (
        "rosterRowSelectors",
        "speakerMarkerSelectors",
        "chatItemSelectors",
        "captionItemSelectors",
        "handSelectors",
    ):
        assert f'"{key}": [' in source


def test_a_disabled_consumer_sends_an_empty_selector_list_rather_than_dropping_the_key() -> None:
    """Same keys either way.

    An omitted key and an empty one behave identically in the page, but only one of them lets
    the parity tests above mean anything — and only one lets an operator read the bootstrap and
    see that a panel is deliberately not being opened.
    """
    assert _bootstrap_keys() == _bootstrap_keys(chat_open_panel=False)
    source = _bootstrap(chat_open_panel=False)
    assert '"chatPanelSelectors": []' in source
    # Captions default to read-but-never-enabled, so the button list is empty until asked for.
    assert '"captionsButtonSelectors": []' in source
    assert '"captionsButtonSelectors": []' not in _bootstrap(captions_auto_enable=True)


# --------------------------------------------------------------------------- #
# Two live failures, pinned
# --------------------------------------------------------------------------- #


def test_the_device_list_advertises_a_microphone(inject_code: str) -> None:
    """**"Mic disconnected", from a live run, while `getUserMedia` worked perfectly.**

    Teams enumerates audio inputs to populate its device menu. Finding nothing it can select,
    it reports the microphone as disconnected in the call and publishes nothing — and the
    patched `getUserMedia` never gets the chance to matter. So the fake input has to be
    *visible*, not merely returned on request.
    """
    assert "enumerateDevices" in inject_code
    assert "'audioinput'" in inject_code or '"audioinput"' in inject_code


def test_the_real_devices_are_kept_and_no_output_is_faked(inject_code: str) -> None:
    """**Where this deliberately differs from the Google Meet bridge**, and the reason is the
    tap's position.

    That bridge replaces the device list with three fakes including an `audiooutput`, and can
    afford to: it taps inbound WebRTC transceivers, so nothing it needs depends on audio being
    played. This connector taps at *playout*. Told the only output is a device that does not
    exist, Teams could route the meeting to a sink that renders nothing — and the tap would go
    silent for a reason indistinguishable from a broken selector.

    A fake `videoinput` is excluded for a different reason: the avatar publishes no video, and
    advertising a camera would have Teams offer one that produces a grey rectangle.
    """
    assert "originalEnumerate()" in inject_code, "the real device list must be read"
    assert re.search(r"\[fake, \.\.\.devices\]", inject_code), "ours is appended, not swapped"
    assert "audiooutput" not in inject_code
    assert "videoinput" not in inject_code


def test_a_panel_toggle_is_never_clicked_inside_the_app_rail(inject_code: str) -> None:
    """**The bug that walked the avatar out of its own meeting.**

    Teams' left-hand app rail has "Chat" and "People" navigation buttons, and an unscoped
    `button[aria-label*='people' i]` matches one. A live run clicked it: the single-page app
    navigated to the contacts page, the meeting kept running behind it, and every observer was
    left reading a page with no meeting in it while the session reported healthy.

    An exclusion rather than a tighter positive match — which container holds the real toolbar
    changes between builds, but app navigation being off-limits is permanent.
    """
    assert "function inAppRail(" in inject_code
    assert "CONFIG.appRailSelectors" in inject_code

    for opener in ("openPanelOnce", "openParticipantsPanel"):
        body = re.search(rf"function {opener}\((.*?)\n  \}}", inject_code, re.DOTALL)
        assert body is not None, opener
        assert "inAppRail(el)" in body.group(1), f"{opener} must skip app-rail candidates"
        assert "if (!inMeeting()) return;" in body.group(1), (
            f"{opener} must not click before the call exists"
        )


def test_the_page_notices_and_recovers_from_losing_its_meeting(inject_code: str) -> None:
    """A session that navigated out of its own meeting is otherwise dead and silent.

    Bounded: only after a meeting has actually been seen, and only a couple of attempts — a
    page that never joined must not spend its session pressing Back. Two presses of Back
    restored the meeting in the live run, which is why this is worth attempting rather than
    only reporting.
    """
    assert "function guardMeetingNavigation(" in inject_code
    assert "CONFIG.meetingMarkerSelectors" in inject_code
    body = re.search(
        r"function guardMeetingNavigation\(\) \{(.*?)\n  \}", inject_code, re.DOTALL
    )
    assert body is not None
    assert "state.sawMeeting" in body.group(1), "recovery must require having been in a call"
    assert "maxRecoverAttempts" in body.group(1), "the attempts must be bounded"
    assert "history.back()" in body.group(1)
    # And it has to actually run on the observer timer, or it is decoration.
    assert "guardMeetingNavigation();" in inject_code


def test_the_panel_selectors_python_sends_are_toolbar_scoped_or_hooks() -> None:
    """No bare ``aria-label`` matcher may reach the page for a panel toggle.

    The page's app-rail exclusion is the second guard; this is the first. A selector that can
    only ever resolve to navigation should not be sent at all.
    """
    from src.connectors.teams_web.automation.selectors import (
        DEFAULT_HAND_SELECTORS,
        DEFAULT_OBSERVER_SELECTORS,
    )

    # **Only the two lists that collide with navigation.** Teams' app rail has "Chat" and
    # "People" buttons and nothing resembling captions, so ``captions_button`` is deliberately
    # left unscoped: the captions control lives in the More menu in several builds, and
    # scoping it to the toolbar would make it match nothing at all. The page's app-rail
    # exclusion still covers it.
    lists = (
        DEFAULT_OBSERVER_SELECTORS.chat_panel_button,
        DEFAULT_HAND_SELECTORS.participants_panel_button,
    )
    for selectors in lists:
        for selector in selectors:
            if "aria-label" not in selector:
                continue
            scoped = selector.startswith(("[data-tid=", "[role='toolbar']", "[role='menuitem']"))
            assert scoped, (
                f"{selector!r} matches by label alone, so it can resolve to Teams' app "
                "navigation and click the avatar out of the meeting"
            )


def test_the_socket_reconnects_after_it_closes(inject_code: str) -> None:
    """**The bug that made the avatar mute and deaf while reporting healthy.**

    The first version connected once at script start and `onclose` merely nulled the
    reference. Teams' join walks through a launcher, a pre-join screen and several short-lived
    frames, and somewhere in that churn the socket closed. The script stayed alive — it had
    already reported `handsArmed` — with nothing to call `connect()` again.

    Every symptom then hides: the avatar publishes into a socket nobody holds, tapped frames
    are dropped before they are framed, and *every diagnostic that would explain it travels
    over the same socket*. `first_audio_published attached_pages=0` was the entire evidence.
    """
    assert "function scheduleReconnect(" in inject_code
    close = re.search(r"socket\.onclose = \(\) => \{(.*?)\};", inject_code, re.DOTALL)
    assert close is not None
    assert "scheduleReconnect()" in close.group(1), "a closed socket must schedule a retry"

    # A construction that throws (a blocked origin, say) must also retry, not give up.
    connect = re.search(r"function connect\(\) \{(.*?)\n  \}", inject_code, re.DOTALL)
    assert connect is not None
    assert "scheduleReconnect()" in connect.group(1)
    assert "state.connectError" in connect.group(1), (
        "the reason must be recorded somewhere the probe can read it — report() cannot help, "
        "it needs the socket that just failed"
    )


def test_the_reconnect_backs_off_and_is_capped(inject_code: str) -> None:
    """Bounded delay rather than bounded attempts: a page that has lost its socket has nothing
    else to do, and the session may outlive any attempt count worth naming."""
    body = re.search(r"function scheduleReconnect\(\) \{(.*?)\n  \}", inject_code, re.DOTALL)
    assert body is not None
    assert "Math.pow(2," in body.group(1), "the delay must grow"
    assert "Math.min(" in body.group(1), "and be capped"
    assert "state.reconnectTimer !== null" in body.group(1), "one timer, not a storm of them"


def test_a_successful_open_resets_the_backoff(inject_code: str) -> None:
    """Reset on success rather than on attempt: a socket that flaps should back off, and one
    that reconnects cleanly should be quick again next time."""
    body = re.search(r"socket\.onopen = \(\) => \{(.*?)\};", inject_code, re.DOTALL)
    assert body is not None
    assert "state.reconnectAttempts = 0" in body.group(1)


def test_the_page_state_carries_what_the_probe_reads(inject_code: str) -> None:
    """The Python-side probe reads ``window.__mcTeamsMic`` directly, over a path that does not
    depend on the channel working — which is the whole point, since the channel is what fails.

    Pinned as a pair so a rename on one side cannot silently produce a probe that reports
    ``undefined`` for everything and reads as healthy.
    """
    from src.connectors.teams_web.session.teams_web_session import _PAGE_PROBE

    assert "window.__mcTeamsMic" in inject_code
    assert "window.__mcTeamsMic" in _PAGE_PROBE
    for field in re.findall(r"s\.(\w+)", _PAGE_PROBE):
        assert f"{field}:" in inject_code or f"state.{field}" in inject_code, (
            f"the probe reads s.{field}, which the page never sets"
        )


def test_the_channel_liveness_is_polled_and_not_only_listened_for(inject_code: str) -> None:
    """**The second live failure, and the reason events alone are not enough.**

    A probe of a joined page reported `socket_state=3` (CLOSED) with `connects=0`, `closes=0`
    and no constructor error — the socket had failed *before* `onopen`/`onclose` were attached,
    so neither ever ran and the retry they schedule was never armed. Chromium is entitled to
    fail a refused WebSocket synchronously; nothing promises a close event on a connection that
    never started.

    A handler that might not fire cannot be the only thing holding the channel open, so the
    state is inspected on a timer as well.
    """
    assert "function ensureConnected(" in inject_code
    body = re.search(r"function ensureConnected\(\) \{(.*?)\n  \}", inject_code, re.DOTALL)
    assert body is not None
    assert "readyState <= 1" in body.group(1), "anything past OPEN must be discarded"
    assert "state.staleSockets" in body.group(1), (
        "a socket found already dead never fired a close event, so it has to be counted "
        "separately — that count is how 'the handlers never ran' is told apart from 'the "
        "channel keeps dropping'"
    )

    # And it has to actually run on a timer of its own, not only on the observers' — the
    # observers can be switched off entirely, and the channel is not optional.
    assert "CONFIG.channelCheckMs" in inject_code
    assert inject_code.count("ensureConnected();") >= 2, (
        "the liveness check must run on its own timer as well as the observers' tick"
    )
