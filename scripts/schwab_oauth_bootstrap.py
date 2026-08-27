"""
One-time interactive Schwab OAuth bootstrap.

Schwab's OAuth flow has no server-to-server or device-code shortcut: a
human has to log into schwab.com in a browser, approve the app, and get
redirected back with a one-time authorization code in the URL. This script
walks through that once and prints the long-lived refresh token it gets
back, which is the only value that then goes into .env — nothing else in
this codebase runs this flow, and nothing here stores or transmits
credentials anywhere but this local terminal.

Usage:
    1. Fill in SCHWAB_APP_KEY, SCHWAB_APP_SECRET, and SCHWAB_REDIRECT_URI in
       .env (get the first two from your Schwab developer app; the redirect
       URI must exactly match what's registered on that app — it does NOT
       need to be a real, reachable server for this flow to work).
    2. Run: .venv/Scripts/python.exe scripts/schwab_oauth_bootstrap.py
    3. Open the printed URL in a browser, log into Schwab, and approve the
       app. Schwab redirects the browser to your redirect URI with
       ?code=...&state=... in the address bar — the page itself will
       likely show an error (nothing is listening there), which is
       expected and fine. Copy the full URL from the address bar.
    4. Paste that URL back into this script's prompt.
    5. Copy the printed refresh_token into .env as SCHWAB_REFRESH_TOKEN.

Schwab refresh tokens expire after 7 days of not being used to mint an
access token — see SchwabOAuthClient.get_access_token, which refreshes the
short-lived access token automatically on every call using this refresh
token, so normal use of a running server keeps it alive. If it does lapse,
BrokerAuthenticationError fires and the kill switch auto-trips (see
Executor._shutdown_on_auth_failure); re-run this script to get a new one.
"""

import asyncio
import secrets
import sys
from urllib.parse import parse_qs, urlparse

# Allow running this script directly (`python scripts/foo.py`) without
# having installed the package — add the repo root to sys.path.
sys.path.insert(0, __file__.rsplit("scripts", 1)[0] or ".")

from src.brokers.schwab.auth import SchwabOAuthClient  # noqa: E402
from src.config import get_settings  # noqa: E402


def _extract_code(pasted: str) -> str:
    """Accepts either the full redirected URL or just the bare code value."""
    pasted = pasted.strip()
    if "code=" not in pasted:
        return pasted  # assume they pasted the bare code
    query = parse_qs(urlparse(pasted).query)
    codes = query.get("code")
    if not codes:
        raise SystemExit("Could not find a 'code' parameter in the pasted URL.")
    return codes[0]


async def main() -> None:
    settings = get_settings()
    missing = [
        name
        for name, value in [
            ("SCHWAB_APP_KEY", settings.schwab_app_key),
            ("SCHWAB_APP_SECRET", settings.schwab_app_secret),
            ("SCHWAB_REDIRECT_URI", settings.schwab_redirect_uri),
        ]
        if not value
    ]
    if missing:
        raise SystemExit(
            f"Missing from .env before this can run: {', '.join(missing)}. "
            f"Get these from your Schwab developer app registration first."
        )

    oauth = SchwabOAuthClient(
        app_key=settings.schwab_app_key,
        app_secret=settings.schwab_app_secret,
        redirect_uri=settings.schwab_redirect_uri,
    )
    state = secrets.token_urlsafe(16)

    print("\nStep 1: open this URL in a browser and log in / approve the app:\n")
    print(f"    {oauth.authorization_url(state)}\n")
    print("Step 2: after approving, your browser lands on a URL starting with")
    print(f"    {settings.schwab_redirect_uri}")
    print("(the page itself may show an error — that's expected). Copy that full URL.\n")

    pasted = input("Paste the redirected URL (or just the 'code' value) here: ")
    code = _extract_code(pasted)

    print("\nExchanging authorization code for tokens...")
    data = await oauth.exchange_authorization_code(code)

    refresh_token = data.get("refresh_token")
    if not refresh_token:
        raise SystemExit(f"Schwab did not return a refresh_token. Raw response: {data}")

    print("\nSuccess. Add this to .env:\n")
    print(f"    SCHWAB_REFRESH_TOKEN={refresh_token}\n")
    print("Then set EXECUTION_MODE and SCHWAB_ACCOUNT_NUMBER (see .env.example) and restart the server.")


if __name__ == "__main__":
    asyncio.run(main())
