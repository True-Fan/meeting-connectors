"""Logging context and metrics."""

from __future__ import annotations

import pytest

from src.domain.context import FrameContext
from src.domain.ids import CorrelationId, SessionId, new_correlation_id, new_session_id
from src.infrastructure.context import (
    bind_context,
    bind_frame_context,
    context_fields,
    current_correlation_id,
    current_frame_context,
    current_session_id,
)
from src.infrastructure.logging import add_context
from src.infrastructure.metrics import (
    DEFAULT_HISTOGRAM_CAPACITY,
    LatencyHistogram,
    MetricName,
    MetricsCollector,
)
from src.infrastructure.prometheus import render


class TestIds:
    def test_ids_are_prefixed_and_unique(self) -> None:
        assert new_session_id().startswith("ses_")
        assert new_correlation_id().startswith("cor_")
        assert new_session_id() != new_session_id()


class TestAmbientContext:
    def test_unset_by_default(self) -> None:
        assert current_session_id() is None
        assert current_correlation_id() is None
        assert current_frame_context() is None
        assert context_fields() == {}

    def test_bind_and_restore(self) -> None:
        sid, cid = new_session_id(), new_correlation_id()
        with bind_context(session_id=sid, correlation_id=cid):
            assert current_session_id() == sid
            assert current_correlation_id() == cid
            assert context_fields() == {"session_id": sid, "correlation_id": cid}
        assert current_session_id() is None

    def test_partial_bind_leaves_other_untouched(self) -> None:
        """An HTTP request binds a correlation id before any session exists."""
        cid = new_correlation_id()
        with bind_context(correlation_id=cid):
            assert current_correlation_id() == cid
            assert current_session_id() is None
            assert context_fields() == {"correlation_id": cid}

    def test_frame_context_requires_both_ids(self) -> None:
        with bind_context(correlation_id=new_correlation_id()):
            assert current_frame_context() is None

    def test_nested_binds_restore_outer(self) -> None:
        outer, inner = new_session_id(), new_session_id()
        with bind_context(session_id=outer):
            with bind_context(session_id=inner):
                assert current_session_id() == inner
            assert current_session_id() == outer

    def test_bind_frame_context(self, frame_ctx: FrameContext) -> None:
        with bind_frame_context(frame_ctx):
            assert current_frame_context() == frame_ctx

    def test_context_survives_exception(self) -> None:
        sid = new_session_id()
        with pytest.raises(RuntimeError), bind_context(session_id=sid):
            raise RuntimeError("boom")
        assert current_session_id() is None


class TestLogProcessor:
    def test_injects_ambient_identity(self, frame_ctx: FrameContext) -> None:
        """Every log line carries identity without the call site mentioning it."""
        with bind_frame_context(frame_ctx):
            event = add_context(None, "info", {"event": "test"})
        assert event["session_id"] == frame_ctx.session_id
        assert event["correlation_id"] == frame_ctx.correlation_id

    def test_explicit_value_wins(self, frame_ctx: FrameContext) -> None:
        """So a log about another session is still possible where intended."""
        with bind_frame_context(frame_ctx):
            event = add_context(None, "info", {"event": "t", "session_id": "ses_other"})
        assert event["session_id"] == "ses_other"

    def test_no_keys_added_without_context(self) -> None:
        event = add_context(None, "info", {"event": "test"})
        assert "session_id" not in event


class TestLatencyHistogram:
    def test_empty_snapshot(self) -> None:
        snapshot = LatencyHistogram().snapshot()
        assert snapshot.count == 0
        assert snapshot.p50 == 0.0
        assert snapshot.mean == 0.0

    def test_percentiles(self) -> None:
        hist = LatencyHistogram()
        for value in range(1, 101):
            hist.observe(float(value))
        snapshot = hist.snapshot()
        assert snapshot.count == 100
        assert snapshot.min == 1.0
        assert snapshot.max == 100.0
        assert snapshot.p50 == pytest.approx(50.0, abs=1.0)
        assert snapshot.p95 == pytest.approx(95.0, abs=1.0)
        assert snapshot.p99 == pytest.approx(99.0, abs=1.0)

    def test_ring_buffer_bounds_memory(self) -> None:
        """Lifetime count and sum are exact; percentiles describe the window."""
        hist = LatencyHistogram(capacity=10)
        for value in range(100):
            hist.observe(float(value))
        snapshot = hist.snapshot()
        assert snapshot.count == 100
        assert snapshot.sum == sum(range(100))
        assert snapshot.p50 >= 90.0  # window holds only the last 10 samples

    def test_default_capacity(self) -> None:
        assert LatencyHistogram().snapshot().count == 0
        assert DEFAULT_HISTOGRAM_CAPACITY == 4096

    def test_rejects_bad_capacity(self) -> None:
        with pytest.raises(ValueError, match="positive"):
            LatencyHistogram(capacity=0)


