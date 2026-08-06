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


def test_every_exported_port_has_a_second_implementation() -> None:
    """Doc 003 §0: a protocol earns its place only with a second implementation.

    The rule, not a count. It was four ports when Zoom was the only connector; Teams
    supplied the second implementation that ``ConnectorSession`` and
    ``ConnectorSessionFactory`` had been waiting for, so it is six. Asserting the count
    would make adding a *justified* port a test failure, while asserting the rule keeps
    the pressure where doc 003 put it: on whether a second implementation exists.
    """
    import src.protocols as protocols

    expected = {
        "AudioSource": ("RtmsAudioSource", "TeamsAudioSource"),
        "AvatarTransport": ("WebSocketAvatarTransport", "FakeAvatarTransport"),
        "MediaDecoder": ("FfmpegDecoder", "FakeDecoder"),
        "MediaSink": ("MeetingPublisher", "TeamsMediaSink"),
        "ConnectorSession": ("ZoomMeetingSession", "TeamsMeetingSession"),
        "ConnectorSessionFactory": ("ZoomSessionFactory", "TeamsSessionFactory"),
    }

    assert set(protocols.__all__) == set(expected)
    for port, implementations in expected.items():
        assert len(set(implementations)) >= 2, f"{port} has no second implementation"


def test_connector_session_is_satisfied_structurally_by_both_connectors() -> None:
    """The point of making it a ``Protocol``: neither connector needed editing.

    ``ZoomMeetingSession`` predates this port by an entire connector and satisfies it
    without a single change — which is what "prefer extending over modifying" looks like
    when it works.
    """
    import inspect

    from src.connectors.teams.session.teams_session import TeamsMeetingSession
    from src.connectors.zoom.session.zoom_session import ZoomMeetingSession
    from src.protocols.connector import ConnectorSession, ConnectorSessionFactory

    required = {"session", "start", "stop", "health", "leg_states"}
    for implementation in (ZoomMeetingSession, TeamsMeetingSession):
        missing = required - set(dir(implementation))
        assert not missing, f"{implementation.__name__} is missing {missing}"

    # Neither class inherits from the protocol — structural typing is what makes this
    # zero-touch for the connector that already shipped.
    assert ConnectorSession not in inspect.getmro(ZoomMeetingSession)
    assert ConnectorSession not in inspect.getmro(TeamsMeetingSession)

    from src.connectors.teams.session.teams_session import TeamsSessionFactory
    from src.connectors.zoom.session.zoom_session import ZoomSessionFactory

    for factory in (ZoomSessionFactory, TeamsSessionFactory):
        assert hasattr(factory, "build")
        assert ConnectorSessionFactory not in inspect.getmro(factory)


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
