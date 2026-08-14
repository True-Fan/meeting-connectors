"""Configuration.

Config-driven with no globals: ``Settings`` is constructed once in ``main.py`` and passed
to every collaborator explicitly. Nothing reads ``os.environ`` directly outside this file.

Environment variables use the ``ORCH_`` prefix and ``__`` as the nesting delimiter, e.g.
``ORCH_GOOGLE__SERVICE_ACCOUNT_FILE``. See ``.env.example`` for the full list.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Read-only is all this service ever needs: it watches for meetings, it never creates,
# edits, or deletes calendar events.
CALENDAR_SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]

# Read-only again, and the narrowest scope that still works. ``gmail.metadata`` would cover
# the sender and subject checks but not the message body, and the Meet link only exists in
# the body.
GMAIL_READONLY_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

# Only requested when ``ORCH_GMAIL__MARK_AS_READ`` is on, since it grants write access to
# the mailbox. Local state is what actually prevents double-joins; marking read is a
# convenience for humans watching the inbox, so it should not widen the scope by default.
GMAIL_MODIFY_SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]


class GoogleAuthSettings(BaseModel):
    """Credentials for the Google Calendar API.

    Two mutually exclusive modes, picked by ``auth_mode``:

    * ``service_account`` — a Workspace service account impersonating the bot's mailbox via
      domain-wide delegation. No browser, no refresh token to babysit; the right choice for
      an unattended service polling a Workspace-managed bot account.
    * ``oauth`` — a normal OAuth2 user credential for ``bot@mydomain.com``. Needed when the
      bot account is a plain Google account (not Workspace-managed) or domain-wide
      delegation isn't available. Requires a one-time interactive consent
      (``scripts/oauth_bootstrap.py``) that produces a refreshable ``token.json``.

    See ``README.md`` for the full setup walkthrough for both.
    """

    auth_mode: Literal["service_account", "oauth"] = "service_account"

    # --- service_account mode ---
    service_account_file: Path | None = None
    delegated_subject: str = ""
    """The bot's mailbox, e.g. ``bot@mydomain.com``. Domain-wide delegation lets the service
    account impersonate this user; without it, Calendar API calls would hit the service
    account's own (nonexistent) calendar instead of the bot's."""

    # --- oauth mode ---
    oauth_client_secret_file: Path | None = None
    oauth_token_file: Path = Path("credentials/token.json")

    @model_validator(mode="after")
    def _check_mode_is_configured(self) -> GoogleAuthSettings:
        if self.auth_mode == "service_account":
            if not self.service_account_file:
                raise ValueError(
                    "ORCH_GOOGLE__AUTH_MODE=service_account requires "
                    "ORCH_GOOGLE__SERVICE_ACCOUNT_FILE"
                )
            if not self.delegated_subject:
                raise ValueError(
                    "ORCH_GOOGLE__AUTH_MODE=service_account requires "
                    "ORCH_GOOGLE__DELEGATED_SUBJECT (the bot's mailbox to impersonate)"
                )
        elif self.auth_mode == "oauth" and not self.oauth_client_secret_file:
            raise ValueError(
                "ORCH_GOOGLE__AUTH_MODE=oauth requires ORCH_GOOGLE__OAUTH_CLIENT_SECRET_FILE"
            )
        return self


class BridgeSettings(BaseModel):
    """The existing meeting-connectors bridge this service triggers."""

    url: str = "http://localhost:8000/sessions"
    platform: str = "google_meet"
    timeout_s: float = Field(default=10.0, gt=0)
    max_retries: int = Field(default=2, ge=0)
    retry_backoff_s: float = Field(default=2.0, gt=0)


class SchedulingSettings(BaseModel):
    """How far ahead to look and how early to join."""

    poll_interval_s: int = Field(default=120, ge=15)
    """How often to re-sync against Google Calendar. Short enough to pick up a
    just-created or just-rescheduled meeting well before it starts."""

    lookahead_hours: int = Field(default=24, ge=1)
    """Only events starting within this window are scheduled. Bounds the sync response
    size and avoids holding scheduler jobs for meetings a day of reschedules away."""

    join_lead_time_s: int = Field(default=60, ge=0)
    """Trigger the bot this many seconds before the event's start time."""

    late_join_grace_s: int = Field(default=120, ge=0)
    """If the join moment has already passed (service was down, poll interval overlapped
    a fast-approaching meeting) but by less than this, join immediately instead of
    skipping. Beyond this window the meeting is treated as missed and logged, not joined
    late into a meeting that may already be well underway."""

    misfire_grace_s: int = Field(default=30, ge=0)
    """APScheduler's own tolerance for a job that fires late (event loop briefly busy)."""


