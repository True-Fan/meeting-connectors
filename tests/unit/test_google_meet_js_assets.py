"""The browser assets, and their agreement with the Python codec.

**This is the highest-value test file in the connector.** ``js/bridge.js`` implements the
wire header independently of ``websocket/protocol.py``, in a different language, and the two
must agree byte for byte. Nothing else catches a divergence: a wrong magic constant or a
flipped endianness produces a session that joins successfully and then carries silence, or
a sheared video frame, with no error anywhere. The only place that can fail loudly is here.

Parity is checked by parsing the constants out of the JavaScript source and comparing them
to the Python ones, so the check survives the file being reformatted.
"""

from __future__ import annotations

import re

import pytest

from src.connectors.google_meet.js import (
    BRIDGE_ASSET,
    CAPTURE_WORKLET_ASSET,
    PLAYOUT_WORKLET_ASSET,
    load_assets,
    read_asset,
)
from src.connectors.google_meet.websocket.protocol import (
    AUDIO_HEADER_SIZE,
    HEADER_SIZE,
    MAGIC,
    VIDEO_HEADER_SIZE,
    WIRE_VERSION,
    MeetFlags,
    MeetMessageType,
    MeetState,
)


@pytest.fixture(scope="module")
def bridge_js() -> str:
    return read_asset(BRIDGE_ASSET)


@pytest.fixture(scope="module")
def bridge_code(bridge_js: str) -> str:
    """``bridge.js`` with its comments removed.

    Needed by the assertions about what the script *does*. The file explains itself at
    length, and several of those explanations mention the very things the code must not
    contain — the comment next to the diagnostics channel says "deliberately not
    console.log", which a naive text search reads as a violation. Checking the code rather
    than the prose is the distinction that matters.

    ``://`` is preserved so protocol-relative URLs are not mistaken for line comments.
    """
    without_blocks = re.sub(r"/\*.*?\*/", "", bridge_js, flags=re.DOTALL)
    return re.sub(r"(?<!:)//[^\n]*", "", without_blocks)


def _js_const(source: str, name: str) -> str:
    """Read a ``const NAME = value;`` out of the JavaScript."""
    match = re.search(rf"const {re.escape(name)}\s*=\s*([^;]+);", source)
    assert match is not None, f"{name} not found in bridge.js"
    return match.group(1).strip()


def _js_object_fields(source: str, name: str) -> dict[str, int]:
    """Read a flat ``const NAME = {{ A: 0x01, ... }};`` object out of the JavaScript."""
    match = re.search(rf"const {re.escape(name)} = \{{(.*?)\}};", source, re.DOTALL)
    assert match is not None, f"{name} not found in bridge.js"
    return {
        key: int(value, 0)
        for key, value in re.findall(r"(\w+)\s*:\s*(0x[0-9a-fA-F]+|\d+)", match.group(1))
    }


class TestAssetsExist:
    def test_all_three_load(self) -> None:
        assets = load_assets()
        assert assets.bridge
        assert assets.capture_worklet
        assert assets.playout_worklet

    def test_a_missing_asset_fails_loudly(self) -> None:
        """Injecting an empty script would present as a page that never connects."""
        with pytest.raises(FileNotFoundError, match="cannot run without its injected"):
            read_asset("no_such_asset.js")

    def test_the_worklets_register_the_processor_names_the_bridge_constructs(
        self, bridge_js: str
    ) -> None:
        assert "registerProcessor('mc-capture'" in read_asset(CAPTURE_WORKLET_ASSET)
        assert "registerProcessor('mc-playout'" in read_asset(PLAYOUT_WORKLET_ASSET)
        assert "'mc-capture'" in bridge_js
        assert "'mc-playout'" in bridge_js


class TestWireParity:
    """The JavaScript and the Python codec must describe the same bytes."""

    def test_magic_matches(self, bridge_js: str) -> None:
        assert int(_js_const(bridge_js, "MAGIC"), 0) == MAGIC

    def test_wire_version_matches(self, bridge_js: str) -> None:
        assert int(_js_const(bridge_js, "WIRE_VERSION"), 0) == WIRE_VERSION

    def test_header_sizes_match(self, bridge_js: str) -> None:
        assert int(_js_const(bridge_js, "HEADER_SIZE"), 0) == HEADER_SIZE
        assert int(_js_const(bridge_js, "AUDIO_HEADER_SIZE"), 0) == AUDIO_HEADER_SIZE
        assert int(_js_const(bridge_js, "VIDEO_HEADER_SIZE"), 0) == VIDEO_HEADER_SIZE

    def test_every_message_type_matches(self, bridge_js: str) -> None:
        js_types = _js_object_fields(bridge_js, "TYPE")
        python_types = {member.name: int(member) for member in MeetMessageType}
        assert js_types == python_types

    def test_every_flag_matches(self, bridge_js: str) -> None:
        js_flags = _js_object_fields(bridge_js, "FLAG")
        # ``__members__`` rather than iteration: an ``IntFlag`` omits its zero-valued member
        # when iterated, so ``NONE`` would be missing from the Python side and the
        # comparison would pass for the wrong reason.
        python_flags = {name: int(member) for name, member in MeetFlags.__members__.items()}
        assert js_flags == python_flags
        assert "NONE" in python_flags

    def test_the_sample_format_code_matches(self, bridge_js: str) -> None:
        """s16le is 1 on the wire; a mismatch would be read as an unknown format."""
        assert int(_js_const(bridge_js, "SAMPLE_FORMAT_S16LE"), 0) == 1

    def test_headers_are_written_big_endian(self, bridge_js: str) -> None:
        """``struct.Struct('>...')`` on one side; explicit ``false`` on the other.

        DataView defaults to big-endian only for some methods and little-endian for none, so
        the flag is passed explicitly on every call. A single omission would swap the byte
        order of one field.
        """
        # Matched to end of statement rather than to the first ``)``: the pts write wraps its
        # value in ``BigInt(Math.trunc(...))``, so a non-greedy paren match stops early and
        # never sees the endianness argument.
        writes = re.findall(r"view\.set(?:Uint|Int|BigInt)\d+\(.*?\);", bridge_js)
        multi_byte = [w for w in writes if not re.search(r"set(?:Uint|Int)8\(", w)]
        assert len(multi_byte) >= 4, f"expected multi-byte header writes, got {multi_byte}"
        for write in multi_byte:
            assert "false" in write, f"missing explicit big-endian flag: {write}"


