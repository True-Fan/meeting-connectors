"""Session state machine and context."""

from __future__ import annotations

import pytest

from src.domain.health import ComponentState
from src.domain.ids import new_correlation_id, new_session_id
from src.domain.meeting import MeetingContext
from src.domain.session import (
    SessionContext,
    SessionState,
    allowed_transitions,
    can_transition,
    derive_state,
)


def _session() -> SessionContext:
    return SessionContext(
        session_id=new_session_id(),
        correlation_id=new_correlation_id(),
        meeting=MeetingContext(meeting_number="1234567890", display_name="Avatar"),
    )


class TestSessionState:
    def test_terminal_states(self) -> None:
        assert SessionState.STOPPED.is_terminal
        assert SessionState.FAILED.is_terminal
        assert not SessionState.ACTIVE.is_terminal

    def test_running_states(self) -> None:
        assert SessionState.ACTIVE.is_running
        assert SessionState.DEGRADED.is_running
        assert not SessionState.JOINING.is_running


class TestTransitions:
    @pytest.mark.parametrize(
        ("current", "requested"),
        [
            (SessionState.CREATED, SessionState.JOINING),
            (SessionState.JOINING, SessionState.ACTIVE),
            (SessionState.ACTIVE, SessionState.DEGRADED),
            (SessionState.DEGRADED, SessionState.ACTIVE),
            (SessionState.ACTIVE, SessionState.STOPPING),
            (SessionState.STOPPING, SessionState.STOPPED),
        ],
    )
    def test_legal(self, current: SessionState, requested: SessionState) -> None:
        assert can_transition(current, requested)

    @pytest.mark.parametrize(
        ("current", "requested"),
        [
            (SessionState.CREATED, SessionState.ACTIVE),
            (SessionState.JOINING, SessionState.DEGRADED),
            (SessionState.STOPPED, SessionState.ACTIVE),
            (SessionState.FAILED, SessionState.ACTIVE),
            (SessionState.ACTIVE, SessionState.CREATED),
        ],
    )
    def test_illegal(self, current: SessionState, requested: SessionState) -> None:
        assert not can_transition(current, requested)

    def test_terminal_states_are_dead_ends(self) -> None:
        assert allowed_transitions(SessionState.STOPPED) == frozenset()
        assert allowed_transitions(SessionState.FAILED) == frozenset()

    def test_every_state_can_reach_failed_or_is_terminal(self) -> None:
        """No state may be a trap that cannot report failure."""
        for state in SessionState:
            assert state.is_terminal or SessionState.FAILED in allowed_transitions(state)

    def test_every_running_state_can_be_stopped(self) -> None:
        for state in SessionState:
            if state.is_terminal or state is SessionState.STOPPING:
                continue
            assert SessionState.STOPPING in allowed_transitions(state)


class TestDeriveState:
    def test_both_healthy_is_active(self) -> None:
        assert derive_state(ComponentState.HEALTHY, ComponentState.HEALTHY) is SessionState.ACTIVE

    def test_unknown_leg_means_still_joining(self) -> None:
        """UNKNOWN is 'not started yet', which is distinct from unhealthy."""
        assert derive_state(ComponentState.UNKNOWN, ComponentState.HEALTHY) is SessionState.JOINING
        assert derive_state(ComponentState.HEALTHY, ComponentState.UNKNOWN) is SessionState.JOINING

    def test_ingest_down_is_degraded(self) -> None:
        """Publisher healthy: the avatar keeps publishing idle media rather than
        vanishing from the meeting."""
        assert (
            derive_state(ComponentState.UNHEALTHY, ComponentState.HEALTHY) is SessionState.DEGRADED
        )

    def test_publish_down_is_degraded(self) -> None:
        assert (
            derive_state(ComponentState.HEALTHY, ComponentState.UNHEALTHY) is SessionState.DEGRADED
        )

    def test_both_down_is_degraded_not_failed(self) -> None:
        """Health alone cannot tell a blip from an unrecoverable failure — only the
        supervisor, which knows the retry budget, may declare FAILED."""
        assert (
            derive_state(ComponentState.UNHEALTHY, ComponentState.UNHEALTHY)
            is SessionState.DEGRADED
        )


class TestSessionContext:
    def test_frame_context_matches_session_identity(self) -> None:
        session = _session()
        ctx = session.frame_context()
        assert ctx.session_id == session.session_id
        assert ctx.correlation_id == session.correlation_id

    def test_frame_context_is_stable_across_calls(self) -> None:
        """Frames from one session must compare equal on identity."""
        session = _session()
        assert session.frame_context() == session.frame_context()

    def test_log_fields_include_identity(self) -> None:
        fields = _session().log_fields()
        assert set(fields) >= {"session_id", "correlation_id", "meeting_number", "session_state"}

    def test_records_multiple_errors_in_order(self) -> None:
        """A session can fail more than once; the sequence is the diagnostic value."""
        session = _session()
        session.record_error("rtms", "first drop")
        session.record_error("publisher", "segfault", fatal=True)
        assert [e.component for e in session.errors] == ["rtms", "publisher"]
        assert session.errors[-1].fatal


class TestMeetingContext:
    def test_with_uuid_returns_a_copy(self) -> None:
        """The UUID arrives from the webhook after the session exists."""
        original = MeetingContext(meeting_number="123", display_name="Avatar")
        updated = original.with_uuid("abc==")
        assert original.meeting_uuid is None
        assert updated.meeting_uuid == "abc=="
        assert updated.meeting_number == "123"

    def test_with_uuid_copies_platform_data(self) -> None:
        original = MeetingContext(
            meeting_number="123", display_name="A", platform_data={"stream": "s1"}
        )
        updated = original.with_uuid("u")
        updated.platform_data["stream"] = "mutated"
        assert original.platform_data["stream"] == "s1"
