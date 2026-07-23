"""One-time OAuth setup for the Drive MCP server.

Run this manually, once, before starting server.py:

    ../../.venv/bin/python auth_setup.py

This is deliberately NOT invoked by server.py and NOT lazy-triggered on
the first tool call. Auth is an interactive, human-in-the-loop step (opens
a browser, requires consent) and mixing that into server startup or into
a tool call makes failures hard to reason about and hides a browser popup
behind whatever is calling the server. Do the flow here, once, and let the
server just load the resulting token.
"""

import sys
from pathlib import Path

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

CONFIG_DIR = Path.home() / ".drive-mcp"
KEYS_PATH = CONFIG_DIR / "gcp-oauth.keys.json"
TOKEN_PATH = CONFIG_DIR / "token.json"

# Read-only. Do not widen this without updating README.md's scope note.
SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]


def main() -> None:
    if not KEYS_PATH.exists():
        print(
            f"OAuth keys file not found. Please place your Google Cloud "
            f"OAuth client credentials at:\n\n"
            f"    {KEYS_PATH}\n\n"
            f"You can create one at https://console.cloud.google.com/apis/credentials\n"
            f"(OAuth client ID -> Desktop app), then download the JSON and save it "
            f"at the path above.",
            file=sys.stderr,
        )
        sys.exit(1)

    flow = InstalledAppFlow.from_client_secrets_file(str(KEYS_PATH), SCOPES)
    credentials: Credentials = flow.run_local_server(port=0)

    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    TOKEN_PATH.write_text(credentials.to_json())

    print(f"Success! Credentials saved to {TOKEN_PATH}")
    print(f"Scope granted: {', '.join(SCOPES)}")


if __name__ == "__main__":
    main()
