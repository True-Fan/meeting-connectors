"""The attendance HTTP surface.

Driven through a stub connector session registered with the real supervisor, because the seam
under test *is* the duck-typing: ``MeetingService`` reaches an attendance ledger through
``getattr`` so that it never learns Google Meet exists. A test that called the ledger directly
would not exercise that, and the failure it is guarding against — a renamed attribute silently
turning every answer into a 404 — is exactly the one it would miss.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from src.api.app import create_app
from src.connectors.google_meet.meeting.attendance import AttendanceLedger
from src.connectors.google_meet.meeting.participants import MeetParticipant, MeetRoster
from src.containers import Container
from src.domain.health import HealthReport
from src.domain.ids import new_correlation_id, new_session_id
from src.domain.meeting import MeetingContext, MeetingPlatform
from src.domain.session import SessionContext


class _StubMeetSession:
    """The shape ``SessionSupervisor`` drives, plus the ``attendance`` property Meet adds."""

    def __init__(self, session: SessionContext, ledger: AttendanceLedger | None) -> None:
        self._session = session
        self._ledger = ledger

    @property
    def session(self) -> SessionContext:
        return self._session

    @property
    def attendance(self) -> AttendanceLedger | None:
        return self._ledger

    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    def health(self) -> HealthReport:
        return HealthReport(components=())

    def leg_states(self):  # type: ignore[no-untyped-def]
        from src.domain.health import ComponentState

        return ComponentState.HEALTHY, ComponentState.HEALTHY


def _roster(*names: str) -> MeetRoster:
    return MeetRoster(
        participants=(
            *(MeetParticipant(page_id=f"p-{n}", display_name=n) for n in names),
            MeetParticipant(page_id="self", display_name="AI Avatar", is_self=True),
        ),
        self_name="AI Avatar",
    )


@pytest.fixture
def client(container: Container) -> Iterator[TestClient]:
    with TestClient(create_app(container=container)) as test_client:
        yield test_client


def _supervise(container: Container, ledger: AttendanceLedger | None) -> str:
    """Make a stub session visible to ``MeetingService``, and return its id.

    Registered into the supervisor's lookup rather than through ``supervise()``, which also
    spawns the state-polling task and so needs a running loop that ``TestClient``'s synchronous
    calls do not provide. What is under test here is the lookup path — service to ledger to
    JSON — and the watch loop has its own tests.
    """
    session = SessionContext(
        session_id=new_session_id(),
        correlation_id=new_correlation_id(),
        meeting=MeetingContext(
            meeting_number="abc-defg-hij",
            display_name="AI Avatar",
            platform=MeetingPlatform.GOOGLE_MEET,
        ),
    )
    container.session_supervisor()._sessions[session.session_id] = _StubMeetSession(
        session, ledger
    )
    return session.session_id


class TestGetParticipants:
    def test_reports_present_departed_and_never_joined(
        self, client: TestClient, container: Container
    ) -> None:
        ledger = AttendanceLedger(invitees=("Aarav Sharma", "Priya Menon", "Rahul Verma"))
        ledger.observe_roster(_roster("Aarav Sharma", "Priya Menon"))
        ledger.observe_roster(_roster("Aarav Sharma"))
        session_id = _supervise(container, ledger)

        body = client.get(f"/sessions/{session_id}/participants").json()

        assert body["present"] == ["Aarav Sharma"]
        assert body["departed"] == ["Priya Menon"]
        assert body["never_joined"] == ["Rahul Verma"]
        assert body["has_invite_list"] is True
        assert body["self_name"] == "AI Avatar"
        assert body["roster_scans"] == 2

    def test_agent_context_is_prose_naming_everyone(
        self, client: TestClient, container: Container
    ) -> None:
        ledger = AttendanceLedger()
        ledger.observe_roster(_roster("Aarav Sharma"))
        session_id = _supervise(container, ledger)

        context = client.get(f"/sessions/{session_id}/participants").json()["agent_context"]

        assert "Aarav Sharma" in context
        assert "No invite list" in context, "the agent must be told what it does not know"

    def test_per_person_detail_is_ordered_by_arrival(
        self, client: TestClient, container: Container
    ) -> None:
        ledger = AttendanceLedger()
        ledger.observe_roster(_roster("Aarav Sharma"))
        ledger.observe_roster(_roster("Aarav Sharma", "Priya Menon"))
        session_id = _supervise(container, ledger)

        people = client.get(f"/sessions/{session_id}/participants").json()["participants"]

        assert [p["display_name"] for p in people] == ["Aarav Sharma", "Priya Menon"]
        assert people[0]["first_seen_at"] is not None
        assert people[0]["seconds_in_meeting"] >= 0

    def test_unknown_session_is_a_404(self, client: TestClient) -> None:
        response = client.get("/sessions/ses_missing/participants")
        assert response.status_code == 404

    def test_a_session_without_a_ledger_is_a_404_not_an_empty_answer(
        self, client: TestClient, container: Container
    ) -> None:
        """Attendance switched off must not read as "nobody attended"."""
        session_id = _supervise(container, None)

        response = client.get(f"/sessions/{session_id}/participants")

        assert response.status_code == 404
        assert "attendance" in response.json()["detail"].lower()


class TestSeedInvitees:
    def test_seeding_makes_never_joined_answerable(
        self, client: TestClient, container: Container
    ) -> None:
        ledger = AttendanceLedger()
        ledger.observe_roster(_roster("Aarav Sharma"))
        session_id = _supervise(container, ledger)

        posted = client.post(
            f"/sessions/{session_id}/invitees",
            json={"invitees": ["Aarav Sharma", "Priya Menon"]},
        )

        assert posted.status_code == 200
        assert posted.json() == {
            "session_id": session_id,
            "newly_recorded": 2,
            "total_invited": 2,
        }
        assert client.get(f"/sessions/{session_id}/participants").json()["never_joined"] == [
            "Priya Menon"
        ]

    def test_re_posting_the_same_list_records_nothing_new(
        self, client: TestClient, container: Container
    ) -> None:
        """The orchestrator may retry; a retry must not inflate the invite list."""
        session_id = _supervise(container, AttendanceLedger())
        payload = {"invitees": ["Priya Menon"]}

        client.post(f"/sessions/{session_id}/invitees", json=payload)
        second = client.post(f"/sessions/{session_id}/invitees", json=payload)

        assert second.json()["newly_recorded"] == 0
        assert second.json()["total_invited"] == 1

    def test_unknown_session_is_a_404(self, client: TestClient) -> None:
        response = client.post("/sessions/ses_missing/invitees", json={"invitees": ["A"]})
        assert response.status_code == 404


class TestPlatformBlindness:
    def test_the_sessions_lifecycle_contract_is_unchanged(self, client: TestClient) -> None:
        """Attendance added endpoints; it must not have altered the ones already shipped."""
        schema = client.get("/openapi.json").json()
        create = schema["components"]["schemas"]["CreateSessionRequest"]["properties"]

        assert "invitees" not in create, (
            "the invite list is a separate call so the join contract stays platform-blind"
        )
        assert {"/sessions/{session_id}/participants", "/sessions/{session_id}/invitees"} <= set(
            schema["paths"]
        )
