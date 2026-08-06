"""In-call controls, and the roster.

**The controls tests guard the one failure nothing else in the system can see.** Meet decides
independently of us whether the tracks it was handed are published. A browser that joins
muted holds a perfectly good synthetic audio track that nobody hears, and every layer reports
healthy: the bridge is up, frames flow, the pacer publishes. The only symptom is silence in
the meeting.

The roster tests cover a DOM that is machine-generated and lossy — names arrive inside ARIA
labels that also carry status text, ids are missing whenever Meet renders a tile without one,
and the same person appears in several places at once.
"""

from __future__ import annotations

from src.connectors.google_meet.automation.selectors import DEFAULT_SELECTORS, MeetSelectors
from src.connectors.google_meet.meeting.controls import MeetControls
from src.connectors.google_meet.meeting.participants import parse_roster
from tests.fakes.meet_page import (
    CAM_OFF_SELECTOR,
    CAM_ON_SELECTOR,
    IN_CALL_SELECTOR,
    MIC_OFF_SELECTOR,
    MIC_ON_SELECTOR,
    FakeBrowserDriver,
    joined_driver,
)


def _controls(driver: FakeBrowserDriver) -> MeetControls:
    return MeetControls(driver=driver, selectors=DEFAULT_SELECTORS)


class TestMuteState:
    async def test_the_selector_matching_is_the_state_read(self) -> None:
        """Meet labels the button with the *action*, so no separate query is needed — and no
        window exists in which a cached state and the real one disagree."""
        assert await _controls(FakeBrowserDriver(visible={MIC_ON_SELECTOR})).is_muted() is False
        assert await _controls(FakeBrowserDriver(visible={MIC_OFF_SELECTOR})).is_muted() is True

    async def test_no_button_is_none_not_false(self) -> None:
        """``False`` would claim "unmuted" for a browser not in a call, so the caller would
        decide no action was needed."""
        assert await _controls(FakeBrowserDriver()).is_muted() is None

    async def test_unmute_turns_the_microphone_on(self) -> None:
        driver = joined_driver()
        assert await _controls(driver).unmute() is True
        assert MIC_ON_SELECTOR in driver.visible

    async def test_unmute_is_idempotent(self) -> None:
        driver = FakeBrowserDriver(visible={MIC_ON_SELECTOR})
        assert await _controls(driver).unmute() is True
        assert driver.clicked == []

    async def test_unmute_reports_failure_when_the_control_is_missing(self) -> None:
        """A renamed control must be visible as a partial publish, not silently ignored."""
        assert await _controls(FakeBrowserDriver()).unmute() is False

    async def test_mute_is_idempotent(self) -> None:
        driver = FakeBrowserDriver(visible={MIC_OFF_SELECTOR})
        assert await _controls(driver).mute() is True
        assert driver.clicked == []


class TestCamera:
    async def test_camera_on_turns_it_on(self) -> None:
        driver = joined_driver()
        assert await _controls(driver).camera_on() is True
        assert CAM_ON_SELECTOR in driver.visible

    async def test_camera_on_is_idempotent(self) -> None:
        driver = FakeBrowserDriver(visible={CAM_ON_SELECTOR})
        assert await _controls(driver).camera_on() is True
        assert driver.clicked == []

    async def test_camera_off(self) -> None:
        driver = FakeBrowserDriver(visible={CAM_ON_SELECTOR}, on_click=_swap)
        assert await _controls(driver).camera_off() is True
        assert CAM_OFF_SELECTOR in driver.visible


class TestPublishBoth:
    async def test_both_devices_are_turned_on(self) -> None:
        driver = joined_driver()
        assert await _controls(driver).publish_both() == (True, True)
        assert {MIC_ON_SELECTOR, CAM_ON_SELECTOR} <= driver.visible

    async def test_the_camera_is_still_attempted_when_the_microphone_fails(self) -> None:
        """Audio-only is degraded but useful; abandoning video because the mic button moved
        would throw that away."""
        driver = FakeBrowserDriver(visible={CAM_OFF_SELECTOR}, on_click=_swap)
        audio, video = await _controls(driver).publish_both()
        assert (audio, video) == (False, True)
        assert CAM_ON_SELECTOR in driver.visible


class TestLeave:
    async def test_leave_clicks_the_button(self) -> None:
        driver = FakeBrowserDriver(visible={IN_CALL_SELECTOR})
        assert await _controls(driver).leave() is True
        assert IN_CALL_SELECTOR in driver.clicked

    async def test_a_missing_leave_button_does_not_raise(self) -> None:
        """This runs during teardown; an exception would propagate out of a session stop."""
        assert await _controls(FakeBrowserDriver()).leave() is False