class TestMetricsCollector:
    def test_observe_and_snapshot(self, metrics: MetricsCollector) -> None:
        metrics.observe(MetricName.DECODE_US, 1500.0)
        snapshot = metrics.snapshot()
        assert snapshot.histograms[(MetricName.DECODE_US, ())].count == 1

    def test_counters(self, metrics: MetricsCollector) -> None:
        metrics.increment(MetricName.FRAMES_DROPPED_TOTAL, stage="router", reason="overflow")
        metrics.increment(MetricName.FRAMES_DROPPED_TOTAL, 4, stage="router", reason="overflow")
        key = (
            MetricName.FRAMES_DROPPED_TOTAL,
            (("reason", "overflow"), ("stage", "router")),
        )
        assert metrics.snapshot().counters[key] == 5

    def test_labels_are_order_independent(self, metrics: MetricsCollector) -> None:
        metrics.increment(MetricName.RECONNECTS_TOTAL, component="rtms", scope="ingest")
        metrics.increment(MetricName.RECONNECTS_TOTAL, scope="ingest", component="rtms")
        assert len(metrics.snapshot().counters) == 1

    def test_explicit_ctx_attributes_to_session(
        self, metrics: MetricsCollector, frame_ctx: FrameContext
    ) -> None:
        metrics.observe(MetricName.PUBLISH_US, 900.0, ctx=frame_ctx)
        assert metrics.tracked_sessions() == (frame_ctx.session_id,)
        assert metrics.session_snapshot(frame_ctx.session_id) is not None

    def test_ambient_ctx_attributes_to_session(
        self, metrics: MetricsCollector, frame_ctx: FrameContext
    ) -> None:
        """Callers not holding a frame still get attribution for free."""
        with bind_frame_context(frame_ctx):
            metrics.increment(MetricName.SESSIONS_STARTED_TOTAL)
        assert metrics.tracked_sessions() == (frame_ctx.session_id,)

    def test_session_snapshot_retains_correlation_id(
        self, metrics: MetricsCollector, frame_ctx: FrameContext
    ) -> None:
        """Recorded everywhere; exported as a label only in the per-session view."""
        metrics.observe(MetricName.END_TO_END_US, 1000.0, ctx=frame_ctx)
        snapshot = metrics.session_snapshot(frame_ctx.session_id)
        assert snapshot is not None
        labels = dict(next(iter(snapshot.histograms))[1])
        assert labels["session_id"] == frame_ctx.session_id
        assert labels["correlation_id"] == frame_ctx.correlation_id

    def test_aggregate_snapshot_has_no_correlation_label(
        self, metrics: MetricsCollector, frame_ctx: FrameContext
    ) -> None:
        """correlation_id is unbounded over time — never a scrape-path label."""
        metrics.observe(MetricName.END_TO_END_US, 1000.0, ctx=frame_ctx)
        for _, labels in metrics.snapshot().histograms:
            assert "correlation_id" not in dict(labels)

    def test_unknown_session_snapshot_is_none(self, metrics: MetricsCollector) -> None:
        assert metrics.session_snapshot(SessionId("ses_nope")) is None

    def test_drop_session_frees_per_session_state(
        self, metrics: MetricsCollector, frame_ctx: FrameContext
    ) -> None:
        """Without this, per-session state is a slow leak in a long-running process."""
        metrics.observe(MetricName.DECODE_US, 1.0, ctx=frame_ctx)
        metrics.drop_session(frame_ctx.session_id)
        assert metrics.session_snapshot(frame_ctx.session_id) is None
        # Aggregate series survive.
        assert metrics.snapshot().histograms[(MetricName.DECODE_US, ())].count == 1

    def test_drop_unknown_session_is_a_noop(self, metrics: MetricsCollector) -> None:
        metrics.drop_session(SessionId("ses_absent"))


class TestMetricNames:
    def test_histogram_classification(self) -> None:
        assert MetricName.DECODE_US.is_histogram
        assert MetricName.AV_SKEW_US.is_histogram
        assert not MetricName.FRAMES_DROPPED_TOTAL.is_histogram

    def test_names_are_unique(self) -> None:
        values = [m.value for m in MetricName]
        assert len(values) == len(set(values))


class TestPrometheusRendering:
    def test_empty_snapshot_renders_empty(self, metrics: MetricsCollector) -> None:
        assert render(metrics.snapshot()) == ""

    def test_histogram_converted_to_seconds(self, metrics: MetricsCollector) -> None:
        metrics.observe(MetricName.DECODE_US, 1_000_000.0)  # 1 s
        output = render(metrics.snapshot())
        assert 'mc_decode_seconds{quantile="0.5"} 1.000000000' in output
        assert "mc_decode_seconds_count 1" in output
        assert "# TYPE mc_decode_seconds summary" in output

    def test_counter_rendered_with_labels(self, metrics: MetricsCollector) -> None:
        metrics.increment(MetricName.FRAMES_DROPPED_TOTAL, 3, stage="pacer", reason="late")
        output = render(metrics.snapshot())
        assert 'mc_frames_dropped_total{reason="late",stage="pacer"} 3' in output
        assert "# TYPE mc_frames_dropped_total counter" in output

    def test_label_values_are_escaped(self, metrics: MetricsCollector) -> None:
        metrics.increment(MetricName.FRAMES_DROPPED_TOTAL, reason='say "hi"')
        assert '\\"hi\\"' in render(metrics.snapshot())

    def test_namespace_is_configurable(self, metrics: MetricsCollector) -> None:
        metrics.increment(MetricName.SESSIONS_STARTED_TOTAL)
        assert "zoom_sessions_started_total" in render(metrics.snapshot(), namespace="zoom")


class TestFrameContext:
    def test_log_fields(self, frame_ctx: FrameContext) -> None:
        assert frame_ctx.as_log_fields() == {
            "session_id": frame_ctx.session_id,
            "correlation_id": frame_ctx.correlation_id,
        }

    def test_is_hashable_and_frozen(self) -> None:
        ctx = FrameContext(SessionId("ses_a"), CorrelationId("cor_b"))
        assert len({ctx, FrameContext(SessionId("ses_a"), CorrelationId("cor_b"))}) == 1
        with pytest.raises((AttributeError, TypeError)):
            ctx.session_id = SessionId("ses_c")  # type: ignore[misc]
