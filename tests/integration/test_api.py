"""HTTP surface."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from src.api.app import create_app
from src.api.middleware import CORRELATION_HEADER
from src.containers import Container
from src.domain.health import ComponentState
from src.infrastructure.metrics import MetricName


@pytest.fixture
def client(container: Container) -> Iterator[TestClient]:
    with TestClient(create_app(container=container)) as test_client:
        yield test_client


class TestHealth:
    def test_health_is_green(self, client: TestClient) -> None:
        response = client.get("/health")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == ComponentState.HEALTHY
        assert body["app"] == "meeting-connectors"
        assert body["env"] == "local"
        assert body["uptime_seconds"] >= 0

    def test_no_components_registered_yet(self, client: TestClient) -> None:
        """M1 has no media components; a process with nothing to do is healthy."""
        assert client.get("/health").json()["components"] == []


class TestCorrelationId:
    def test_generated_when_absent(self, client: TestClient) -> None:
        correlation_id = client.get("/health").headers[CORRELATION_HEADER]
        assert correlation_id.startswith("cor_")

    def test_inbound_header_is_honoured(self, client: TestClient) -> None:
        """Lets an operator's request be traced across service boundaries."""
        response = client.get("/health", headers={CORRELATION_HEADER: "cor_from_caller"})
        assert response.headers[CORRELATION_HEADER] == "cor_from_caller"

    def test_each_request_gets_a_distinct_id(self, client: TestClient) -> None:
        first = client.get("/health").headers[CORRELATION_HEADER]
        second = client.get("/health").headers[CORRELATION_HEADER]
        assert first != second


class TestMetricsEndpoint:
    def test_empty_when_nothing_recorded(self, client: TestClient) -> None:
        response = client.get("/metrics")
        assert response.status_code == 200
        assert response.text == ""

    def test_renders_recorded_metrics(self, client: TestClient, container: Container) -> None:
        container.metrics().increment(MetricName.SESSIONS_STARTED_TOTAL)
        response = client.get("/metrics")
        assert "mc_sessions_started_total 1" in response.text
        assert "version=0.0.4" in response.headers["content-type"]

    def test_unknown_session_returns_404(self, client: TestClient) -> None:
        response = client.get("/metrics/sessions/ses_missing")
        assert response.status_code == 404

    def test_per_session_view_includes_correlation_id(
        self, client: TestClient, container: Container, frame_ctx: object
    ) -> None:
        from src.domain.context import FrameContext

        assert isinstance(frame_ctx, FrameContext)
        container.metrics().observe(MetricName.END_TO_END_US, 1_000.0, ctx=frame_ctx)
        response = client.get(f"/metrics/sessions/{frame_ctx.session_id}")
        assert response.status_code == 200
        assert frame_ctx.correlation_id in response.text
        assert frame_ctx.session_id in response.text


class TestOpenApi:
    def test_schema_is_served(self, client: TestClient) -> None:
        schema = client.get("/openapi.json").json()
        assert "/health" in schema["paths"]
        assert "/metrics" in schema["paths"]
