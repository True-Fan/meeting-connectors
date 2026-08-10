#!/usr/bin/env python3
"""One-time interactive OAuth2 consent for ``ORCH_GOOGLE__AUTH_MODE=oauth``.

Opens a browser, asks you to sign in as the bot's Google account, and writes a refreshable
token to ``ORCH_GOOGLE__OAUTH_TOKEN_FILE`` (default ``credentials/token.json``). The
orchestrator service refreshes that token on its own afterward — this script only needs to
be run again if the token file is deleted or the OAuth client is revoked.

Usage:
    python scripts/oauth_bootstrap.py

Requires ``ORCH_GOOGLE__OAUTH_CLIENT_SECRET_FILE`` to point at an OAuth "Desktop app"
client secret downloaded from Google Cloud Console. See README.md for the full walkthrough.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Allow running as `python scripts/oauth_bootstrap.py` from the project root without
# installing the package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from google_auth_oauthlib.flow import InstalledAppFlow  # noqa: E402

from app.config import CALENDAR_SCOPES, Settings  # noqa: E402


def main() -> None:
    settings = Settings()
    if settings.google.auth_mode != "oauth":
        print(
            f"ORCH_GOOGLE__AUTH_MODE is {settings.google.auth_mode!r}, not 'oauth'. "
            "Set it to 'oauth' (and ORCH_GOOGLE__OAUTH_CLIENT_SECRET_FILE) before running "
            "this script.",
            file=sys.stderr,
        )
        raise SystemExit(1)

    client_secret_file = settings.google.oauth_client_secret_file
    if client_secret_file is None or not client_secret_file.exists():
        print(
            f"ORCH_GOOGLE__OAUTH_CLIENT_SECRET_FILE not found: {client_secret_file}",
            file=sys.stderr,
        )
        raise SystemExit(1)

    flow = InstalledAppFlow.from_client_secrets_file(str(client_secret_file), CALENDAR_SCOPES)
    print("Opening a browser for sign-in. Sign in as the BOT's Google account, not your own.")
    credentials = flow.run_local_server(port=0)

    token_file = settings.google.oauth_token_file
    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text(credentials.to_json(), encoding="utf-8")
    print(f"Wrote refreshable token to {token_file}")


if __name__ == "__main__":
    main()