class TestStateParity:
    def test_every_state_the_page_reports_is_a_known_domain_state(self, bridge_js: str) -> None:
        """A state Python cannot parse is logged and dropped, so the page would go unheard."""
        reported = set(re.findall(r"return '(\w+)';", bridge_js))
        known = {str(state) for state in MeetState}
        assert reported <= known, f"bridge.js reports unknown states: {reported - known}"

    def test_the_page_can_report_every_terminal_state(self, bridge_js: str) -> None:
        """If the page cannot say 'ejected', the connector would rejoin into a refusal."""
        reported = set(re.findall(r"return '(\w+)';", bridge_js))
        for state in (MeetState.DENIED, MeetState.EJECTED, MeetState.ENDED):
            assert str(state) in reported


class TestBridgeContract:
    """Properties of the injected script the Python side depends on."""

    def test_it_reads_its_configuration_from_the_injected_globals(self, bridge_js: str) -> None:
        assert "window.__MC_BRIDGE_CONFIG__" in bridge_js
        assert "window.__MC_BRIDGE_WORKLETS__" in bridge_js

    def test_it_installs_only_in_the_top_frame(self, bridge_js: str) -> None:
        """Meet renders into iframes, and one socket per frame would be rejected."""
        assert "window !== window.top" in bridge_js

    def test_it_guards_against_double_installation(self, bridge_js: str) -> None:
        assert "__MC_BRIDGE_INSTALLED__" in bridge_js

    def test_it_patches_getusermedia_and_permissions(self, bridge_js: str) -> None:
        """Meet renders 'camera blocked' and never calls getUserMedia without the latter."""
        assert "media.getUserMedia = " in bridge_js
        assert "navigator.permissions.query" in bridge_js

    def test_it_leaves_getdisplaymedia_alone(self, bridge_code: str) -> None:
        """Faking screen share would make Meet offer a feature that produces a grey box."""
        assert "getDisplayMedia = " not in bridge_code

    def test_it_taps_inbound_tracks_only(self, bridge_js: str) -> None:
        """Which is what makes echo structurally impossible rather than merely filtered."""
        assert "addEventListener('track'" in bridge_js
        assert "'audio'" in bridge_js

    def test_the_capture_context_is_built_at_the_configured_rate(self, bridge_js: str) -> None:
        """16 kHz there is what removes the need for a resampler anywhere in the repo."""
        assert "sampleRate: CONFIG.captureSampleRateHz" in bridge_js

    def test_video_frames_are_closed(self, bridge_js: str) -> None:
        """VideoFrames hold non-GC'd media memory; a miss leaks until the renderer dies."""
        assert "frame.close()" in bridge_js

    def test_the_page_does_not_reconnect_on_its_own(self, bridge_code: str) -> None:
        """Two peers both healing the same link is how you get a duplicate avatar."""
        onclose = re.search(r"socket\.onclose = \(\) => \{(.*?)\};", bridge_code, re.DOTALL)
        assert onclose is not None
        assert "new WebSocket" not in onclose.group(1)
        assert "connect()" not in onclose.group(1)

    def test_it_carries_no_logging(self, bridge_code: str) -> None:
        """The Chromium bridge holds no logging: it reports events and Python decides.

        Checked against the comment-stripped source, because the file explains *why* it does
        not log and that explanation names the calls it avoids.
        """
        for call in ("console.log", "console.error", "console.warn", "console.debug"):
            assert call not in bridge_code, f"bridge.js must not call {call}"

    def test_it_carries_no_metrics(self, bridge_code: str) -> None:
        """Frame accounting lives in the three Python adapters, where the frames are."""
        assert "performance.mark" not in bridge_code
        assert "navigator.sendBeacon" not in bridge_code


