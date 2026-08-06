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
        """Returning false would tear the node out and kill the microphone track for good."""
        source = read_asset(PLAYOUT_WORKLET_ASSET)
        assert "return false" not in source
        assert "return true" in source

    def test_the_playout_worklet_drops_oldest_on_overflow(self) -> None:
        """Python ahead of the sound card means latency is accumulating; stale audio goes."""
        source = read_asset(PLAYOUT_WORKLET_ASSET)
        assert "_dropped" in source
        assert "_underruns" in source

    def test_both_worklets_report_upward_rather_than_judging(self) -> None:
        """The bridge carries no metrics; it reports counters and Python decides."""
        playout = read_asset(PLAYOUT_WORKLET_ASSET)
        assert "type: 'stats'" in playout
