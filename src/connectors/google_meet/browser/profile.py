"""The Chromium profile — the connector's only durable state.

**Why a persistent profile at all.** The avatar has to be signed in to Google, and a
sign-in is not something to perform per session: Google's flow can present a second
factor or a device-verification challenge, and scripting it repeatedly is both fragile
and the fastest route to an account being flagged. So the account is authenticated
*once*, interactively, into a profile directory on disk, and every session afterwards
inherits that session cookie. That is why the launch is
``launch_persistent_context`` rather than ``chromium.launch``.

**Why sessions get a copy rather than the profile itself.** A Chromium profile is a
single-writer resource. Two browsers pointed at one directory do not share it — the
second either refuses to start or corrupts the first's state, and the failure mode is a
profile that has silently lost its Google session. Since the connector is expected to
run more than one meeting at a time, that would cap concurrency at one.

So the configured directory is treated as a **template**, and each session gets a
throwaway working copy seeded from it. The account cookie comes along; nothing written
during a meeting flows back. Sessions cannot corrupt each other's state, and the
template cannot be corrupted at all — a re-authentication is only ever needed because
Google expired the session, never because a session crashed.

**Why the copy is selective rather than a whole-tree clone.** A working Chromium profile
accumulates caches, service-worker storage, GPU shader caches, and crash dumps, and
reaches hundreds of megabytes within a few sessions. Copying all of it would put seconds
of disk I/O on every session start to duplicate data the browser is about to regenerate.
``_AUTH_PATHS`` is the subset that actually carries the identity; everything else is
rebuilt by Chromium on first run.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from src.connectors.google_meet.exceptions import MeetConfigurationError
from src.infrastructure.logging import get_logger

logger = get_logger(__name__)

_AUTH_PATHS: tuple[str, ...] = (
    "Default/Cookies",
    "Default/Cookies-journal",
    "Default/Login Data",
    "Default/Login Data-journal",
    "Default/Web Data",
    "Default/Preferences",
    "Default/Secure Preferences",
    "Local State",
)
"""What has to be copied for the working profile to be signed in.

``Cookies`` is the one that matters — it holds the Google session. ``Local State`` and
``Preferences`` come along because Chromium's cookie encryption key is referenced from
them, and a ``Cookies`` file without them decrypts to nothing, which presents as a
profile that is mysteriously signed out. The ``-journal`` siblings are SQLite
write-ahead files: copying the database without them can yield a torn read."""

_SESSION_DIR_NAME = "sessions"


@dataclass(frozen=True, slots=True)
class ProfileLease:
    """One session's working profile."""

    path: Path
    session_key: str
    is_template: bool
    """True when the session was handed the template directly, which only happens with
    ``clone_per_session=False``. Worth carrying because it changes teardown: a template
    must never be deleted."""


class ProfileManager:
    """Hands out per-session working profiles seeded from a template."""

    __slots__ = ("_clone", "_template")

    def __init__(self, *, template: Path, clone_per_session: bool = True) -> None:
        self._template = Path(template)
        self._clone = clone_per_session

    @property
    def template(self) -> Path:
        return self._template

    def is_authenticated(self) -> bool:
        """True when the template looks like it holds a Google session.

        A heuristic, and honest about it: the presence of a cookie database is not proof
        that the cookie inside it is still valid — only Google can say that. It is worth
        checking anyway, because it separates "nobody ever authenticated this profile", a
        deployment step that was skipped, from "the session expired", which needs a
        re-authentication. Those have different remedies and the same symptom.
        """
        return (self._template / "Default" / "Cookies").is_file()

    def ensure_template(self) -> None:
        """Create the template directory if it does not exist.

        Raises:
            MeetConfigurationError: the path exists but is not a directory, or cannot be
                created. Fatal: without somewhere to keep the Google session there is
                nothing to launch.
        """
        if self._template.exists() and not self._template.is_dir():
            raise MeetConfigurationError(
                f"google_meet profile_dir {self._template} exists but is not a directory"
            )
        try:
            self._template.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise MeetConfigurationError(
                f"cannot create google_meet profile_dir {self._template}: {exc}"
            ) from exc

        if not self.is_authenticated():
            # Not fatal here. A first run legitimately has an empty profile, and
            # ``auth/google_login.py`` decides whether it can be bootstrapped. Failing
            # now would make an interactive first-time sign-in impossible.
            logger.warning(
                "meet_profile.not_authenticated",
                template=str(self._template),
                note="no cookie store found; the profile must be signed in to Google "
                "before a session can join a meeting",
            )

    def acquire(self, session_key: str) -> ProfileLease:
        """Produce a working profile for one session.

        Raises:
            MeetConfigurationError: the working copy could not be created.
        """
        self.ensure_template()

        if not self._clone:
            # Single-profile mode. Documented as concurrency-1 rather than guarded with
            # a lock: a lock here would turn a configuration mistake into a session that
            # blocks indefinitely, and the honest failure is Chromium refusing to open a
            # profile that is already in use, which names the real problem.
            logger.info("meet_profile.using_template", path=str(self._template))
            return ProfileLease(
                path=self._template, session_key=session_key, is_template=True
            )

        working = self._template.parent / _SESSION_DIR_NAME / session_key
        try:
            if working.exists():
                shutil.rmtree(working, ignore_errors=True)
            (working / "Default").mkdir(parents=True, exist_ok=True)
            copied = self._seed(working)
        except OSError as exc:
            raise MeetConfigurationError(
                f"cannot prepare a working profile at {working}: {exc}"
            ) from exc

        logger.info(
            "meet_profile.cloned",
            template=str(self._template),
            working=str(working),
            files=copied,
        )
        return ProfileLease(path=working, session_key=session_key, is_template=False)

    def _seed(self, working: Path) -> int:
        """Copy the identity-bearing files into a fresh working profile."""
        copied = 0
        for relative in _AUTH_PATHS:
            source = self._template / relative
            if not source.exists():
                # Every one of these is optional. A profile that has never run has none
                # of them, and a profile signed in without saved passwords has no
                # ``Login Data`` — neither is an error.
                continue
            target = working / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            copied += 1
        return copied

    def release(self, lease: ProfileLease) -> None:
        """Discard a session's working profile. Idempotent, and never raises.

        Teardown must not be able to fail a session stop, so a leftover directory is
        logged rather than raised. The next ``acquire`` with the same key removes it
        anyway, which makes the leak self-healing.
        """
        if lease.is_template:
            return
        try:
            shutil.rmtree(lease.path, ignore_errors=True)
        except OSError as exc:  # pragma: no cover - ignore_errors covers the normal case
            logger.warning(
                "meet_profile.cleanup_failed", working=str(lease.path), error=str(exc)
            )
            return
        logger.debug("meet_profile.released", working=str(lease.path))
