"""Per-session Chromium profiles on the Zoom-web and Teams-web connectors.

**What these tests are defending.** Both connectors used to hand the configured
``profile_dir`` straight to ``launch_persistent_context``. A Chromium profile is a
single-writer resource, so two concurrent meetings meant the second browser either
refused to start or corrupted the first's session — and on Zoom that session *is* the
microphone selection the profile exists to carry. Each session now runs on a copy leased
from ``ProfileManager``, which is what allows several meetings to share one process (and
one container).

The lifecycle methods are exercised on instances built with ``object.__new__`` and given
only the attributes they touch. That is deliberate: a real ``ZoomWebSession`` needs some
fifteen collaborators, none of which has anything to do with profile management, and
building them here would test the fixture rather than the behaviour. ``test_*_start_*``
goes through the genuine ``start()`` entry point, so the wiring between the three methods
is covered rather than assumed.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from src.connectors.google_meet.browser.profile import ProfileManager
from src.connectors.teams_web.session.teams_web_session import TeamsWebSession
from src.connectors.zoom_web.session.zoom_web_session import ZoomWebSession
from src.domain.media import VideoFormat

SESSION_CLASSES = (ZoomWebSession, TeamsWebSession)


class _FailingDriver:
    """A browser that cannot be launched, and remembers whether it was closed."""

    def __init__(self) -> None:
        self.stopped = False

    async def start(self, plan: object) -> None:
        raise RuntimeError("chromium refused to launch")

    async def stop(self) -> None:
        self.stopped = True


def _config(profile_dir: Path | None) -> SimpleNamespace:
    """Only the fields the launch path reads before the driver is touched."""
    return SimpleNamespace(
        profile_dir=profile_dir,
        headless=True,
        no_sandbox=False,
        bypass_csp=False,
        video_format=VideoFormat(width=1280, height=720, fps=25),
    )


def _session(cls: type, profile_dir: Path | None, session_id: str = "sess_a") -> object:
    """A session carrying just enough state for the profile lifecycle."""
    obj = object.__new__(cls)
    obj._config = _config(profile_dir)
    obj._session = SimpleNamespace(session_id=session_id)
    obj._temp_profile = None
    obj._profiles = None
    obj._lease = None
    obj._driver = _FailingDriver()
    return obj


def _template(tmp_path: Path, name: str) -> Path:
    """A signed-in-looking template: a cookie store is what ``is_authenticated`` reads."""
    template = tmp_path / name
    (template / "Default").mkdir(parents=True)
    (template / "Default" / "Cookies").write_bytes(b"cookie-store")
    (template / "Default" / "Preferences").write_text('{"microphone": "chosen"}')
    (template / "Local State").write_text('{"os_crypt": {}}')
    return template


# -- isolation ------------------------------------------------------------------


@pytest.mark.parametrize("cls", SESSION_CLASSES)
def test_two_sessions_receive_different_profile_paths(cls: type, tmp_path: Path) -> None:
    """The property the single-container model depends on."""
    template = _template(tmp_path, "profile")
    first = _session(cls, template, session_id="sess_a")
    second = _session(cls, template, session_id="sess_b")

    path_a = first._acquire_profile()
    path_b = second._acquire_profile()

    assert path_a != path_b
    # Both live, both seeded — neither was waiting on the other to let go.
    assert path_a.is_dir()
    assert path_b.is_dir()
    assert (path_a / "Default" / "Cookies").is_file()
    assert (path_b / "Default" / "Cookies").is_file()


@pytest.mark.parametrize("cls", SESSION_CLASSES)
def test_template_profile_is_never_launched_directly(cls: type, tmp_path: Path) -> None:
    """The regression itself: the meeting must not run on the configured directory."""
    template = _template(tmp_path, "profile")
    session = _session(cls, template)

    path = session._acquire_profile()

    assert path != template
    assert not path.is_relative_to(template)
    assert session._lease is not None
    assert session._lease.is_template is False


@pytest.mark.parametrize("cls", SESSION_CLASSES)
def test_meeting_does_not_write_back_to_the_template(cls: type, tmp_path: Path) -> None:
    """A session's writes stay in its copy, so a crash cannot cost the sign-in."""
    template = _template(tmp_path, "profile")
    session = _session(cls, template)

    path = session._acquire_profile()
    (path / "Default" / "Cookies").write_bytes(b"clobbered-during-the-meeting")
    (path / "Default" / "scratch").write_text("session junk")

    assert (template / "Default" / "Cookies").read_bytes() == b"cookie-store"
    assert not (template / "Default" / "scratch").exists()


