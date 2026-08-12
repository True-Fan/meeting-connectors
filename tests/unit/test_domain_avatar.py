"""The fixed avatar contract and its protocol handshake."""

from __future__ import annotations

import pytest

from src.domain.avatar import (
    AVATAR_INPUT_FORMAT,
    AVATAR_OUTPUT_CONTAINER,
    AVATAR_PROTOCOL_VERSION,
    AvatarAudioSpec,
    AvatarClientHello,
    AvatarProtocolVersion,
    AvatarServerHello,
    check_handshake,
)
from src.domain.context import FrameContext
from src.domain.exceptions import AvatarProtocolMismatchError
from src.domain.media import ContainerFormat, SampleFormat


class TestFixedContract:
    def test_input_format_is_pcm_16k_mono(self) -> None:
        """The avatar's input is fixed: PCM, 16 kHz, mono."""
        assert AVATAR_INPUT_FORMAT.sample_rate_hz == 16_000
        assert AVATAR_INPUT_FORMAT.channels == 1
        assert AVATAR_INPUT_FORMAT.sample_format is SampleFormat.S16LE

    def test_output_is_fragmented_mp4(self) -> None:
        assert AVATAR_OUTPUT_CONTAINER is ContainerFormat.FRAGMENTED_MP4

    def test_rtms_native_format_matches_avatar_input(self) -> None:
        """Zero-resample ingest is a checked property, not a happy accident.

        RTMS is configured for L16 / SR_16K / MONO, so if the avatar contract and
        the RTMS subscription ever drift apart, this fails rather than silently
        inserting a resampler in the hot path.
        """
        from src.domain.media import AudioFormat

        rtms_native = AudioFormat(16_000, 1, SampleFormat.S16LE)
        assert rtms_native == AVATAR_INPUT_FORMAT


class TestAvatarProtocolVersion:
    def test_parse_roundtrip(self) -> None:
        assert AvatarProtocolVersion.parse("2.7") == AvatarProtocolVersion(2, 7)
        assert str(AvatarProtocolVersion(2, 7)) == "2.7"

    @pytest.mark.parametrize("raw", ["", "1", "1.2.3", "x.y", "1.", "-1.0"])
    def test_rejects_malformed(self, raw: str) -> None:
        with pytest.raises(ValueError, match="malformed"):
            AvatarProtocolVersion.parse(raw)

    def test_same_major_is_compatible(self) -> None:
        assert AvatarProtocolVersion(1, 0).is_compatible_with(AvatarProtocolVersion(1, 9))

    def test_different_major_is_incompatible(self) -> None:
        assert not AvatarProtocolVersion(1, 0).is_compatible_with(AvatarProtocolVersion(2, 0))

    def test_orders_by_major_then_minor(self) -> None:
        assert AvatarProtocolVersion(1, 2) < AvatarProtocolVersion(1, 10)
        assert AvatarProtocolVersion(1, 9) < AvatarProtocolVersion(2, 0)


class TestHandshake:
    def _hello(self, ctx: FrameContext) -> AvatarClientHello:
        return AvatarClientHello(session_id=ctx.session_id, correlation_id=ctx.correlation_id)

    def test_client_hello_defaults_to_the_fixed_contract(self, frame_ctx: FrameContext) -> None:
        hello = self._hello(frame_ctx)
        assert hello.protocol_version == str(AVATAR_PROTOCOL_VERSION)
        assert hello.audio == AvatarAudioSpec.from_domain(AVATAR_INPUT_FORMAT)
        assert hello.expects_container is AVATAR_OUTPUT_CONTAINER

    def test_client_hello_carries_identity(self, frame_ctx: FrameContext) -> None:
        """So the agent's logs can be correlated with ours for one conversation."""
        hello = self._hello(frame_ctx)
        assert hello.session_id == frame_ctx.session_id
        assert hello.correlation_id == frame_ctx.correlation_id

    def test_audio_spec_roundtrips_to_domain(self) -> None:
        spec = AvatarAudioSpec.from_domain(AVATAR_INPUT_FORMAT)
        assert spec.to_domain() == AVATAR_INPUT_FORMAT

    def test_matching_versions_negotiate(self, frame_ctx: FrameContext) -> None:
        negotiated = check_handshake(
            self._hello(frame_ctx), AvatarServerHello(protocol_version="1.0")
        )
        assert negotiated == AvatarProtocolVersion(1, 0)

    def test_negotiates_down_to_lower_minor(self, frame_ctx: FrameContext) -> None:
        """Neither side may assume a feature the other has not shipped."""
        hello = AvatarClientHello(
            protocol_version="1.5",
            session_id=frame_ctx.session_id,
            correlation_id=frame_ctx.correlation_id,
        )
        assert check_handshake(hello, AvatarServerHello(protocol_version="1.2")) == (
            AvatarProtocolVersion(1, 2)
        )

    def test_major_mismatch_is_rejected(self, frame_ctx: FrameContext) -> None:
        with pytest.raises(AvatarProtocolMismatchError, match="protocol mismatch"):
            check_handshake(self._hello(frame_ctx), AvatarServerHello(protocol_version="2.0"))

    def test_explicit_rejection_is_honoured(self, frame_ctx: FrameContext) -> None:
        with pytest.raises(AvatarProtocolMismatchError, match="at capacity"):
            check_handshake(
                self._hello(frame_ctx),
                AvatarServerHello(protocol_version="1.0", accepted=False, reason="at capacity"),
            )

    def test_unexpected_container_is_rejected(self, frame_ctx: FrameContext) -> None:
        """A non-fMP4 container cannot be decoded while streaming — fail at the
        handshake rather than three hops later on an undecodable stream."""
        reply = AvatarServerHello(protocol_version="1.0").model_copy(update={"container": "webm"})
        with pytest.raises(AvatarProtocolMismatchError):
            check_handshake(self._hello(frame_ctx), reply)

    def test_hello_serialises_to_json(self, frame_ctx: FrameContext) -> None:
        payload = self._hello(frame_ctx).model_dump_json()
        # Derived from the constant, not written out: the version is expected to rise as
        # additive frames are introduced, and a literal here turns every bump into a
        # failing test that says nothing about whether serialisation works.
        assert f'"protocol_version":"{AVATAR_PROTOCOL_VERSION}"' in payload
        assert frame_ctx.session_id in payload
