"""Port conformance.

Verifies the four ports are structurally satisfiable and that a minimal stand-in
type-checks against them. This is what keeps the scope-down rule from doc 003 §0
honest: if a port cannot be implemented twice, it should not exist.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from src.domain.avatar import AvatarClientHello, AvatarServerHello
from src.domain.health import ComponentHealth
from src.domain.media import AudioFrame, MediaChunk, VideoFrame
from src.domain.meeting import MeetingContext, ParticipantRef
from src.protocols import AudioSource, AvatarTransport, MediaDecoder, MediaSink


class _StubAudioSource:
    async def start(self) -> None: ...
    async def stop(self) -> None: ...

    def frames(self) -> AsyncIterator[AudioFrame]:  # pragma: no cover - shape only
        raise NotImplementedError

    def health(self) -> ComponentHealth:
        return ComponentHealth.healthy("stub")


class _StubSink:
    async def start(self, meeting: MeetingContext) -> None: ...
    async def stop(self) -> None: ...
    async def publish_audio(self, frame: AudioFrame) -> None: ...
    async def publish_video(self, frame: VideoFrame) -> None: ...

    def health(self) -> ComponentHealth:
        return ComponentHealth.healthy("stub")

    def own_participant(self) -> ParticipantRef | None:
        return None


class _StubDecoder:
    async def start(self, init_segment: MediaChunk | None = None) -> None: ...
    async def stop(self) -> None: ...
    async def feed(self, chunk: MediaChunk) -> None: ...

    def video(self) -> AsyncIterator[VideoFrame]:  # pragma: no cover - shape only
        raise NotImplementedError

    def audio(self) -> AsyncIterator[AudioFrame]:  # pragma: no cover - shape only
        raise NotImplementedError

    def health(self) -> ComponentHealth:
        return ComponentHealth.healthy("stub")


class _StubAvatarTransport:
    async def connect(self, hello: AvatarClientHello) -> AvatarServerHello:
        return AvatarServerHello(protocol_version=hello.protocol_version)

    async def close(self) -> None: ...
    async def send_pcm(self, pcm: bytes) -> None: ...

    def chunks(self) -> AsyncIterator[MediaChunk]:  # pragma: no cover - shape only
        raise NotImplementedError

    def health(self) -> ComponentHealth:
        return ComponentHealth.healthy("stub")


def test_audio_source_is_satisfiable() -> None:
    source: AudioSource = _StubAudioSource()
    assert isinstance(source, AudioSource)


def test_media_sink_is_satisfiable() -> None:
    sink: MediaSink = _StubSink()
    assert isinstance(sink, MediaSink)


def test_media_decoder_is_satisfiable() -> None:
    decoder: MediaDecoder = _StubDecoder()
    assert isinstance(decoder, MediaDecoder)


def test_avatar_transport_is_satisfiable() -> None:
    transport: AvatarTransport = _StubAvatarTransport()
    assert isinstance(transport, AvatarTransport)


def test_incomplete_implementation_fails_the_check() -> None:
    """Guards against the runtime check being vacuous."""

    class Incomplete:
        async def start(self) -> None: ...

    assert not isinstance(Incomplete(), AudioSource)


def test_exactly_four_ports_are_exported() -> None:
    """Doc 003 §0: a protocol earns its place only with a second implementation."""
    import src.protocols as protocols

    assert set(protocols.__all__) == {
        "AudioSource",
        "AvatarTransport",
        "MediaDecoder",
        "MediaSink",
    }


class TestHealthModels:
    def test_report_worst_of_aggregation(self) -> None:
        from src.domain.health import ComponentState, HealthReport

        report = HealthReport(
            components=(
                ComponentHealth.healthy("rtms"),
                ComponentHealth.unhealthy("publisher", "uds eof"),
            )
        )
        assert report.state is ComponentState.UNHEALTHY
        assert report.component("publisher") is not None
        assert report.component("absent") is None

    def test_empty_report_is_healthy(self) -> None:
        from src.domain.health import ComponentState, HealthReport

        assert HealthReport().state is ComponentState.HEALTHY

    def test_unknown_outranks_degraded(self) -> None:
        """A component that has not started is more alarming than an impaired one."""
        from src.domain.health import ComponentState, HealthReport

        report = HealthReport(
            components=(
                ComponentHealth.unknown("publisher"),
                ComponentHealth("rtms", ComponentState.DEGRADED, "dropping"),
            )
        )
        assert report.state is ComponentState.UNKNOWN

    def test_is_serving(self) -> None:
        from src.domain.health import ComponentState

        assert ComponentState.HEALTHY.is_serving
        assert ComponentState.DEGRADED.is_serving
        assert not ComponentState.UNHEALTHY.is_serving
        assert not ComponentState.UNKNOWN.is_serving