class TestSelectors:
    def test_aria_labels_are_preferred_over_generated_class_names(self) -> None:
        """``aria-label`` is a commitment to screen readers; a hashed class name is a build
        artefact that changes without notice."""
        selectors = MeetSelectors()
        assert any("aria-label" in s for s in selectors.leave)
        assert any("aria-label" in s for s in selectors.in_call)

    def test_several_candidates_exist_per_concept(self) -> None:
        """So a Meet rename degrades gracefully instead of breaking the connector."""
        selectors = MeetSelectors()
        assert len(selectors.join_button) >= 2
        assert len(selectors.in_call) >= 2
        assert len(selectors.participant) >= 2

    def test_the_join_button_is_matched_by_text_because_it_has_no_aria_label(self) -> None:
        selectors = MeetSelectors()
        assert any("Join now" in s for s in selectors.join_button)
        assert any("Ask to join" in s for s in selectors.join_button)

    def test_only_observation_selectors_cross_into_the_page(self) -> None:
        """Pre-join selectors stay in Python: Playwright can wait for one to appear, which is
        the whole difficulty of joining and something the injected script cannot do well."""
        page_config = MeetSelectors().to_page_config()
        assert "inCall" in page_config
        assert "leave" in page_config
        assert "nameInput" not in page_config
        assert "joinButton" not in page_config

    def test_the_page_config_is_json_serialisable(self) -> None:
        import json

        json.dumps(DEFAULT_SELECTORS.to_page_config())


class TestRoster:
    def test_status_text_is_stripped_from_names(self) -> None:
        """Otherwise one person appears as two entries the moment they start presenting."""
        roster = parse_roster(
            {
                "participants": [
                    {"id": "p1", "name": "Alice Smith, presenting"},
                    {"id": "p2", "name": "Bob Jones (you)"},
                ],
                "selfName": "Bob Jones",
            }
        )
        assert roster.participants[0].display_name == "Alice Smith"
        assert roster.participants[1].display_name == "Bob Jones"

    def test_duplicates_are_collapsed(self) -> None:
        """The selector set matches a tile, a roster row and a banner for the same person."""
        roster = parse_roster(
            {
                "participants": [
                    {"id": "p1", "name": "Alice"},
                    {"id": "p1", "name": "Alice, pinned"},
                ],
                "selfName": "AI Avatar",
            }
        )
        assert roster.count == 1

    def test_the_avatars_own_entry_is_identified(self) -> None:
        roster = parse_roster(
            {
                "participants": [{"id": "p1", "name": "Alice"}, {"id": "p2", "name": "AI Avatar"}],
                "selfName": "AI Avatar",
            }
        )
        assert roster.count == 2
        assert len(roster.others) == 1
        assert roster.others[0].display_name == "Alice"

    def test_a_malformed_entry_is_dropped_and_the_rest_kept(self) -> None:
        """Losing the whole roster because one tile was odd trades a complete answer for
        no answer."""
        roster = parse_roster(
            {"participants": [{"id": "p1", "name": "Alice"}, "not-a-dict", {}], "selfName": None}
        )
        assert roster.count == 1

    def test_a_missing_participants_key_is_an_empty_roster(self) -> None:
        assert parse_roster({}).count == 0
        assert parse_roster({"participants": "nonsense"}).count == 0

    def test_ids_are_stable_across_processes(self) -> None:
        """``hash()`` is randomised per process, so a reconnect would show everyone leaving
        and rejoining."""
        first = parse_roster({"participants": [{"id": "p1", "name": "Alice"}]})
        second = parse_roster({"participants": [{"id": "p1", "name": "Alice"}]})
        assert first.to_domain()[0].user_id == second.to_domain()[0].user_id

    def test_ids_are_non_negative(self) -> None:
        """``ParticipantRef.user_id`` is an int across the domain."""
        roster = parse_roster({"participants": [{"id": "abc-xyz", "name": "Alice"}]})
        assert roster.to_domain()[0].user_id >= 0

    def test_an_entry_with_only_a_name_still_counts(self) -> None:
        """Meet renders tiles with no id; dropping them would undercount the meeting."""
        roster = parse_roster({"participants": [{"name": "Alice"}]})
        assert roster.count == 1


def _swap(driver: FakeBrowserDriver, selector: str) -> None:
    swaps = {
        MIC_OFF_SELECTOR: MIC_ON_SELECTOR,
        MIC_ON_SELECTOR: MIC_OFF_SELECTOR,
        CAM_OFF_SELECTOR: CAM_ON_SELECTOR,
        CAM_ON_SELECTOR: CAM_OFF_SELECTOR,
    }
    replacement = swaps.get(selector)
    if replacement is not None:
        driver.hide(selector)
        driver.show(replacement)
