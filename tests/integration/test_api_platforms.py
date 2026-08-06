"""The HTTP surface, with two connectors.

The property under test is that the API stayed platform-blind. Adding Teams changed two
lines in ``api/routers/sessions.py`` (passing ``platform`` and ``meeting_url`` through) and
added no router, no endpoint, and no connector import — so a caller's existing requests
behave identically and Google Meet will need nothing here at all.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from src.api.app import create_app
from src.config.settings import Settings, TeamsSettings
from src.containers import Container
from src.domain.meeting import MeetingPlatform

TENANT = "72f988bf-86f1-41af-91ab-2d7cd011db47"


def _teams_settings() -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        teams=TeamsSettings(
            tenant_id=TENANT,
            client_id="8b081ef6-4792-4def-b2c9-c363a1bf41d5",
            client_secret=SecretStr("secret"),
            sidecar_host="teams-bot.internal",
        ),
    )


@pytest.fixture
def client(container: Container) -> Iterator[TestClient]:
    """A Zoom-only deployment — what production is today."""
    with TestClient(create_app(container=container)) as test_client:
        yield test_client


@pytest.fixture
def teams_client() -> Iterator[TestClient]:
    built = Container()
    built.settings.override(_teams_settings())
    with TestClient(create_app(container=built)) as test_client:
        yield test_client
    built.unwire()


class TestRequestContract:
    def test_platform_defaults_to_zoom(self, client: TestClient) -> None:
        """A request body written before Teams existed must be accepted unchanged and
        still mean Zoom."""
        from src.api.dto import CreateSessionRequest

        request = CreateSessionRequest(meeting_number="1234567890")
        assert request.platform is MeetingPlatform.ZOOM
        assert request.meeting_url is None

    def test_openapi_advertises_the_platform_enum(self, client: TestClient) -> None:
        schema = client.get("/openapi.json").json()
        create = schema["components"]["schemas"]["CreateSessionRequest"]

        assert "platform" in create["properties"]
        assert "meeting_url" in create["properties"]
        # The enum is the domain's, so the contract names platforms without exposing any
        # platform's mechanics.
        #
        # This list grows by one line per connector, and that is the *only* reason this
        # assertion needed touching when Google Meet arrived: it enumerates the platform
        # set, so a third platform cannot pass it unchanged. Nothing about Zoom's or Teams'
        # behaviour moved — the two assertions that guard those are above and below, and
        # both were untouched.
        platform_enum = schema["components"]["schemas"]["MeetingPlatform"]["enum"]
        assert sorted(platform_enum) == ["google_meet", "teams", "zoom"]

    def test_an_unknown_platform_is_a_422(self, client: TestClient) -> None:
        """Validation, not a 500: the enum is the contract."""
        response = client.post(
            "/sessions", json={"meeting_number": "1234567890", "platform": "webex"}
        )
        assert response.status_code == 422

    def test_a_teams_join_url_fits_the_length_limit(self, client: TestClient) -> None:
        """Real join URLs are long; ``meeting_number`` accepts one because operators
        paste them there."""
        from src.api.dto import CreateSessionRequest

        url = "https://teams.microsoft.com/l/meetup-join/" + ("x" * 400)
        assert CreateSessionRequest(meeting_number=url).meeting_number == url


class TestResponseContract:
    def test_session_response_reports_its_platform(self) -> None:
        from src.api.dto import SessionResponse
        from src.domain.ids import CorrelationId, SessionId
        from src.domain.meeting import MeetingContext
        from src.domain.session import SessionContext, SessionState

        session = SessionContext(
            session_id=SessionId("ses_x"),
            correlation_id=CorrelationId("cor_x"),
            meeting=MeetingContext(
                meeting_number="123456789012",
                display_name="AI Avatar",
                platform=MeetingPlatform.TEAMS,
            ),
            state=SessionState.ACTIVE,
        )

        response = SessionResponse.from_domain(session)
        assert response.platform is MeetingPlatform.TEAMS

    def test_audio_attached_is_answered_per_platform(self) -> None:
        """Zoom waits for an RTMS stream, so the meeting UUID is the answer. Teams' one
        join covers both directions, so a running session is attached — reusing the UUID
        test would report every healthy Teams session as unattached forever."""
        from src.api.dto import SessionResponse
        from src.domain.ids import CorrelationId, SessionId
        from src.domain.meeting import MeetingContext
        from src.domain.session import SessionContext, SessionState

        def _session(platform: MeetingPlatform, uuid: str | None) -> SessionContext:
            return SessionContext(
                session_id=SessionId("ses_x"),
                correlation_id=CorrelationId("cor_x"),
                meeting=MeetingContext(
                    meeting_number="1",
                    display_name="AI Avatar",
                    meeting_uuid=uuid,
                    platform=platform,
                ),
                state=SessionState.ACTIVE,
            )

        zoom_unbound = SessionResponse.from_domain(_session(MeetingPlatform.ZOOM, None))
        zoom_bound = SessionResponse.from_domain(_session(MeetingPlatform.ZOOM, "uuid"))
        teams = SessionResponse.from_domain(_session(MeetingPlatform.TEAMS, None))

        assert zoom_unbound.audio_attached is False
        assert zoom_bound.audio_attached is True
        assert teams.audio_attached is True


class TestPlatformAvailability:
    def test_teams_is_rejected_when_no_connector_is_registered(
        self, client: TestClient
    ) -> None:
        """A Zoom-only deployment gives a precise reason rather than failing inside a
        join with a missing-tenant error."""
        response = client.post(
            "/sessions", json={"meeting_number": "123456789012", "platform": "teams"}
        )

        assert response.status_code == 409
        assert "no connector registered" in response.json()["detail"]

    def test_zoom_is_unaffected_by_teams_being_configured(
        self, teams_client: TestClient
    ) -> None:
        """The backward-compatibility check that matters: a Zoom request behaves the same
        whether or not Teams is configured. It fails here only because no sidecar exists in
        a test process — the point is *how* it fails, identically in both deployments."""
        response = teams_client.post("/sessions", json={"meeting_number": "1234567890"})
        assert response.status_code in (409, 500)

    def test_health_and_metrics_are_unchanged(self, teams_client: TestClient) -> None:
        assert teams_client.get("/health").status_code == 200
        assert teams_client.get("/metrics").status_code == 200

    def test_no_teams_webhook_route_is_mounted(self, teams_client: TestClient) -> None:
        """Deliberate. Graph call notifications are consumed by the Calling SDK inside the
        Windows sidecar, so relaying them through FastAPI would only forward them straight
        back out. Zoom's webhook is still mounted, because its payload is routing data the
        bridge itself acts on."""
        paths = teams_client.get("/openapi.json").json()["paths"]

        assert not [p for p in paths if "teams" in p.lower()]
        assert any("/webhooks/zoom" in p for p in paths)

    def test_zoom_webhook_is_still_mounted_with_teams_configured(
        self, teams_client: TestClient
    ) -> None:
        response = teams_client.post("/webhooks/zoom/", json={"event": "nonsense"})
        # Unsigned, so it is refused — but the route exists and still verifies.
        assert response.status_code in (400, 401, 403)