class TestScanCost:
    """What the DOM scans cost the media path.

    Everything this bridge does shares one thread with Meet's own WebRTC, the canvas that
    backs the avatar's camera track, and the `postMessage` that feeds the playout worklet.
    Work done here is not free and does not fail loudly — it is paid for in media that
    arrives late, which is what a participant hears as an avatar that answers slowly.
    """

    def test_the_page_text_is_read_once_per_scan(self, bridge_code: str) -> None:
        """``innerText`` forces a synchronous layout of the whole document and then
        serialises the text out of it. ``observedState`` used to call it up to four times per
        scan, on a scan driven by Meet's own mutations — hundreds of full-page reflows a
        second, for four checks that could share one read."""
        state_fn = bridge_code.split("function observedState()", 1)[1].split(
            "\n  function ", 1
        )[0]
        assert "innerText" not in state_fn, "observedState must not read the page itself"
        assert state_fn.count("bodyText()") == 1, "one read, however many checks consume it"
        assert bridge_code.count("document.body.innerText") == 1

    def test_the_scans_have_a_floor_between_them(self, bridge_code: str) -> None:
        """Coalescing per animation frame bounds the scans at Meet's repaint rate and no
        lower, and Meet mutates on essentially every frame — so they ran at ~60 Hz for the
        whole call, each pass laying out the document and reading ``innerText`` off every chat
        and participant row."""
        observers = bridge_code.split("function installObservers()", 1)[1]
        assert "CONFIG.scanThrottleMs" in observers
        assert "lastScanAt" in observers

    def test_a_scan_deferred_by_the_floor_still_happens(self, bridge_code: str) -> None:
        """Dropping a scan that arrives inside the window would mean a chat message typed
        during a burst of mutations waits for the *next* mutation — and in a still meeting
        the next one may be seconds away, which reads as an ignored question."""
        observers = bridge_code.split("function installObservers()", 1)[1]
        assert "trailing" in observers
        assert "setTimeout" in observers


class TestWorkletContract:
    def test_the_capture_worklet_batches_to_a_whole_frame(self) -> None:
        """Partial frames would be rejected by ``AudioFrame``'s own validation."""
        source = read_asset(CAPTURE_WORKLET_ASSET)
        assert "_frameSamples" in source
        assert "postMessage" in source

    def test_the_capture_worklet_scales_int16_asymmetrically(self) -> None:
        """int16 spans -32768..32767; a symmetric scale wraps -1.0 to positive."""
        source = read_asset(CAPTURE_WORKLET_ASSET)
        assert "0x8000" in source
        assert "0x7fff" in source

    def test_the_playout_worklet_never_stalls_the_graph(self) -> None:
        """Returning false would tear the node out and kill the microphone track for good.

        Scoped to ``process`` rather than to the file: it is the return value of *that* method
        that the graph acts on, and a predicate helper answering "is this block silent" with
        ``false`` is not the same statement at all.
        """
        source = read_asset(PLAYOUT_WORKLET_ASSET)
        process = source.split("  process(_inputs, outputs) {", 1)[1]
        assert "return false" not in process
        assert "return true" in process

    def test_the_playout_worklet_drops_oldest_on_overflow(self) -> None:
        """Python ahead of the sound card means latency is accumulating; stale audio goes."""
        source = read_asset(PLAYOUT_WORKLET_ASSET)
        assert "_dropped" in source
        assert "_underruns" in source

    def test_both_worklets_report_upward_rather_than_judging(self) -> None:
        """The bridge carries no metrics; it reports counters and Python decides."""
        playout = read_asset(PLAYOUT_WORKLET_ASSET)
        assert "type: 'stats'" in playout

    def test_the_playout_worklet_gives_accumulated_latency_back(self) -> None:
        """Overflow at capacity was the only thing that ever removed samples.

        Nothing corrects the two clocks either side of the ring, so every hiccup added to its
        standing depth and none of it came back: the buffer ratcheted towards its half-second
        capacity over a call and stayed there, which is half a second added to every reply
        with every health check still green.
        """
        source = read_asset(PLAYOUT_WORKLET_ASSET)
        assert "_trim()" in source
        assert "_target" in source

    def test_the_playout_worklet_only_ever_trims_silence(self) -> None:
        """A shortened pause is inaudible; a shortened word is a hole in speech.

        The trim stops at the first block carrying any sound, so a backlog behind a sentence
        still being spoken waits for the pause after it rather than eating into it.
        """
        source = read_asset(PLAYOUT_WORKLET_ASSET)
        assert "_silentAhead" in source
        assert "SILENCE_FLOOR" in source

    def test_the_playout_worklet_tests_a_block_rather_than_a_sample(self) -> None:
        """Speech crosses zero on every cycle, so a per-sample silence test would cut into a
        vowel at the first zero crossing it found."""
        source = read_asset(PLAYOUT_WORKLET_ASSET)
        assert "_trimBlock" in source
        assert "_silentAhead(block)" in source


