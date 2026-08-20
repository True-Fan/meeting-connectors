"""The injected script, and its agreement with the Python side.

**This is the highest-value test file for browser ingest**, for the reason the Google Meet
equivalent is: ``inject.js`` implements the audio wire header independently of
``page/protocol.py``, in a different language, and the two must agree byte for byte. A wrong
magic constant or a flipped field offset produces a session that joins successfully, reports
healthy, and carries silence — with no error anywhere. The only place that can fail loudly
is here.

The configuration surface is checked the same way and for the same reason. Every observer in
the page is switched on by a key in ``_page_bootstrap``, and a key the page reads under a
name Python does not send reads as ``undefined``, which is falsy, which is *the feature
silently off*. That failure is indistinguishable from a quiet meeting.

Constants are parsed out of the JavaScript rather than matched as text, so the checks survive
the file being reformatted.
"""

from __future__ import annotations

import json
import re
import struct

import pytest

from src.config.settings import Settings
from src.connectors.zoom_web.config import ZoomWebConnectorConfig
from src.connectors.zoom_web.js import capture_worklet, inject_script, playout_worklet
from src.connectors.zoom_web.page.protocol import (
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

    The file explains itself at length and several explanations mention the very things the
    code must not contain — the audio-tap commentary discusses ``createMediaElementSource``
    in order to reject it, which a naive text search reads as a violation. Checking the code
    rather than the prose is the distinction that matters.

    ``://`` is preserved so protocol-relative URLs are not mistaken for line comments.
    """
    without_blocks = re.sub(r"/\*.*?\*/", "", inject_js, flags=re.DOTALL)
    return re.sub(r"(?<!:)//[^\n]*", "", without_blocks)


def _bootstrap(**env: object) -> str:
    from src.connectors.zoom_web.session.zoom_web_session import ZoomWebSessionFactory
    from tests.fakes.meet_page import FakeBrowserDriver

    settings = Settings(
        zoom_web={"enabled": True, **env},  # type: ignore[arg-type]
        _env_file=None,  # type: ignore[call-arg]
    )
    session = SessionContext(
        session_id=SessionId("ses_zoomweb000000000000000000000"),
        correlation_id=CorrelationId("cor_zoomweb000000000000000000000"),
        meeting=MeetingContext(
            platform=MeetingPlatform.ZOOM_WEB,
            meeting_number="94241716923",
            passcode="139601",
            display_name="AI Avatar",
        ),
    )
    factory = ZoomWebSessionFactory(
        config=ZoomWebConnectorConfig.from_settings(settings),
        driver_override=FakeBrowserDriver(),
    )
    return factory.build(session)._page_bootstrap()


def _bootstrap_keys(**env: object) -> set[str]:
    """The keys ``_page_bootstrap`` actually puts on ``window.__mcZoomConfig``."""
    source = _bootstrap(**env)
    match = re.search(r"window\.__mcZoomConfig = (\{.*?\});\n", source, re.DOTALL)
    assert match is not None, "the bootstrap no longer assigns __mcZoomConfig"
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
    assert "registerProcessor('mc-zoom-capture'" in capture_worklet()
    assert "registerProcessor('mc-playout'" in playout_worklet()
    assert "'mc-zoom-capture'" in inject_js
    assert "'mc-playout'" in inject_js


# --------------------------------------------------------------------------- #
# The audio wire, implemented twice
# --------------------------------------------------------------------------- #


def test_the_page_writes_the_header_python_reads(inject_code: str) -> None:
    """Parsed out of the JavaScript, so a reformat cannot break the check.

    The page writes the four magic bytes one at a time because a ``DataView`` has no string
    setter; this asserts those four literals really are ``ZWB1``, in order.
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


def test_the_capture_context_is_built_at_the_avatar_s_rate(inject_code: str) -> None:
    """16 kHz on the constructor is the entire resampling story.

    Web Audio downsamples in native code before the worklet sees a sample, which is why
    ``capture_worklet.js`` has no resampler and ``PageAudioSource`` asserts the format
    rather than converting it. A page that built a 48 kHz context would produce a chipmunk
    voice three services downstream.
    """
    assert re.search(r"new Ctx\(\{ sampleRate: 16000 \}\)", inject_code)


# --------------------------------------------------------------------------- #
# The tap
# --------------------------------------------------------------------------- #


def test_all_three_playout_paths_are_patched(inject_code: str) -> None:
    """A peer-connection tap alone finds nothing on Zoom — doc 009 §3.

    Zoom's web client decodes audio in WebAssembly and renders it through Web Audio in its
    long-standing mode, with no inbound audio transceiver on the page at all. The tap is
    placed at playout, which both modes must converge on, and covers every way a browser can
    render audio.
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

    Routing an element through a Web Audio source node disconnects it from the speakers
    unless it is reconnected to a destination, and getting that wrong makes the meeting
    inaudible to anyone watching the avatar's browser.
    """
    assert "captureStream" in inject_code
    assert "createMediaElementSource" not in inject_code


def test_zoom_s_own_graph_is_left_intact(inject_code: str) -> None:
    """The patched ``connect`` must always perform the connection it intercepted.

    Adding a tap edge instead of the original one would silence the page.
    """
    assert re.search(r"const result = original\.apply\(this, arguments\);", inject_code)
    assert re.search(r"return result;", inject_code)


def test_chat_de_duplication_counts_copies_rather_than_remembering_content(
    inject_code: str,
) -> None:
    """**Two live bugs, opposite in effect, same root cause: a set.**

    Keyed on ``index + name + text``, Zoom's virtualised list renumbered untouched messages
    and the avatar re-answered the backlog aloud. Keyed on ``name + text`` in a set, re-sending
    an identical message did nothing at all — reported from a meeting where pasting the same
    question was ignored and retyping a different one worked.

    "Is this message new?" is not a boolean question. The panel shows N copies of a line and
    the avatar has answered M; anything past M is new. This pins the shape — a per-message
    count compared against a high-water mark — because the failure mode of getting it wrong
    is silence, and silence is what a chat observer looks like when it is working and nobody
    has typed.

    A shape assertion rather than a behavioural one: the logic lives in a page script driven
    by a DOM this suite has no way to build. What it can guarantee is that the structure which
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
    """**The bug that ate a participant's first question.**

    The baseline exists to stop the avatar reading a meeting's chat backlog aloud when it
    joins. The old rule armed on the first pass that found *content* — so a panel that opened
    empty stayed unarmed until somebody typed, then recorded that person's message as backlog
    and answered nothing. The observer reported itself armed (``observerArmed existing: 1``),
    the avatar was silent, and the log said a message had been seen.

    An empty result means either "open and nobody has typed" or "not rendered yet", and those
    demand opposite treatment. The container element separates them: it exists whether or not
    it holds children. The backlog worth suppressing is whatever is present *when the panel
    opens* — if that is nothing, nothing is suppressed.

    Pinned as a shape for the reason the chat-key test is: the failure is silence, which is
    indistinguishable from a meeting where nobody typed.
    """
    assert "function armOnFirstSight" not in inject_code, "the rule that swallowed a message"
    assert "function armWhenReady" in inject_code
    assert "function panelReady" in inject_code

    for scan, container in (
        ("scanChat", "chatContainerSelectors"),
        ("scanCaptions", "captionContainerSelectors"),
    ):
        body = re.search(rf"function {scan}\(\) \{{(.*?)\n  \}}", inject_code, re.DOTALL)
        assert body is not None, scan
        assert f"CONFIG.{container}" in body.group(1), f"{scan} must detect its container"
        # Nothing is emitted or recorded before the panel can be read: a message seen while
        # the panel is still rendering cannot be classified either way.
        assert re.search(r"if \(!state\.\w+Armed && !ready\) return;", body.group(1)), scan


def test_the_capture_graph_terminates_at_a_destination(inject_code: str) -> None:
    """**A worklet whose output goes nowhere is not guaranteed to be pulled.**

    Found while writing this: the capture node was built with ``numberOfOutputs: 0`` and
    left dangling, so the render loop had no reason to run it. ``process`` is then never
    called, no frame is ever posted, and *every other part of the connector reports healthy*
    — the avatar joins, is audible, and is deaf. The Google Meet bridge terminates its
    capture graph the same way, having found the same thing.

    Zero gain because there is nothing to play it to, and on a host with speakers, playing
    the conference aloud would create an acoustic loop.
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


def test_the_scripts_own_audio_graphs_are_excluded_from_the_tap(
    inject_code: str,
) -> None:
    """The capture context terminates at its own destination, which the tap watches.

    Without the exclusion the tap wires into itself: harmless to the sound, because the
    termination runs through a zero gain, and ruinous to the diagnostics — ``audioTapped``
    is the line an operator is told to check first when the avatar is deaf, and it would
    then always be present.
    """
    assert inject_code.count("__mcOwn = true") == 2, "both own contexts must be marked"
    assert re.search(r"!context\.__mcOwn && destination === context\.destination", inject_code)


def test_the_avatar_s_microphone_never_reaches_the_tap(inject_code: str) -> None:
    """**The structural property the whole barge-in design rests on** (doc 009 §4).

    The tap fires on edges into ``context.destination``. The microphone graph connects only
    to a ``MediaStreamDestination``, so the avatar's own voice cannot enter the tap — which
    is what lets the echo gate stay open and energy barge-in work at all. A ``node.connect``
    onto a real destination anywhere in the microphone path would create a feedback loop in
    which the agent answers its own sentences.
    """
    build = re.search(r"async function build\(\) \{(.*?)\n  \}", inject_code, re.DOTALL)
    assert build is not None
    body = build.group(1)
    assert "createMediaStreamDestination()" in body
    assert "connect(destination)" in body
    assert "context.destination" not in body


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
    ``_page_bootstrap``, or it can be read with an inline default (``CONFIG.handScanMs ||
    500``) — a page-side tuning knob deliberately not exposed as a setting. What must not
    exist is a key with neither, because that is a typo that reads as "switched off".
    """
    read = set(re.findall(r"CONFIG\.(\w+)", inject_code))
    defaulted = set(re.findall(r"CONFIG\.(\w+)\s*\|\|", inject_code))
    sent = _bootstrap_keys()

    orphans = sorted(read - defaulted - sent)
    assert not orphans, (
        f"these page config keys are neither sent by _page_bootstrap nor given an inline "
        f"default, so they silently read as undefined: {orphans}"
    )


def test_every_switch_python_sends_is_read_under_that_exact_name(
    inject_code: str,
) -> None:
    """The reverse direction, restricted to keys where a mismatch is silent and harmful.

    A selector list Python sends and the page ignores is dead weight. A *switch* Python
    sends and the page ignores means the two disagree about whether a feature is running,
    and the operator believes Python.
    """
    read = set(re.findall(r"CONFIG\.(\w+)", inject_code))
    unread = sorted(key for key in _bootstrap_keys() if key not in read)
    assert not unread, f"_page_bootstrap sends keys the page never reads: {unread}"


def test_the_page_is_told_about_every_observer_the_session_can_run(
    inject_code: str,
) -> None:
    """The other direction, restricted to the switches.

    An unread *selector* list is harmless, but an unread *enable* flag means Python believes
    a feature is on and the page never runs it.
    """
    read = set(re.findall(r"CONFIG\.(\w+)", inject_code))
    for switch in (
        "ingestMode",
        "rosterEnabled",
        "speakerEnabled",
        "chatEnabled",
        "captionsEnabled",
        "handRaiseEnabled",
        "captureWorkletSource",
    ):
        assert switch in read, f"{switch} is sent but never read by the page"


def test_the_bootstrap_is_valid_javascript_with_the_config_first() -> None:
    """The script reads ``window.__mcZoomConfig`` at its first statement.

    Appending the config instead would leave every observer reading ``undefined`` on the one
    pass that matters — the patches run before Zoom's own scripts, which is the whole reason
    they see anything.
    """
    source = _bootstrap()
    assert source.startswith("window.__mcZoomConfig = {")
    assert source.index("window.__mcZoomConfig") < source.index("(() => {")


def test_selectors_reach_the_page_as_data() -> None:
    """A Zoom UI change is then an edit to one Python file, not an asset change.

    Every list is optional in the page and an unparseable entry is a miss, so a stale
    selector costs the signal it carried and nothing else.
    """
    source = _bootstrap()
    for key in (
        "rosterRowSelectors",
        "speakerMarkerSelectors",
        "chatItemSelectors",
        "captionItemSelectors",
    ):
        assert f'"{key}": [' in source


def test_rtms_mode_sends_the_switches_off_rather_than_omitting_them() -> None:
    """Same keys either way.

    An omitted key and a false one behave identically in the page, but only one of them lets
    the parity test above mean anything — and only one lets an operator read the bootstrap
    and see that a feature is off rather than absent.
    """
    assert _bootstrap_keys() == _bootstrap_keys(ingest_mode="rtms")
