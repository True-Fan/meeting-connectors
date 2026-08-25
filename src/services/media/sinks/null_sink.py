"""NullSink — counts and times frames, discards payloads.

For load and latency testing: it makes the publish hop free, so a measurement isolates
the rest of the pipeline instead of the disk or the sidecar. Also the default sink in
tests that only care about *whether* frames flowed.
"""

from __future__ import annotations

from src.domain.health import ComponentHealth, ComponentState
from src.domain.media import AudioFrame, VideoFrame
from src.domain.meeting import MeetingContext, ParticipantRef


class NullSink:
    """``MediaSink`` that records arrivals and drops payloads."""

    __slots__ = (
        "_audio_frames",
        "_audio_pts",
        "_own_participant",
        "_started",
        "_video_frames",
        "_video_pts",
    )

    def __init__(self, *, own_participant: ParticipantRef | None = None) -> None:
        self._own_participant = own_participant
        self._started = False
        self._video_frames = 0
        self._audio_frames = 0
        self._video_pts: list[int] = []
        self._audio_pts: list[int] = []

    async def start(self, meeting: MeetingContext) -> None:  # noqa: ARG002
        self._started = True

    async def stop(self) -> None:
        self._started = False

    async def publish_video(self, frame: VideoFrame) -> None:
        self._video_frames += 1
        self._video_pts.append(frame.pts_us)

    async def publish_audio(self, frame: AudioFrame) -> None:
        self._audio_frames += 1
        self._audio_pts.append(frame.pts_us)

    def health(self) -> ComponentHealth:
        state = ComponentState.HEALTHY if self._started else ComponentState.UNKNOWN
        return ComponentHealth(name="null_sink", state=state)

    def own_participant(self) -> ParticipantRef | None:
        return self._own_participant

    # -- assertions for tests ---------------------------------------------

    @property
    def video_frames(self) -> int:
        return self._video_frames

    @property
    def audio_frames(self) -> int:
        return self._audio_frames

    @property
    def video_pts(self) -> tuple[int, ...]:
        return tuple(self._video_pts)

    @property
    def audio_pts(self) -> tuple[int, ...]:
        return tuple(self._audio_pts)

    def max_av_skew_us(self) -> int:
        """Largest gap between the newest audio and video PTS.

        The direct observable for whether the shared media clock is doing its job
        (doc 003 §7.5). Zero when either stream is absent.
        """
        if not self._video_pts or not self._audio_pts:
            return 0
        return abs(self._video_pts[-1] - self._audio_pts[-1])