class TestOutboundAudioEnforcement:
    """Putting the avatar's audio on the wire regardless of what Meet chose to publish.

    ``getUserMedia`` interception is necessary and not sufficient. It only works if Meet asks
    for a microphone, at a moment we can answer, and then publishes what we handed over —
    and Meet fails all three in the field: it may acquire a device before the patch installs,
    ask only for video, or route our track through its own processing graph and publish the
    result instead. In every case the avatar is inaudible while each upstream signal reads
    healthy: PCM delivered, worklet rendering, track live, microphone unmuted.

    The RTP sender is downstream of all of it and is what actually decides what the meeting
    hears, so that is what gets enforced. Verified in real Chromium over a loopback
    PeerConnection pair: with the sender carrying silence the remote peer measured rms 0.0,
    and after ``replaceTrack`` it measured rms 0.259 with the SDP unchanged and signalling
    still ``stable`` — so the swap costs no renegotiation.
    """

    def test_the_bridge_forces_audio_onto_the_sender(self) -> None:
        source = read_asset(BRIDGE_ASSET)
        assert "forceOutboundAudio" in source
        assert "replaceTrack" in source

    def test_it_reconciles_rather_than_running_once(self) -> None:
        """Meet's audio transceiver does not exist until negotiation, and Meet may swap the
        track again later; a single pass at construction would miss both."""
        source = read_asset(BRIDGE_ASSET)
        assert "superviseOutboundAudio" in source
        assert "setInterval" in source
        assert "negotiationneeded" in source

    def test_it_identifies_senders_by_transceiver_not_by_track(self) -> None:
        """A sender whose track is null — which is what Meet has while muted — carries no
        `kind` of its own, so the receiver's track has to supply it."""
        source = read_asset(BRIDGE_ASSET)
        assert "getTransceivers" in source
        assert "transceiver.receiver" in source

    def test_it_does_not_replace_its_own_track_repeatedly(self) -> None:
        """Without an identity check every pass would replace the track it just installed."""
        source = read_asset(BRIDGE_ASSET)
        assert "ourAudioTracks" in source

    def test_a_forced_track_starts_enabled(self) -> None:
        """Meet mutes by clearing `enabled`; a freshly installed track must start audible or
        the avatar stays silent until somebody toggles the button."""
        source = read_asset(BRIDGE_ASSET)
        assert "enabled = true" in source

    def test_the_outcome_is_observable_from_python(self) -> None:
        """Whether the avatar reached the wire must be a reading, not a deduction."""
        source = read_asset(BRIDGE_ASSET)
        assert "audioSendersForced" in source
        assert "audioSendersSeen" in source

    def test_the_enforcement_interval_is_configured_from_python(self) -> None:
        """Timing belongs in settings, like every other cadence this connector uses."""
        from src.connectors.google_meet.bridge.chromium_bridge import (
            AUDIO_ENFORCE_INTERVAL_MS,
        )

        assert AUDIO_ENFORCE_INTERVAL_MS > 0
        assert "audioEnforceIntervalMs" in read_asset(BRIDGE_ASSET)


class TestGraphBuildersRunOnce:
    """One media graph per page, whatever calls the builder.

    ``if (state.playoutContext) return;`` looks like a guard and is not one: the flag is
    assigned only after two awaits, so callers arriving in that window each build a complete
    graph — AudioContext, worklet node, destination stream. The last assignment wins and
    everything wired to the earlier graphs is orphaned, including the microphone track Meet is
    already holding. Measured in real Chromium: **four concurrent callers built four
    AudioContexts.**

    Both directions have this shape. ``ensureCapture`` is called once per remote audio track
    straight from the ``track`` event, so two participants arriving in one tick is enough;
    ``ensurePlayout`` became racy the moment outbound enforcement started calling it from an
    interval and from three connection events.

    Extra contexts are not merely wasteful — Chromium caps how many a document may hold, so
    the surplus can make a later ``new AudioContext()`` throw and take the *other* direction
    down with it.
    """

    def test_both_builders_memoise_their_promise(self) -> None:
        source = read_asset(BRIDGE_ASSET)
        assert "playoutBuild" in source
        assert "captureBuild" in source
        assert "buildPlayout" in source
        assert "buildCapture" in source

    def test_a_failed_build_can_be_retried(self) -> None:
        """A cached rejected promise would make one transient error permanent."""
        source = read_asset(BRIDGE_ASSET)
        assert "state.playoutBuild = null" in source
        assert "state.captureBuild = null" in source

    def test_the_outbound_track_is_cloned_once_not_per_pass(self) -> None:
        """A reconciliation loop that mints a clone every two seconds leaks a live
        MediaStreamTrack per tick for as long as Meet contests the sender."""
        source = read_asset(BRIDGE_ASSET)
        assert "ensureSendTrack" in source
        assert "state.sendTrack" in source

    def test_only_sending_transceivers_are_touched(self) -> None:
        """A recvonly transceiver is how another participant's voice arrives; it is not ours
        to put a track on, and skipping it keeps the enforcement to its own business.

        The predicate must gate on *direction* rather than on kind alone, so it is checked for
        both directions it accepts — a version testing only ``sendrecv`` would silently skip a
        sendonly m-line and leave the avatar mute.
        """
        source = read_asset(BRIDGE_ASSET)
        body = source.split("function sendsAudio(transceiver) {", 1)[1].split("\n  }", 1)[0]
        assert "direction" in body
        assert "'sendrecv'" in body
        assert "'sendonly'" in body


