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
    """Fallback platform, used only when the link itself did not identify one.

    **No longer the platform every join is sent with.** A meeting's platform is a property of
    the invite — a Zoom link means ``zoom_web``, a Meet code means ``google_meet`` — so it is
    decided per meeting by ``meeting_link`` and this is what remains for the case where
    something reached the bridge without going through that. Left at its old default and its
    old name so an existing ``.env`` keeps working and keeps meaning the same thing."""

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

    allowed_senders: tuple[str, ...] = (
        "meetings-noreply@google.com",
        "no-reply@zoom.us",
        "noreply@zoom.us",
        "no-reply@zoom.com",
    )
    """Only mail whose ``From:`` address matches exactly is considered, and the value is
    pushed into the Gmail query so the poll never retrieves anything else.

    **This is the security boundary.** Everything downstream makes the bot join a meeting,
    so anything a human could forge — a lookalike domain, a spoofed display name — must fail
    here. Matched against the parsed address, never the raw header, because a display name
    reading ``meetings-noreply@google.com`` is something anyone can set.

    Three entry forms, widening in this order (see ``invite_parser.sender_allowed``):

    * ``someone@example.com`` — that mailbox only.
    * ``@example.com`` — anybody at that domain.
    * ``*`` — anybody at all.

    Zoom's addresses are additions, not replacements: a deployment that had this list at its
    default keeps accepting exactly the Meet invites it accepted before, and gains Zoom's
    *scheduled* invitations, which come from ``no-reply@zoom.us`` (or ``@zoom.com`` on some
    tenants — which one a given account uses is not something this service can discover).

    **A direct in-meeting invite does not come from Zoom, and that is why it needs a
    decision from you.** Clicking *Invite → Email* in a running meeting composes the mail
    from the **host's own mailbox**, so it arrives from a colleague at
    ``someone@example.com`` or ``someone@yourcompany.com``. There is no system address to
    allow-list, so out of the box that invite is ignored — which is the safe default and
    also, for most deployments, the wrong one.

    To accept them, name the senders you trust::

        ORCH_GMAIL__ALLOWED_SENDERS='["no-reply@zoom.us","meetings-noreply@google.com","@yourcompany.com"]'

    A domain entry is the proportionate choice: it grants the people who already share your
    Workspace and nobody else. ``*`` grants everyone who can reach the mailbox, which is
    reasonable for an unpublished test address and not for anything else."""

    subject_markers: tuple[str, ...] = (
        # Google Meet
        "Happening now:",
        "is inviting you to a video call",
        # Zoom's scheduled invitation, which really does come from Zoom's own address.
        "is inviting you to a scheduled zoom meeting",
        "zoom meeting invitation",
        "invitation: ",
    )
    """Matched case-insensitively against the subject; any one hit is enough.

    Both platforms use one sender address for mail that must not trigger a join — recording
    ready, missed call, cloud storage nearly full — so the sender check alone is not
    sufficient. This is the second half of that filter, and it is a substring match so a
    subject carrying the meeting's own title around the marker still hits."""

    any_sender_subject_markers: tuple[str, ...] = (
        "please join zoom meeting in progress",
    )
    """Subjects trusted enough to act on **whoever sent them**.

    This exists for one case, and it is the case the whole direct-invite feature is for:
    clicking **Invite → Email** inside a running Zoom meeting. No calendar event is created,
    so the calendar poller structurally cannot see it, and Zoom composes the mail from the
    **host's own mailbox** — so there is no system address to allow-list and no way to
    predict the sender. Identifying it by subject is the only handle there is.

    ``allowed_senders`` is skipped for a message matching one of these. Everything else still
    applies: the body must still yield a real Zoom link (``meeting_link``), the invite must
    still be newer than ``max_invite_age_s``, and the join is still de-duplicated against
    meetings the bridge already has.

    **What that costs, stated plainly.** Anyone who can email the bot, using this exact
    subject and a Zoom link, can make it join that meeting. There is no cryptographic
    difference between the host's invite and a stranger's — both are ordinary mail. The
    mitigations are that the subject is an exact Zoom string rather than anything a person
    would type by accident, that the bot's address is not usually published, and that
    ``max_invite_age_s`` bounds how long any one message stays actionable.

    **Emptying this tuple turns the behaviour off** and restores sender-only trust::

        ORCH_GMAIL__ANY_SENDER_SUBJECT_MARKERS='[]'

    To keep the subject working but only from known senders, move the string into
    ``subject_markers`` instead — that list is still gated by ``allowed_senders``."""

    accept_calendar_invitations: bool = True
    """Act on a Google Calendar invitation, whoever the organiser is.

    A calendar invitation has **neither** handle the other routes use: Google sends it *as
    the organiser*, so the ``From:`` is an ordinary personal address, and the subject is the
    event's own title — ``test zoom``, ``Standup``, anything, in any language. What it does
    have is an ``invite.ics`` part, and that is what identifies it. A structural fact beats a
    string somebody chose: it cannot be produced by accident and does not depend on wording.

    **Only invitations for a meeting that is already running are acted on**, which is the
    condition that keeps this from trampling the calendar poller. An invitation to next
    Tuesday's standup arrives *now* and would otherwise pass every filter — fresh mail, real
    link, genuine organiser — putting the bot in a meeting six days early. So the inbox path
    claims only what it alone can do: react to a meeting in progress, the case the calendar
    poller structurally cannot reach in time. Anything scheduled is left for that poller to
    join at ``join_lead_time_s`` before it starts.

    Exposure is the same shape as ``any_sender_subject_markers``: anyone who can email the
    bot a valid ``.ics`` for a live meeting with a Zoom or Meet link in it can make it join.
    Set false to require an allow-listed sender for these too."""

    accept_zoom_invite_bodies: bool = True
    """Act on a message whose **body** is a Zoom invitation, whoever sent it and whatever it
    is called.

    The last of the three routes in, and the one that catches what the other two structurally
    cannot: a Zoom meeting added to a calendar event. Its subject is the *event's* title —
    whatever the organiser typed, in any language — and it comes from their own mailbox, so
    neither the subject markers nor ``allowed_senders`` holds any signal. What is invariant is
    the invite text Zoom generates.

    The signature is a join link **plus** a labelled ``Meeting ID:`` or ``Passcode:`` line,
    not merely a link: a colleague writing "we used to meet at zoom.us/j/123456789" has a
    link and no invitation, and must not move the bot.

    **An ics, where the message has one, overrules this entirely** — including when it says
    the meeting is next week. Otherwise a body match would walk straight past the timing gate
    and join six days early.

    Where there is no ics there is no timing to check, so the bounds are the ones that always
    apply: ``max_invite_age_s`` (a stale message ages out), the join de-duplication, and the
    fact that the bot's address is not usually published. Exposure is the same shape as the
    other two open routes — anyone who can email the bot a Zoom invitation can make it join
    that meeting. Set false to require an allow-listed sender for these."""

    accept_teams_invite_bodies: bool = True
    """Act on a message whose **body** is a Microsoft Teams invitation, whoever sent it.

    The Teams counterpart of ``accept_zoom_invite_bodies``, and it is the route Teams needs
    most rather than least. Teams has no system sender to allow-list and no in-meeting "invite
    by email" with a fixed subject: a Teams meeting reaches a mailbox either as an Outlook
    calendar invitation (whose subject is the event's own title) or as a link somebody pasted.
    So for Teams the body is not a third route — it is the only one that does not depend on the
    organiser being known in advance.

    The signature is a join link plus a labelled line from the block Teams generates, with one
    Teams-specific addition: its short link carries the passcode in the URL
    (``teams.live.com/meet/<id>?p=<passcode>``), and that string only comes from *Copy link*,
    so it counts as an invitation on its own. ``has_teams_invite_block`` is where that is
    decided and where the trade-off is written out.

    **An ics, where the message has one, overrules this entirely** — including when it says the
    meeting is next week — exactly as it does for Zoom. Otherwise a body match would walk past
    the timing gate and join days early.

    Exposure is the same shape as the other open routes: anyone who can email the bot a Teams
    invitation can make it join that meeting, bounded by ``max_invite_age_s`` and the join
    de-duplication. Set false to require an allow-listed sender for these."""

    calendar_invite_lead_s: int = Field(default=300, ge=0)
    """How far before an invited meeting's start time it still counts as "happening now".

    Covers the ordinary case of somebody creating the event a few minutes before the meeting
    and the bot being expected in it at the top of the hour. Kept well under
    ``scheduling.join_lead_time_s``'s sibling concerns: this is about deciding *whether* the
    inbox path owns an invitation at all, not about when to join."""

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