@pytest.mark.parametrize("cls", SESSION_CLASSES)
def test_seeded_profile_carries_the_identity_files(cls: type, tmp_path: Path) -> None:
    """``Preferences`` is the one Zoom's microphone selection lives in."""
    template = _template(tmp_path, "profile")
    session = _session(cls, template)

    path = session._acquire_profile()

    assert (path / "Default" / "Preferences").read_text() == '{"microphone": "chosen"}'
    assert (path / "Local State").is_file()


# -- cleanup --------------------------------------------------------------------


@pytest.mark.parametrize("cls", SESSION_CLASSES)
def test_release_removes_the_leased_profile(cls: type, tmp_path: Path) -> None:
    template = _template(tmp_path, "profile")
    session = _session(cls, template)
    path = session._acquire_profile()
    assert path.is_dir()

    session._release_profile()

    assert not path.exists()
    assert template.is_dir()
    assert (template / "Default" / "Cookies").is_file()
    assert session._lease is None
    assert session._profiles is None


@pytest.mark.parametrize("cls", SESSION_CLASSES)
def test_release_is_idempotent(cls: type, tmp_path: Path) -> None:
    """Teardown runs on paths that may already have cleaned up; it must not raise."""
    session = _session(cls, _template(tmp_path, "profile"))
    session._acquire_profile()

    session._release_profile()
    session._release_profile()


@pytest.mark.parametrize("cls", SESSION_CLASSES)
async def test_failed_start_releases_the_lease(cls: type, tmp_path: Path) -> None:
    """``MeetingService`` never calls ``stop()`` on a session that failed to start."""
    template = _template(tmp_path, "profile")
    session = _session(cls, template)

    with pytest.raises(RuntimeError, match="chromium refused to launch"):
        await session.start()

    sessions_dir = template.parent / "sessions"
    assert session._lease is None
    if sessions_dir.exists():
        assert list(sessions_dir.iterdir()) == []
    # Closed before the directory went, or we would be deleting a live profile.
    assert session._driver.stopped is True
    assert (template / "Default" / "Cookies").is_file()


@pytest.mark.parametrize("cls", SESSION_CLASSES)
async def test_failed_start_removes_the_temporary_profile(cls: type, tmp_path: Path) -> None:
    session = _session(cls, None)

    with pytest.raises(RuntimeError, match="chromium refused to launch"):
        await session.start()

    assert session._temp_profile is None


# -- the unconfigured fallback --------------------------------------------------


@pytest.mark.parametrize("cls", SESSION_CLASSES)
def test_unconfigured_profile_dir_still_uses_a_temporary_directory(
    cls: type, tmp_path: Path
) -> None:
    """Unchanged behaviour: no profile configured means a throwaway one per session."""
    session = _session(cls, None)

    path = session._acquire_profile()

    assert path.is_dir()
    assert session._temp_profile == str(path)
    assert session._lease is None
    assert session._profiles is None


@pytest.mark.parametrize("cls", SESSION_CLASSES)
def test_unconfigured_sessions_get_distinct_temporary_directories(
    cls: type, tmp_path: Path
) -> None:
    first = _session(cls, None)
    second = _session(cls, None)

    assert first._acquire_profile() != second._acquire_profile()

    first._release_profile()
    second._release_profile()


@pytest.mark.parametrize("cls", SESSION_CLASSES)
def test_release_removes_the_temporary_directory(cls: type, tmp_path: Path) -> None:
    session = _session(cls, None)
    path = session._acquire_profile()

    session._release_profile()

    assert not path.exists()
    assert session._temp_profile is None


# -- the shared mechanism -------------------------------------------------------


def test_profile_manager_keys_working_copies_by_session_id(tmp_path: Path) -> None:
    """Both connectors rely on this, and so does Meet: the key is the session id."""
    template = _template(tmp_path, "profile")
    manager = ProfileManager(template=template)

    lease = manager.acquire("sess_x")

    assert lease.path == template.parent / "sessions" / "sess_x"
    assert lease.session_key == "sess_x"


def test_profile_manager_release_never_deletes_a_template(tmp_path: Path) -> None:
    """The guard that makes ``clone_per_session=False`` safe — asserted, not assumed."""
    template = _template(tmp_path, "profile")
    manager = ProfileManager(template=template, clone_per_session=False)

    lease = manager.acquire("sess_x")
    assert lease.is_template is True
    manager.release(lease)

    assert template.is_dir()
    assert (template / "Default" / "Cookies").is_file()