class TestChatCapture:
    """Reading the meeting's chat, so a typed question gets a spoken answer.

    Meet exposes no chat API to a participant, so the rendered panel is the only source. Two
    properties of that make or break the feature, and both are easy to get wrong in a way that
    fails silently — an avatar that simply never answers anything typed.
    """

    def test_the_page_opens_the_chat_panel(self, bridge_code: str) -> None:
        """With the panel closed a message is a transient popup that leaves nothing in the DOM,
        so without this click the feature reads nothing at all."""
        assert "ensureChatPanel" in bridge_code
        assert "chatOpenButton" in bridge_code

    def test_opening_is_bounded_by_time_not_by_scan_count(self, bridge_code: str) -> None:
        """The second bug that stopped chat working, after the pre-join one.

        A budget of ten *scans* sounds bounded and is not: scans are driven by DOM mutations, and
        Meet mutates continuously. Observed in a live session — ``attempt 5`` through
        ``attempt 10`` inside the same second and a half, then a permanent give-up, while Meet
        had not yet drawn the in-call control bar holding the button. A wall-clock window with a
        minimum gap between clicks is what the intent actually was.
        """
        from src.connectors.google_meet.bridge.chromium_bridge import (
            CHAT_OPEN_RETRY_MS,
            CHAT_OPEN_WINDOW_MS,
        )

        # Long enough for Meet to finish rendering, which is many seconds after "joined".
        assert CHAT_OPEN_WINDOW_MS >= 30_000
        assert CHAT_OPEN_RETRY_MS >= 500
        assert "chatOpenWindowMs" in bridge_code
        assert "chatOpenRetryMs" in bridge_code
        assert "chatOpenMaxAttempts" not in bridge_code, "the scan-count budget must be gone"

    def test_the_chat_button_is_also_found_by_its_label(self, bridge_code: str) -> None:
        """Meet's exact label has moved more than once ("Chat with everyone", "Chat",
        "Open chat"). A substring match on the rendered accessible name survives that; a list of
        equality selectors survives none of it — every one of four candidates missed in a live
        session, reported as ``clicked: False`` ten times."""
        assert "findChatButtonByLabel" in bridge_code

    def test_giving_up_reports_the_labels_actually_on_the_page(self, bridge_code: str) -> None:
        """So the next selector fix is a reading rather than a third guess."""
        assert "chatButtonLabels" in bridge_code
        assert "buttonsSeen" in bridge_code

    def test_history_is_baselined_rather_than_answered(self, bridge_code: str) -> None:
        """Opening the panel renders the whole backlog. Answering it would have the avatar
        reply to a conversation that happened before it joined."""
        assert "chatBaselined" in bridge_code

    def test_messages_are_deduplicated(self, bridge_code: str) -> None:
        """Meet re-renders the chat list on almost every DOM mutation, so without identity one
        question would be forwarded on every scan and answered repeatedly."""
        assert "chatSeen" in bridge_code
        assert "data-message-id" in bridge_code

    def test_chat_scanning_is_driven_by_the_existing_observer(self, bridge_code: str) -> None:
        """Reusing the coalesced mutation scan rather than adding a second timer — Meet mutates
        the DOM continuously and a per-mutation scan would spend more time in the DOM than in
        media."""
        assert "scanChat()" in bridge_code

    def test_chat_can_be_switched_off_from_python(self, bridge_code: str) -> None:
        """Opening the panel is a visible action on the avatar's own account, so it is a
        setting rather than unconditional behaviour."""
        assert "CONFIG.chatEnabled" in bridge_code

    def test_the_page_reports_authorship_but_does_not_act_on_it(self, bridge_code: str) -> None:
        """The page can see which row is ours; whether to answer it is policy, and policy lives
        in Python. So `isSelf` is reported and the filter is in AvatarClient.send_chat."""
        assert "isSelf" in bridge_code

    # The CHAT_MESSAGE wire value is already covered, and more strongly, by
    # ``TestWireParity.test_every_message_type_matches`` — it compares the whole table.

    def test_every_chat_selector_reaches_the_page(self) -> None:
        """A selector defined in Python but absent from `to_page_config` is dead code that
        looks live."""
        from src.connectors.google_meet.automation.selectors import DEFAULT_SELECTORS

        config = DEFAULT_SELECTORS.to_page_config()
        for key in ("chatOpenButton", "chatPanel", "chatMessage", "chatSender"):
            assert key in config, f"{key} never reaches bridge.js"
            assert config[key], f"{key} has no candidates"

    def test_chat_only_scans_once_admitted(self, bridge_code: str) -> None:
        """The bug that made chat never work at all, in any meeting.

        ``installObservers`` starts scanning at DOMContentLoaded — on the *pre-join* screen,
        where Meet has no chat button, because you cannot chat in a call you have not entered.
        Meet mutates the DOM continuously, so the ten-attempt budget was spent within seconds of
        the page loading and ``ensureChatPanel`` had given up permanently before there was ever a
        button to click. The budget is meant to bound attempts *in the call*.
        """
        assert "state.meetState !== 'joined'" in bridge_code
        assert "chatWasJoined" in bridge_code

    def test_giving_up_on_chat_is_reported(self, bridge_code: str) -> None:
        """Silence was the real defect: the only report fired on attempt 1, before the socket
        was open, so ``send`` dropped it and the feature failed with no line in any log."""
        assert "chatOpenGaveUp" in bridge_code
        assert "chatGaveUp" in bridge_code

    def test_the_open_budget_resets_on_rejoin(self, bridge_code: str) -> None:
        """A rejoin is a fresh call and deserves a fresh budget, or chat works exactly once."""
        assert "state.chatOpenAttempts = 0" in bridge_code

    def test_panel_detection_does_not_rely_on_a_generic_panel_attribute(self) -> None:
        """Meet reuses ``data-panel-id`` for the people and activities panels, so it matched with
        chat closed — the page reported the panel open, scanned, and found nothing, silently."""
        from src.connectors.google_meet.automation.selectors import DEFAULT_SELECTORS

        assert "div[data-panel-id]" not in DEFAULT_SELECTORS.chat_panel
        # The message-entry box exists only while chat is open, which is the strong signal.
        assert any("Send a message" in s for s in DEFAULT_SELECTORS.chat_panel)

    def test_the_history_window_is_timed_from_the_panel_opening(self, bridge_code: str) -> None:
        """The bug where the avatar ignored the *first* message anybody typed.

        Baselining meant "the first scan that finds any messages is history", and the scan
        returned early when the panel held none. With an empty panel the flag was therefore never
        set, so the user's opening message became the thing that got baselined away — replies
        began only from the second message.

        Timing the window from the panel opening fixes both cases at once: a real backlog renders
        inside it and is skipped, and an empty panel lets it lapse so the first real message is
        forwarded like any other.
        """
        from src.connectors.google_meet.bridge.chromium_bridge import CHAT_BASELINE_MS

        assert CHAT_BASELINE_MS > 0
        assert "chatBaselineUntil" in bridge_code
        assert "chatBaselineMs" in bridge_code
        # The flag must not be reachable only via a scan that found messages.
        assert "const baselining = !state.chatBaselined" not in bridge_code

    def test_an_empty_chat_panel_does_not_block_baselining(self, bridge_code: str) -> None:
        """The early return that caused it: with no message nodes the function bailed out before
        the flag was ever set, leaving the next message — the first real one — to be swallowed."""
        scan = bridge_code.split("function scanChat()", 1)[1].split("function scanRoster", 1)[0]
        assert "if (!nodes.length) {" not in scan, (
            "scanChat must not return early on an empty panel; that is what deferred the "
            "baseline onto the user's first message"
        )

    def test_message_identity_does_not_depend_on_position(self, bridge_code: str) -> None:
        """Meet recycles chat list nodes, so a shifted index turns an already-answered message
        into a new one and the avatar answers it twice. Position is the one part of a chat row
        guaranteed not to be stable."""
        assert "function chatMessageId(node, text)" in bridge_code
        assert "|${index}" not in bridge_code


