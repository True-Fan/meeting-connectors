"""TeamsAudioSource — the ``AudioSource`` port over the media sidecar link.

A view, not an owner. ``TeamsSidecarLink`` owns the connection, the join, and recovery
because Teams runs receive and send through one media session (see ``sidecar/link.py``);
this adapter exists so that ``MediaRouter`` — shared, platform-blind, already shipped
for Zoom — can consume Teams audio through the identical port it consumes RTMS audio
through.

Contrast with ``RtmsAudioSource``, which is a much larger object: it owns its own
WebSocket, handshake, keep-alive, and reconnect loop, because Zoom's ingest genuinely
is an independent integration. The asymmetry between these two files is not an
inconsistency — it *is* the difference between the two platforms, and flattening it
would mean inventing a fake ingest lifecycle for Teams that could not fail
independently of egress.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from src.connectors.teams.sidecar.link import TeamsSidecarLink
from src.domain.avatar import AVATAR_INPUT_FORMAT
from src.domain.health import ComponentHealth
from src.domain.media import AudioFormat, AudioFrame
from src.infrastructure.logging import get_logger
from src.services.media.queues import QueueClosedError

logger = get_logger(__name__)

COMPONENT_NAME = "teams_ingest"


class TeamsAudioSource:
    """Live Teams participant audio, delivered over the sidecar link."""

    __slots__ = ("_link",)

    def __init__(self, *, link: TeamsSidecarLink) -> None:
        self._link = link

    async def start(self) -> None:
        """No-op: the link's join already started the media flow.

        The port requires it, and honouring it as a no-op is the honest
        implementation — there is nothing for ingest to start on its own. Making this
        connect a second socket would be inventing an independence Teams does not have.
        """

    async def stop(self) -> None:
        """No-op: ``TeamsMeetingSession`` stops the link once, for both legs.

        Idempotent by construction. Stopping the link from here would tear down egress
        as a side effect of stopping ingest.
        """

    async def frames(self) -> AsyncIterator[AudioFrame]:
        """Yield participant audio until the link closes."""
        queue = self._link.audio_queue()
        while True:
            try:
                yield await queue.get()
            except QueueClosedError:
                return

    def health(self) -> ComponentHealth:
        """The link's health, renamed for this leg.

        Both legs report the same underlying state, which is the truth for a single
        media session: when the link is gone, neither direction is working.
        """
        link_health = self._link.health()
        return ComponentHealth(
            name=COMPONENT_NAME, state=link_health.state, detail=link_health.detail
        )

    @property
    def audio_format(self) -> AudioFormat:
        """What this source yields.

        Always ``AVATAR_INPUT_FORMAT``: the media platform is configured for ``Pcm16K``
        mono and ``ingest/mapping.py`` rejects anything else at the boundary, so this
        is a checked property rather than an assumption.
        """
        return AVATAR_INPUT_FORMAT
