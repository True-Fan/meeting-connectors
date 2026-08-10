"""Google credential construction for the two supported auth modes.

Kept separate from ``calendar_service.py`` so the "how do we authenticate" question and
the "how do we read events" question can be reasoned about independently, and so
``scripts/oauth_bootstrap.py`` can reuse the OAuth path without importing the calendar
client at all.
"""

from __future__ import annotations

import logging

from google.oauth2 import service_account
from google.oauth2.credentials import Credentials as UserCredentials

from app.config import CALENDAR_SCOPES, GoogleAuthSettings

logger = logging.getLogger(__name__)


class CredentialError(RuntimeError):
    """Raised when credentials can't be constructed or loaded — always fatal at startup."""


def build_credentials(settings: GoogleAuthSettings) -> service_account.Credentials | UserCredentials:
    """Build Google API credentials for whichever mode ``settings.auth_mode`` selects.

    ``Settings`` validation already guarantees the required fields are present for the
    chosen mode (see ``GoogleAuthSettings._check_mode_is_configured``), so failures here are
    about the *contents* of those files, not missing configuration.
    """
    if settings.auth_mode == "service_account":
        return _service_account_credentials(settings)
    return _oauth_credentials(settings)


def _service_account_credentials(settings: GoogleAuthSettings) -> service_account.Credentials:
    assert settings.service_account_file is not None  # enforced by Settings validation
    if not settings.service_account_file.exists():
        raise CredentialError(f"service account file not found: {settings.service_account_file}")
    try:
        creds = service_account.Credentials.from_service_account_file(
            str(settings.service_account_file), scopes=CALENDAR_SCOPES
        )
    except (ValueError, OSError) as exc:
        raise CredentialError(f"invalid service account file: {exc}") from exc
    # Domain-wide delegation: without this the service account calls its own (nonexistent)
    # calendar rather than the bot mailbox's.
    return creds.with_subject(settings.delegated_subject)


def _oauth_credentials(settings: GoogleAuthSettings) -> UserCredentials:
    if not settings.oauth_token_file.exists():
        raise CredentialError(
            f"no OAuth token at {settings.oauth_token_file}. Run "
            "`python scripts/oauth_bootstrap.py` once to create it — see README.md."
        )
    creds = UserCredentials.from_authorized_user_file(
        str(settings.oauth_token_file), scopes=CALENDAR_SCOPES
    )
    if creds.expired and creds.refresh_token:
        from google.auth.transport.requests import Request

        creds.refresh(Request())
        settings.oauth_token_file.write_text(creds.to_json(), encoding="utf-8")
        logger.info("refreshed OAuth token at %s", settings.oauth_token_file)
    return creds