class TestHandRaiseCapture:
    """Noticing that somebody wants to speak, so the avatar can stop and hand over.

    Meet exposes no hand-raise API either, so this is the DOM again — but the failure mode is
    the opposite of chat's. Chat fails silently by reading nothing; this fails *loudly*, by
    reading the same raised hand on every re-render and interrupting the avatar continuously.
    Everything here is about the edge.
    """

    def test_hand_scanning_is_driven_by_the_existing_observer(self, bridge_code: str) -> None:
        """Reusing the coalesced mutation scan rather than adding a third timer."""
        assert "scanHands()" in bridge_code

    def test_a_raised_hand_is_reported_on_its_edge_not_its_presence(
        self, bridge_code: str
    ) -> None:
        """Meet renders a hand as a state that persists until it is lowered, and re-renders it
        constantly. Reporting presence would send the same hand dozens of times a second, and
        every one of them would interrupt the avatar mid-sentence."""
        scan = bridge_code.split("function scanHands()", 1)[1].split("function scanRoster", 1)[0]
        assert "state.handsUp.has(candidate.key)" in scan, (
            "a hand already up must not be re-reported"
        )
        assert "state.handsUp.add(candidate.key)" in scan, (
            "a hand observed up must be recorded as up, so the next scan reads it as the "
            "same hand rather than as a new one"
        )

    def test_a_hand_still_up_is_recorded_before_the_gates_that_withhold_it(
        self, bridge_code: str
    ) -> None:
        """Baselining and the cooldown decide whether to *report* a hand, never whether it is
        up. Recording it only after those gates would leave a withheld hand looking unseen, so
        every later scan would read it as a fresh raise and the gate it just failed would be
        the only thing holding it back — which is a rate limit, not an edge.
        """
        scan = bridge_code.split("function scanHands()", 1)[1].split("function scanRoster", 1)[0]
        recorded = scan.index("state.handsUp.add(candidate.key)")
        assert recorded < scan.index("if (baselining)"), "recorded before the baseline gate"
        assert recorded < scan.index("cooldownMs && now - last"), "recorded before the cooldown"

    def test_a_hand_comes_down_only_after_a_grace_window_of_not_being_seen(
        self, bridge_code: str
    ) -> None:
        """**The fix for an avatar repeating "ok, go ahead" at a hand that never moved.**

        The set of raised hands used to be replaced wholesale by whatever the current scan
        found — and only the twice-a-second sweep finds anything, because Meet renders the
        evidence as text and icon glyphs rather than as attributes. Every scan in between
        emptied the set, so the next sweep re-reported the same unmoved hand as a new one and
        the cooldowns turned that into an interruption on a timer. Measured against this
        file's own scan loop: 118 interruptions from one hand held up for ten minutes.

        A hand therefore comes down when it has not been *seen* for a while, not when one scan
        missed it — a real raised hand does briefly leave the DOM when Meet re-renders a tile,
        closes the people panel, or scrolls a participant out of its virtualised list.
        """
        scan = bridge_code.split("function scanHands()", 1)[1].split("function scanRoster", 1)[0]
        assert "state.handsUp = current" not in scan, (
            "replacing the set from one scan is the bug: a scan that could not see a hand is "
            "not evidence that it came down"
        )
        assert "state.handsSeenAt.set(candidate.key, now)" in scan, (
            "a hand still up must refresh its timestamp, not merely be skipped"
        )
        assert "handRaiseDownGraceMs" in scan
        assert "state.handsUp.delete(key)" in scan

    def test_a_hand_is_only_retired_on_a_full_sweep(self, bridge_code: str) -> None:
        """The narrow selector pass that runs between sweeps matches nothing in the layouts
        Meet actually renders, so letting a partial scan age a hand out would reintroduce the
        same false edge the grace window exists to remove."""
        scan = bridge_code.split("function scanHands()", 1)[1].split("function scanRoster", 1)[0]
        # The last `if (sweep)` is the retirement pass; the first only stamps the sweep clock.
        retire = scan.rsplit("if (sweep) {", 1)
        assert len(retire) > 1, "retirement must be inside a sweep-only branch"
        assert "state.handsUp.delete(key)" in retire[1].split("\n    }", 1)[0]

    def test_a_hand_is_recognised_by_wording_rather_than_by_markup(
        self, bridge_code: str
    ) -> None:
        """The fix for a live meeting where the attribute selectors matched nothing at all —
        the same failure the chat button had, and the same remedy. Meet's class names and
        `jsname` attributes are build artefacts; the words it shows a human are the most
        durable thing on the page."""
        assert "HAND_TRIGGERS" in bridge_code
        assert "raised their hand" in bridge_code
        assert "'[aria-label], [data-tooltip]'" in bridge_code

    def test_the_text_of_the_page_is_read_and_not_only_its_labels(
        self, bridge_code: str
    ) -> None:
        """**The second live run is what this encodes.** With a participant's hand up, the
        page reported exactly one label containing "hand" — the avatar's own toolbar button.
        Meet marks the raised hand with an icon-font glyph on the tile, and an icon glyph is a
        *text node* holding the glyph's name. No label, no attribute, nothing a selector or a
        label sweep can reach.
        """
        assert "HAND_ICONS" in bridge_code
        assert "front_hand" in bridge_code
        assert "createTreeWalker" in bridge_code
        assert "NodeFilter.SHOW_TEXT" in bridge_code

    def test_the_text_walk_does_not_force_layout_and_is_bounded(
        self, bridge_code: str
    ) -> None:
        """It runs beside a live media path. ``innerText`` on the document would force a
        reflow every sweep, and an unbounded walk over a page Meet is still building shows up
        as dropped frames rather than as an error."""
        # ``bridge_code`` has its comments stripped, so the next ``function`` is the boundary.
        walk = bridge_code.split("function handTextWalk(", 1)[1].split("\n  function ", 1)[0]
        assert "innerText" not in walk
        assert "seen < 6000" in walk

    def test_the_icon_glyph_only_counts_inside_a_participant(self, bridge_code: str) -> None:
        """The identical glyph sits in our own "Raise hand" toolbar button, which exists in
        every meeting from the moment we join. Requiring a participant container is the whole
        difference between reading a raised hand and raising one at ourselves."""
        block = bridge_code.split("handTextWalk((text, parent) => {", 1)[1].split(
            "return found;", 1
        )[0]
        assert "HAND_ICONS.indexOf" in block
        assert "handResolve(parent, null, 'icon'" in block
        # handResolve returns null without a holder, which is what enforces it.
        resolve = bridge_code.split("function handResolve(", 1)[1].split("\n  function ", 1)[0]
        assert "if (!key) {" in resolve

    def test_the_controls_and_the_panel_heading_cannot_trigger_it(
        self, bridge_code: str
    ) -> None:
        """Each of these contains a trigger phrase as a substring and is present in every
        meeting: "Raise hand" is the toolbar control, "Lower hand" is what it becomes, and
        "Raised hands" is the people panel's heading. Matching any of them would interrupt the
        avatar continuously, for a hand nobody raised."""
        assert "HAND_EXCLUDE" in bridge_code
        for phrase in ("'raise hand'", "'lower hand'", "'raised hands'"):
            assert phrase in bridge_code, f"{phrase} must be excluded"

    def test_an_unattributable_trigger_is_skipped_rather_than_keyed(
        self, bridge_code: str
    ) -> None:
        """A label that reads like a raised hand but names nobody and sits in no participant
        would otherwise get a constant key, reappear on every scan, and interrupt forever. A
        missed hand is much cheaper than that."""
        block = bridge_code.split("function handResolve(", 1)[1].split("\n  function ", 1)[0]
        assert "if (!key) {" in block
        assert "'anonymous'" not in block

    def test_the_sweep_is_rate_limited_independently_of_the_scan(
        self, bridge_code: str
    ) -> None:
        """It reads every labelled element on the page, and the scan is driven by Meet's
        mutations — which never stop."""
        from src.connectors.google_meet.bridge.chromium_bridge import HAND_RAISE_SWEEP_MS

        assert HAND_RAISE_SWEEP_MS > 0
        assert "handRaiseSweepMs" in bridge_code
        assert "state.handsLastSweepAt" in bridge_code

    def test_finding_nothing_is_reported_with_the_labels_the_page_does_have(
        self, bridge_code: str
    ) -> None:
        """Silent failure is what made the first attempt useless in a live meeting: no hand
        event, no error, nothing to read. Guessing Meet's wording from the outside cost the
        chat button two rounds; reporting what is actually on the page replaces the next
        guess with a reading."""
        from src.connectors.google_meet.bridge.chromium_bridge import HAND_RAISE_DIAG_MS

        assert HAND_RAISE_DIAG_MS > 0
        assert "handRaiseNothingSeen" in bridge_code
        assert "labelsWithHand" in bridge_code
        # Bounded: a meeting where nobody raises a hand is the normal case.
        assert "state.handsDiagnostics < 4" in bridge_code

    def test_arming_is_announced_so_a_live_log_shows_the_feature_running(
        self, bridge_code: str
    ) -> None:
        """The first question when a raised hand produces nothing is whether the code is even
        installed, and a log with no line for it cannot answer that."""
        assert "'handsArmed'" in bridge_code

    def test_a_flickering_indicator_cannot_produce_a_burst(self, bridge_code: str) -> None:
        """A hand that momentarily disappears during a re-render would otherwise read as a
        lower followed by a raise, which is a new edge and a second interrupt."""
        from src.connectors.google_meet.bridge.chromium_bridge import HAND_RAISE_COOLDOWN_MS

        assert HAND_RAISE_COOLDOWN_MS > 0
        assert "handsLastSentAt" in bridge_code
        assert "handRaiseCooldownMs" in bridge_code

    def test_hands_already_up_on_arrival_are_not_interruptions(
        self, bridge_code: str
    ) -> None:
        """The avatar had not said anything yet, so there is nothing for them to interrupt.
        Without this the avatar's first act would be to yield the floor to everyone at once."""
        from src.connectors.google_meet.bridge.chromium_bridge import HAND_RAISE_BASELINE_MS

        assert HAND_RAISE_BASELINE_MS > 0
        assert "handsBaselineUntil" in bridge_code
        assert "handRaiseBaselineMs" in bridge_code

    def test_hands_are_only_scanned_once_admitted(self, bridge_code: str) -> None:
        """The pre-join screen has no participants, and arming there would spend the baseline
        window before anybody could raise anything — the bug chat had, not repeated."""
        scan = bridge_code.split("function scanHands()", 1)[1].split("function scanRoster", 1)[0]
        assert "state.meetState !== 'joined'" in scan

    def test_hand_raises_can_be_switched_off_from_python(self, bridge_code: str) -> None:
        """A meeting where the avatar should hold the floor — a presentation, a read-out."""
        assert "CONFIG.handRaiseEnabled" in bridge_code

    def test_the_page_reports_authorship_but_does_not_act_on_it(
        self, bridge_code: str
    ) -> None:
        """Same split as chat: the page can see whose row it is, and Python decides what that
        means. The self filter lives in ``MeetHandRaiseSource`` and ``send_interrupt``."""
        scan = bridge_code.split("function scanHands()", 1)[1].split("function scanRoster", 1)[0]
        assert "isSelf" in scan

    def test_the_hand_selectors_reach_the_page(self) -> None:
        """A selector defined in Python but absent from ``to_page_config`` is dead code that
        looks live."""
        from src.connectors.google_meet.automation.selectors import DEFAULT_SELECTORS

        config = DEFAULT_SELECTORS.to_page_config()
        assert config.get("handRaised"), "handRaised never reaches bridge.js"

    def test_the_selectors_match_a_raised_hand_and_not_the_raise_button(self) -> None:
        """Every meeting has a "Raise hand" button in its control bar. A selector that matched
        it would fire in every meeting, immediately, for a hand nobody raised — Meet's labels
        state the *action* on a control and the *state* on an indicator, which is the same
        asymmetry ``mute_toggle`` depends on."""
        from src.connectors.google_meet.automation.selectors import DEFAULT_SELECTORS

        for selector in DEFAULT_SELECTORS.hand_raised:
            lowered = selector.lower()
            assert '"raise hand' not in lowered, selector
            assert "*=\"raise " not in lowered, selector
