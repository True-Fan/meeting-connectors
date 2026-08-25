"""Prometheus text-format rendering.

Hand-rolled rather than pulling ``prometheus_client``: the collector already owns
aggregation, so all that is needed is a formatter, and a dependency whose registry
is a process-global would fight the no-globals rule.

Format reference: histograms are exposed as ``_count``/``_sum`` plus explicit
quantile series. Latencies are recorded in microseconds and exported in **seconds**,
per Prometheus base-unit convention.
"""

from __future__ import annotations

from src.infrastructure.metrics import MetricName, MetricsSnapshot

_US_PER_S = 1_000_000.0


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _render_labels(labels: tuple[tuple[str, str], ...], **extra: str) -> str:
    pairs = [*labels, *sorted(extra.items())]
    if not pairs:
        return ""
    inner = ",".join(f'{key}="{_escape(value)}"' for key, value in pairs)
    return "{" + inner + "}"


def render(snapshot: MetricsSnapshot, *, namespace: str = "mc") -> str:
    """Render a snapshot as Prometheus text exposition format."""
    lines: list[str] = []

    for metric in MetricName:
        if not metric.is_histogram:
            continue
        series = {k: v for k, v in snapshot.histograms.items() if k[0] is metric}
        if not series:
            continue
        base = f"{namespace}_{metric.value.removesuffix('_us')}_seconds"
        lines.append(f"# HELP {base} {metric.value} recorded by the bridge")
        lines.append(f"# TYPE {base} summary")
        for (_, labels), stats in sorted(series.items(), key=lambda kv: kv[0][1]):
            for quantile, value in (("0.5", stats.p50), ("0.95", stats.p95), ("0.99", stats.p99)):
                rendered = _render_labels(labels, quantile=quantile)
                lines.append(f"{base}{rendered} {value / _US_PER_S:.9f}")
            plain = _render_labels(labels)
            lines.append(f"{base}_sum{plain} {stats.sum / _US_PER_S:.9f}")
            lines.append(f"{base}_count{plain} {stats.count}")

    for metric in MetricName:
        if metric.is_histogram:
            continue
        series = {k: v for k, v in snapshot.counters.items() if k[0] is metric}
        if not series:
            continue
        base = f"{namespace}_{metric.value}"
        lines.append(f"# HELP {base} {metric.value} recorded by the bridge")
        lines.append(f"# TYPE {base} counter")
        for (_, labels), value in sorted(series.items(), key=lambda kv: kv[0][1]):
            lines.append(f"{base}{_render_labels(labels)} {value}")

    return "\n".join(lines) + "\n" if lines else ""
