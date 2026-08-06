"""Chromium launch flags and the profile that holds the Google session.

Both are the kind of thing that regresses silently. A dropped launch flag does not raise —
it produces a headless browser whose ``AudioContext`` never starts, or whose compositor is
throttled to a few frames a second, while every health check stays green. So the flags that
the media path genuinely depends on are asserted by name, with the reason in the test name.

The profile tests cover the property that makes concurrency possible: sessions get copies,
never the template, so one session cannot cost another its login.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.connectors.google_meet.browser.launcher import (
    AUTOMATION_ARGS,
    MEDIA_ARGS,
    STABILITY_ARGS,
    build_launch_plan,
)
from src.connectors.google_meet.browser.profile import ProfileManager
from src.connectors.google_meet.exceptions import MeetConfigurationError
from src.domain.media import VideoFormat

FORMAT = VideoFormat(width=1280, height=720, fps=25)


def _plan(**kwargs: object):
    return build_launch_plan(
        user_data_dir=Path("/tmp/profile"),
        video_format=FORMAT,
        **kwargs,  # type: ignore[arg-type]
    )


class TestFlagsTheMediaPathNeeds:
    def test_permission_prompts_are_auto_accepted(self) -> None:
        """In headless mode the prompt is not even rendered, so the promise never settles."""
        assert "--use-fake-ui-for-media-stream" in _plan().args

    def test_autoplay_policy_is_disabled_so_audiocontexts_start(self) -> None:
        """A suspended AudioContext renders nothing, silently, in both directions."""
        assert "--autoplay-policy=no-user-gesture-required" in _plan().args

    def test_chromiums_own_fake_devices_are_not_enabled(self) -> None:
        """They would hand Meet a test pattern if our getUserMedia patch failed to install.

        That is the worst outcome available: the avatar would appear as a rolling colour bar
        and the session would look healthy. Without the flag, a failed patch means no track
        at all, which surfaces immediately.
        """
        args = _plan().args
        assert "--use-fake-device-for-media-stream" not in args
        assert not any("fake-device" in arg for arg in args)

    def test_mute_audio_is_removed_from_playwrights_defaults(self) -> None:
        """Playwright mutes audio by default; a muted renderer risks suppressing the graph."""
        assert _plan().to_playwright_kwargs()["ignore_default_args"] == ["--mute-audio"]

    def test_camera_and_microphone_are_granted_up_front(self) -> None:
        """A second, independent mechanism: a prompt in a headless browser is unrecoverable."""
        assert _plan().to_playwright_kwargs()["permissions"] == ["camera", "microphone"]


class TestFlagsThatKeepItRendering:
    def test_dev_shm_is_not_used(self) -> None:
        """Docker's default /dev/shm is 64 MB, which a video-carrying renderer exhausts."""
        assert "--disable-dev-shm-usage" in _plan().args

    @pytest.mark.parametrize(
        "flag",
        [
            "--disable-background-timer-throttling",
            "--disable-backgrounding-occluded-windows",
            "--disable-renderer-backgrounding",
        ],
    )
    def test_backgrounding_throttles_are_disabled(self, flag: str) -> None:
        """A headless window is never 'visible', so Chromium would throttle the compositor
        and quietly drop the published frame rate while health stayed green."""
        assert flag in _plan().args

    def test_automation_fingerprint_is_reduced(self) -> None:
        """Google's sign-in treats navigator.webdriver as a signal and will challenge it."""
        assert "--disable-blink-features=AutomationControlled" in _plan().args


class TestPlanShape:
    def test_locale_is_pinned_so_english_text_matching_holds(self) -> None:
        """``automation/selectors.py`` detects terminal states from English copy."""
        plan = _plan()
        assert plan.locale == "en-US"
        assert "--lang=en-US" in plan.args
        assert plan.to_playwright_kwargs()["locale"] == "en-US"

    def test_the_viewport_never_collapses_below_720p(self) -> None:
        """Meet picks its layout from the viewport, and a tiny one hides the join controls."""
        plan = build_launch_plan(
            user_data_dir=Path("/tmp/p"),
            video_format=VideoFormat(width=320, height=180, fps=15),
        )
        assert plan.viewport == (1280, 720)

    def test_the_viewport_follows_a_larger_publish_geometry(self) -> None:
        plan = build_launch_plan(
            user_data_dir=Path("/tmp/p"),
            video_format=VideoFormat(width=1920, height=1080, fps=25),
        )
        assert plan.viewport == (1920, 1080)
        assert "--window-size=1920,1080" in plan.args

    def test_sandbox_flags_are_opt_in(self) -> None:
        assert "--no-sandbox" not in _plan().args
        assert "--no-sandbox" in _plan(no_sandbox=True).args

    def test_extra_args_come_last_so_they_can_override(self) -> None:
        """Chromium takes the final occurrence of a repeated switch."""
        plan = _plan(extra_args=("--autoplay-policy=user-gesture-required",))
        assert plan.args[-1] == "--autoplay-policy=user-gesture-required"

    def test_executable_path_is_omitted_when_unset(self) -> None:
        assert "executable_path" not in _plan().to_playwright_kwargs()
        kwargs = _plan(executable_path=Path("/usr/bin/chromium")).to_playwright_kwargs()
        assert kwargs["executable_path"] == "/usr/bin/chromium"

    def test_timeout_is_converted_to_milliseconds(self) -> None:
        assert _plan(timeout_s=30.0).to_playwright_kwargs()["timeout"] == 30_000

    def test_every_documented_flag_group_is_applied(self) -> None:
        args = set(_plan().args)
        for group in (MEDIA_ARGS, STABILITY_ARGS, AUTOMATION_ARGS):
            assert set(group) <= args


