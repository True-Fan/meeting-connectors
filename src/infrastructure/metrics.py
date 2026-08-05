"""In-process metrics.

Doc 003 §0.1 cut the event bus, so metrics are recorded by a direct call. That is
sound because ``observe``/``increment`` are pure in-memory arithmetic on the event
loop thread — no lock, no I/O, no await — so a call from the media hot path costs
microseconds and cannot introduce latency or backpressure.

**A note on cardinality, since the requirement is that every metric carries the
session and correlation id.** Both are recorded, and per-session snapshots retain
them — that is what makes single-session debugging possible. But ``correlation_id``
is unbounded over time, so exporting it as a time-series label would be a
cardinality explosion in any Prometheus-style backend. The resolution:

* **recorded** — every sample is attributed to its session and correlation id;
* **exported at** ``/metrics`` — aggregated across sessions, ``session_id`` label
  optional and off by default, ``correlation_id`` never a label;
* **exported at** ``/metrics/sessions/{id}`` — full per-session detail including
  the correlation id.

So the requirement holds where it is useful (attribution and debugging) without the
foot-gun it would otherwise create in the scrape path.

Percentiles come from a bounded ring buffer per series rather than fixed histogram
buckets: sessions are few and short-lived, so exact percentiles over the last N
samples are both cheaper and more accurate than bucket interpolation.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final

from src.domain.context import FrameContext
from src.domain.ids import CorrelationId, SessionId
from src.infrastructure.context import current_correlation_id, current_session_id

DEFAULT_HISTOGRAM_CAPACITY: Final = 4096


class MetricName(StrEnum):
    """Every metric the bridge records.

    An enum rather than free strings so a typo is a failed import instead of a
    silently orphaned series.
    """

    # --- latency, microseconds (doc 003 §7.5) ---
    INGEST_TO_ROUTER_US = "ingest_to_router_us"
    ROUTER_TO_AVATAR_US = "router_to_avatar_us"
    AVATAR_RTT_US = "avatar_rtt_us"
    DECODE_US = "decode_us"
    PACE_WAIT_US = "pace_wait_us"
    PUBLISH_US = "publish_us"
    SIDECAR_IPC_US = "sidecar_ipc_us"
    WEBSOCKET_SEND_US = "websocket_send_us"
    END_TO_END_US = "end_to_end_us"

    # --- synchronisation ---
    AUDIO_DELAY_US = "audio_delay_us"
    VIDEO_DELAY_US = "video_delay_us"
    AV_SKEW_US = "av_skew_us"
    """Direct observable for whether the shared media clock is working. Zoom's
    send-audio and send-video are separate paths with documented desync risk
    (doc 001 §7.1); this is how we know rather than assume."""

    # --- counters ---
    FRAMES_RECEIVED_TOTAL = "frames_received_total"
    FRAMES_PUBLISHED_TOTAL = "frames_published_total"
    FRAMES_DROPPED_TOTAL = "frames_dropped_total"
    ECHO_FRAMES_SUPPRESSED_TOTAL = "echo_frames_suppressed_total"
    RECONNECTS_TOTAL = "reconnects_total"
    DECODER_RESTARTS_TOTAL = "decoder_restarts_total"
    IDLE_FRAMES_PUBLISHED_TOTAL = "idle_frames_published_total"
    SESSIONS_STARTED_TOTAL = "sessions_started_total"
    SESSIONS_FAILED_TOTAL = "sessions_failed_total"

    @property
    def is_histogram(self) -> bool:
        return self.value.endswith("_us")


_Labels = tuple[tuple[str, str], ...]


def _normalise(labels: Mapping[str, str] | None) -> _Labels:
    if not labels:
        return ()
    return tuple(sorted((str(k), str(v)) for k, v in labels.items()))


@dataclass(frozen=True, slots=True)
class HistogramSnapshot:
    """Computed statistics for one latency series."""

    count: int
    sum: float
    min: float
    max: float
    p50: float
    p95: float
    p99: float

    @property
    def mean(self) -> float:
        return self.sum / self.count if self.count else 0.0


class LatencyHistogram:
    """A bounded-window latency series.

    Retains the most recent ``capacity`` samples in a ring buffer. ``count`` and
    ``sum`` are lifetime totals; percentiles describe the window.
    """

    __slots__ = ("_buf", "_capacity", "_count", "_idx", "_max", "_min", "_sum")

    def __init__(self, capacity: int = DEFAULT_HISTOGRAM_CAPACITY) -> None:
        if capacity < 1:
            raise ValueError("capacity must be positive")
        self._capacity = capacity
        self._buf: list[float] = []
        self._idx = 0
        self._count = 0
        self._sum = 0.0
        self._min = float("inf")
        self._max = float("-inf")

    def observe(self, value: float) -> None:
        """Record one sample. Hot path — no allocation once warm."""
        self._count += 1
        self._sum += value
        if value < self._min:
            self._min = value
        if value > self._max:
            self._max = value
        if len(self._buf) < self._capacity:
            self._buf.append(value)
        else:
            self._buf[self._idx] = value
            self._idx = (self._idx + 1) % self._capacity

    def snapshot(self) -> HistogramSnapshot:
        """Compute statistics. Sorting happens here, never on ``observe``."""
        if not self._buf:
            return HistogramSnapshot(0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
        ordered = sorted(self._buf)
        return HistogramSnapshot(
            count=self._count,
            sum=self._sum,
            min=self._min,
            max=self._max,
            p50=_percentile(ordered, 0.50),
            p95=_percentile(ordered, 0.95),
            p99=_percentile(ordered, 0.99),
        )

    @property
    def count(self) -> int:
        return self._count


def _percentile(ordered: list[float], q: float) -> float:
    """Nearest-rank percentile of a pre-sorted list."""
    if not ordered:
        return 0.0
    idx = round(q * (len(ordered) - 1))
    return ordered[min(max(idx, 0), len(ordered) - 1)]


@dataclass(slots=True)
class SessionMetrics:
    """Metrics attributed to one session, retaining its correlation id."""

    session_id: SessionId
    correlation_id: CorrelationId | None = None
    histograms: dict[tuple[MetricName, _Labels], LatencyHistogram] = field(default_factory=dict)
    counters: dict[tuple[MetricName, _Labels], int] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class MetricsSnapshot:
    """A point-in-time read of the collector."""

    histograms: Mapping[tuple[MetricName, _Labels], HistogramSnapshot]
    counters: Mapping[tuple[MetricName, _Labels], int]
    session_ids: tuple[SessionId, ...] = ()


class MetricsCollector:
    """Records latency samples and counters.

    Not a global. One instance is created by the DI container and injected.

    Thread-safety: intentionally lock-free. The bridge is single-threaded asyncio,
    so every caller runs on the same loop thread. If a thread pool is ever added,
    this needs a lock — noted here rather than pre-emptively paid for.
    """

    __slots__ = ("_capacity", "_counters", "_histograms", "_sessions")

    def __init__(self, histogram_capacity: int = DEFAULT_HISTOGRAM_CAPACITY) -> None:
        self._capacity = histogram_capacity
        self._histograms: dict[tuple[MetricName, _Labels], LatencyHistogram] = {}
        self._counters: dict[tuple[MetricName, _Labels], int] = {}
        self._sessions: dict[SessionId, SessionMetrics] = {}

    # -- recording ---------------------------------------------------------

    def observe(
        self,
        metric: MetricName,
        value_us: float,
        *,
        ctx: FrameContext | None = None,
        **labels: str,
    ) -> None:
        """Record a latency sample in microseconds.

        Attribution comes from ``ctx`` when supplied, otherwise from the ambient
        context, so hot-path callers holding a frame pass ``ctx=frame.ctx`` and
        everyone else gets it for free.
        """
        key = (metric, _normalise(labels))
        hist = self._histograms.get(key)
        if hist is None:
            hist = self._histograms[key] = LatencyHistogram(self._capacity)
        hist.observe(value_us)

        session = self._session_for(ctx)
        if session is not None:
            shist = session.histograms.get(key)
            if shist is None:
                shist = session.histograms[key] = LatencyHistogram(self._capacity)
            shist.observe(value_us)

    def increment(
        self,
        metric: MetricName,
        amount: int = 1,
        *,
        ctx: FrameContext | None = None,
        **labels: str,
    ) -> None:
        """Increment a counter."""
        key = (metric, _normalise(labels))
        self._counters[key] = self._counters.get(key, 0) + amount

        session = self._session_for(ctx)
        if session is not None:
            session.counters[key] = session.counters.get(key, 0) + amount

    # -- session bookkeeping ----------------------------------------------

    def _session_for(self, ctx: FrameContext | None) -> SessionMetrics | None:
        session_id = ctx.session_id if ctx else current_session_id()
        if session_id is None:
            return None
        existing = self._sessions.get(session_id)
        if existing is not None:
            return existing
        correlation = ctx.correlation_id if ctx else current_correlation_id()
        created = SessionMetrics(session_id=session_id, correlation_id=correlation)
        self._sessions[session_id] = created
        return created

    def drop_session(self, session_id: SessionId) -> None:
        """Forget a finished session's metrics.

        Called on session teardown. Without this, per-session state is a slow leak
        for a long-running process; aggregate series are unaffected.
        """
        self._sessions.pop(session_id, None)

    def tracked_sessions(self) -> tuple[SessionId, ...]:
        return tuple(self._sessions)

    # -- reading -----------------------------------------------------------

    def snapshot(self) -> MetricsSnapshot:
        """Aggregate view across all sessions."""
        return MetricsSnapshot(
            histograms={k: v.snapshot() for k, v in self._histograms.items()},
            counters=dict(self._counters),
            session_ids=tuple(self._sessions),
        )

    def session_snapshot(self, session_id: SessionId) -> MetricsSnapshot | None:
        """Per-session view, retaining the correlation id as a label."""
        session = self._sessions.get(session_id)
        if session is None:
            return None
        extra = {"session_id": session_id}
        if session.correlation_id is not None:
            extra["correlation_id"] = session.correlation_id
        tag = _normalise(extra)

        return MetricsSnapshot(
            histograms={
                (name, labels + tag): hist.snapshot()
                for (name, labels), hist in session.histograms.items()
            },
            counters={
                (name, labels + tag): value for (name, labels), value in session.counters.items()
            },
            session_ids=(session_id,),
        )
