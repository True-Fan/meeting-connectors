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
    def _check_mode_is_configured(self) -> "GoogleAuthSettings":
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