class TestProfile:
    def test_a_session_gets_a_copy_not_the_template(self, tmp_path: Path) -> None:
        """Two browsers on one profile do not share it — the second corrupts the first."""
        template = tmp_path / "template"
        (template / "Default").mkdir(parents=True)
        (template / "Default" / "Cookies").write_bytes(b"cookie-db")

        manager = ProfileManager(template=template)
        lease = manager.acquire("ses_abc")

        assert lease.path != template
        assert lease.is_template is False
        assert (lease.path / "Default" / "Cookies").read_bytes() == b"cookie-db"

    def test_two_sessions_get_independent_profiles(self, tmp_path: Path) -> None:
        template = tmp_path / "template"
        (template / "Default").mkdir(parents=True)
        (template / "Default" / "Cookies").write_bytes(b"cookie-db")

        manager = ProfileManager(template=template)
        first = manager.acquire("ses_one")
        second = manager.acquire("ses_two")

        assert first.path != second.path
        assert (first.path / "Default" / "Cookies").exists()
        assert (second.path / "Default" / "Cookies").exists()

    def test_the_cookie_encryption_key_files_come_along(self, tmp_path: Path) -> None:
        """A Cookies file without Local State decrypts to nothing, presenting as signed out."""
        template = tmp_path / "template"
        (template / "Default").mkdir(parents=True)
        (template / "Default" / "Cookies").write_bytes(b"c")
        (template / "Local State").write_text("{}")
        (template / "Default" / "Preferences").write_text("{}")

        lease = ProfileManager(template=template).acquire("ses_abc")

        assert (lease.path / "Local State").exists()
        assert (lease.path / "Default" / "Preferences").exists()

    def test_caches_are_not_copied(self, tmp_path: Path) -> None:
        """A whole-tree clone would put seconds of I/O on every session start."""
        template = tmp_path / "template"
        (template / "Default" / "Cache").mkdir(parents=True)
        (template / "Default" / "Cookies").write_bytes(b"c")
        (template / "Default" / "Cache" / "big").write_bytes(b"x" * 4096)

        lease = ProfileManager(template=template).acquire("ses_abc")
        assert not (lease.path / "Default" / "Cache").exists()

    def test_release_removes_the_copy_and_never_the_template(self, tmp_path: Path) -> None:
        template = tmp_path / "template"
        (template / "Default").mkdir(parents=True)
        (template / "Default" / "Cookies").write_bytes(b"c")

        manager = ProfileManager(template=template)
        lease = manager.acquire("ses_abc")
        manager.release(lease)

        assert not lease.path.exists()
        assert (template / "Default" / "Cookies").exists()

    def test_releasing_the_template_is_a_no_op(self, tmp_path: Path) -> None:
        """Concurrency-1 mode must not delete the deployment's Google session."""
        template = tmp_path / "template"
        (template / "Default").mkdir(parents=True)
        (template / "Default" / "Cookies").write_bytes(b"c")

        manager = ProfileManager(template=template, clone_per_session=False)
        lease = manager.acquire("ses_abc")
        assert lease.is_template
        assert lease.path == template

        manager.release(lease)
        assert (template / "Default" / "Cookies").exists()

    def test_reacquiring_the_same_key_starts_clean(self, tmp_path: Path) -> None:
        """Which is what makes a leaked working directory self-healing after a crash."""
        template = tmp_path / "template"
        (template / "Default").mkdir(parents=True)
        (template / "Default" / "Cookies").write_bytes(b"c")

        manager = ProfileManager(template=template)
        first = manager.acquire("ses_abc")
        (first.path / "leftover").write_text("stale")

        second = manager.acquire("ses_abc")
        assert second.path == first.path
        assert not (second.path / "leftover").exists()

    def test_an_empty_template_is_created_but_flagged_unauthenticated(
        self, tmp_path: Path
    ) -> None:
        """A first run legitimately has no cookie store; failing here would make the
        interactive sign-in impossible."""
        template = tmp_path / "fresh"
        manager = ProfileManager(template=template)
        manager.ensure_template()

        assert template.is_dir()
        assert manager.is_authenticated() is False

    def test_a_template_path_that_is_a_file_is_fatal(self, tmp_path: Path) -> None:
        blocker = tmp_path / "not-a-dir"
        blocker.write_text("oops")
        with pytest.raises(MeetConfigurationError, match="not a directory"):
            ProfileManager(template=blocker).ensure_template()