class GmailSettings(BaseModel):
    """Polling the bot's inbox for Google Meet "instant invites".

    Covers the case the calendar poller structurally cannot: someone clicks **Add people**
    in a meeting that is *already running*. No calendar event is created, so there is
    nothing for ``calendar_service`` to find — the only artifact is an email to the bot's
    inbox from Google Meet.

    Same shape as the calendar path on purpose: an APScheduler interval job, a stateless
    query against Google, and a durable dedup file. Nothing here is push-based, so there is
    no public HTTPS endpoint, no Pub/Sub topic, and no subscription to keep alive.

    Off by default. Enabling it requires a Gmail scope on the bot's credential, which
    existing deployments do not have — see README.md, "Instant invites".
    """

    enabled: bool = False

    poll_interval_s: float = Field(default=5.0, gt=0)
    """How often to check the inbox. Fast because an instant invite is only useful if the
    bot turns up while the meeting is still going.

    Cheap enough to leave here: ``messages.list`` costs 5 quota units, so a 5s interval is
    ~1 unit/second against a 250 units/second per-user ceiling — roughly 0.4% of the budget,
    and about 0.01% of the daily one. It is the poll *cadence* that costs nothing; each
    message actually fetched costs another 5 units, which is why the query is narrowed to
    the invite sender rather than filtering a broad result set in Python."""

    allowed_senders: tuple[str, ...] = ("meetings-noreply@google.com",)
    """Only mail whose ``From:`` address matches exactly is considered, and the value is
    pushed into the Gmail query so the poll never retrieves anything else.

    **This is the security boundary.** Everything downstream makes the bot join a meeting,
    so anything a human could forge — a forwarded invite, a lookalike domain — must fail
    here."""

    subject_markers: tuple[str, ...] = (
        "Happening now:",
        "is inviting you to a video call",
    )
    """Matched case-insensitively against the subject; any one hit is enough. Meet uses the
    same sender for mail that must not trigger a join (recordings ready, missed call), so
    the sender check alone is not sufficient."""

    unread_only: bool = True
    """Restrict the poll to unread mail. Narrows the query to the handful of messages that
    could plausibly be a new invite; local state is what actually guarantees dedup."""

    max_results: int = Field(default=10, ge=1, le=100)
    """Cap on messages examined per poll. At a 5s cadence the real number is almost always
    0 or 1, so this only bounds the damage from an unexpected burst."""

    max_invite_age_s: int = Field(default=600, ge=30)
    """Ignore invites older than this, measured from Gmail's ``internalDate``.

    Load-bearing on first run. A mailbox with a backlog of old unread Meet invites would
    otherwise have every one of them fired at the bridge the moment the feature is switched
    on, joining meetings that ended days ago. It also bounds retries: a message that keeps
    failing eventually ages out instead of being retried forever."""

    max_attempts: int = Field(default=3, ge=1)
    """How many polls may retry one invite whose join failed, before giving up on it.
    Without a cap a bridge outage would retry the same invite every ``poll_interval_s``
    until it aged out."""

    state_file: Path = Path("state/processed_invites.json")
    """Durable set of already-handled message ids. Separate from ``ORCH_STATE_FILE`` so the
    calendar path's dedup state and this one cannot corrupt each other."""

    seen_limit: int = Field(default=500, ge=50)
    """How many processed message ids to retain. A dedup window, not an archive — combined
    with ``max_invite_age_s`` an evicted id can never come back as a live invite."""

    mark_as_read: bool = False
    """Also mark handled invites read in Gmail. Requires the broader ``gmail.modify`` scope,
    so it is opt-in; dedup does not depend on it."""

    join_dedupe_ttl_s: int = Field(default=900, ge=0)
    """In-process guard against joining the same meeting code twice in quick succession —
    two people clicking "Add people" produces two emails and one meeting."""


class Settings(BaseSettings):
    """Root application settings."""

    model_config = SettingsConfigDict(
        env_prefix="ORCH_",
        env_nested_delimiter="__",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    log_level: str = "INFO"
    calendar_id: str = "primary"
    state_file: Path = Path("state/triggered_events.json")

    google: GoogleAuthSettings = Field(default_factory=GoogleAuthSettings)
    bridge: BridgeSettings = Field(default_factory=BridgeSettings)
    scheduling: SchedulingSettings = Field(default_factory=SchedulingSettings)
    gmail: GmailSettings = Field(default_factory=GmailSettings)

    def required_scopes(self) -> list[str]:
        """Scopes the single Google credential must carry for the enabled features.

        One credential serves both APIs, so the Gmail scope is only requested when instant
        invites are on. That is what keeps this backward compatible: a deployment that
        leaves ``ORCH_GMAIL__ENABLED`` at its default asks for exactly the scopes it asked
        for before, and its existing token keeps working untouched.
        """
        scopes = list(CALENDAR_SCOPES)
        if self.gmail.enabled:
            scopes += GMAIL_MODIFY_SCOPES if self.gmail.mark_as_read else GMAIL_READONLY_SCOPES
        return scopes
