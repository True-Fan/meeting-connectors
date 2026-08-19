"""The avatar must not hear itself.

**A real failure, reproduced.** RTMS returns every participant in the meeting,
including the avatar, so its own published voice came straight back. The transcriber
turned it into fragments of Portuguese and Hungarian, the agent answered them, and
the interruption detector fired on its own speech — a conversation with nobody, from
an operator who had not said a word.

``EchoGuard`` could not have caught it: it matches on ``user_id``, and the browser
never tells us our own Zoom participant id, so its identity filter was inert and it
fell through to a speaking gate whose hangover exists for barge-in, not for masking
seconds of the avatar talking.

The name is the identity we *do* have, and these tests pin the two halves of getting
that right: our own audio is dropped completely, and everyone else's — including a
participant talking over the avatar — still gets through.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from src.connectors.zoom_web.audio_capture.self_filter import SelfAudioFilter
from src.domain.avatar import AVATAR_INPUT_FORMAT
from src.domain.context import FrameContext
from src.domain.health import ComponentHealth, ComponentState
from src.domain.ids import CorrelationId, SessionId
from src.domain.media import AudioFrame
from src.domain.meeting import ParticipantRef

PCM = bytes(640)


def _ctx() -> FrameContext:
    return FrameContext(
        session_id=SessionId("ses_zwself0000000000000000000000"),
        correlation_id=CorrelationId("cor_zwself0000000000000000000000"),
    )


def _frame(name: str | None, user_id: int | None = 1) -> AudioFrame:
    participant = (
        ParticipantRef(user_id=user_id, display_name=name) if name is not None else None
    )
    return AudioFrame(
        pcm=PCM,
        pts_us=0,
        format=AVATAR_INPUT_FORMAT,
        ctx=_ctx(),
        participant=participant,
    )


class ScriptedSource:
    def __init__(self, frames: list[AudioFrame]) -> None:
        self._frames = frames
        self.started = False

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None: ...

    async def frames(self) -> AsyncIterator[AudioFrame]:
        for frame in self._frames:
            yield frame

    def health(self) -> ComponentHealth:
        return ComponentHealth(
            name="rtms_ingest", state=ComponentState.HEALTHY, detail="received=3"
        )


def _filtered(frames: list[AudioFrame], display_name: str = "AI Avatar"):
    return SelfAudioFilter(inner=ScriptedSource(frames), display_name=display_name)


async def test_the_avatars_own_audio_is_dropped() -> None:
    """The echo loop that made the agent answer itself."""
    source = _filtered([_frame("AI Avatar"), _frame("AI Avatar")])

    assert [f async for f in source.frames()] == []
    assert source.suppressed == 2


async def test_other_participants_are_forwarded() -> None:
    source = _filtered([_frame("Dev Choudhary"), _frame("Ada")])

    assert len([f async for f in source.frames()]) == 2
    assert source.suppressed == 0


async def test_a_participant_talking_over_the_avatar_still_gets_through() -> None:
    """The property a timing-based gate would break: barge-in must survive.

    Matching on identity rather than on "the avatar spoke recently" is what keeps a
    real interruption audible.
    """
    source = _filtered(
        [_frame("AI Avatar"), _frame("Dev Choudhary"), _frame("AI Avatar")]
    )

    forwarded = [f async for f in source.frames()]

    assert len(forwarded) == 1
    assert forwarded[0].participant is not None
    assert forwarded[0].participant.display_name == "Dev Choudhary"


async def test_matching_ignores_case_and_padding() -> None:
    """Zoom echoes the name back as typed, and it has carried whitespace."""
    source = _filtered([_frame("  ai avatar  ")], display_name="AI Avatar")

    assert [f async for f in source.frames()] == []
    assert source.suppressed == 1


async def test_unattributed_audio_is_forwarded_not_dropped() -> None:
    """A mixed stream carries no name.

    Dropping everything nameless would make the agent deaf rather than merely
    echo-free — a worse failure than the one being fixed.
    """
    source = _filtered([_frame(None)])

    assert len([f async for f in source.frames()]) == 1
    assert source.suppressed == 0


async def test_a_participant_whose_name_merely_contains_ours_is_kept() -> None:
    """Exact match, not substring: "AI Avatar Fan" is a different person."""
    source = _filtered([_frame("AI Avatar Fan")])

    assert len([f async for f in source.frames()]) == 1


async def test_health_reports_what_was_suppressed() -> None:
    """Zero suppressed while the avatar speaks means the echo loop is live again."""
    source = _filtered([_frame("AI Avatar"), _frame("Dev")])
    [f async for f in source.frames()]

    health = source.health()

    assert health.state is ComponentState.HEALTHY
    assert "self_suppressed=1" in (health.detail or "")
    assert "received=3" in (health.detail or "")  # the wrapped source's own detail


async def test_lifecycle_is_delegated() -> None:
    inner = ScriptedSource([])
    source = SelfAudioFilter(inner=inner, display_name="AI Avatar")

    await source.start()

    assert inner.started
