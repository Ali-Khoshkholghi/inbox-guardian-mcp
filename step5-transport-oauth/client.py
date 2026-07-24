"""Streamable HTTP + OAuth 2.1 client — Inbox Guardian Step 5.

Talks to server.py over Streamable HTTP instead of spawning a stdio
subprocess (Steps 1-4's pattern). Demonstrates, in order, the three
things this step adds on top of the Step 3 transport:

  1. Progress notifications: download_drive_file reports real
     byte-level progress as notifications/progress, asynchronously
     relative to the eventual tools/call response -- this client prints
     each update as it arrives, not just the final result.
  2. Cancellation: a second download is started and then cancelled
     mid-flight via notifications/cancelled. The proof this actually
     stopped server-side work (not just that this client gave up
     waiting) is in server.py's own log, not in anything printed here --
     see README.
  3. OAuth 2.1: every request above happens over a session obtained
     through mcp.client.auth.oauth2.OAuthClientProvider, an httpx.Auth
     implementation already part of the mcp SDK that performs the full
     401 -> protected-resource discovery -> dynamic client registration
     -> PKCE authorization-code flow -> Bearer token dance. That's SDK
     machinery, not a vendored auth layer; hand-rolling the plumbing
     that discovers/redirects/exchanges per RFC 9728 + RFC 7636 would
     just be re-deriving what a compliant client is supposed to do,
     with no more insight than reading the module it's built from.
"""

import asyncio
import logging
import sys
import webbrowser
from pathlib import Path

import httpx
import mcp.types as types
from mcp import ClientSession
from mcp.client.auth.oauth2 import OAuthClientProvider, TokenStorage
from mcp.client.streamable_http import streamablehttp_client
from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata, OAuthToken
from mcp.shared.exceptions import McpError

STEP_DIR = Path(__file__).parent
SERVER_URL = "http://127.0.0.1:8770/mcp"
CALLBACK_HOST = "127.0.0.1"
CALLBACK_PORT = 8771

logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,
    format="%(asctime)s [client] %(levelname)s %(message)s",
)
logger = logging.getLogger("inbox-guardian.step5.client")


# --- OAuth plumbing: token storage + browser redirect + local callback ----


class InMemoryTokenStorage(TokenStorage):
    """Tokens live only for this process's run -- there is no persisted
    credential file the way Drive/Gmail's stdio servers have, since the
    point here is to exercise the OAuth dance itself each run, not to
    skip it on subsequent runs."""

    def __init__(self) -> None:
        self._tokens: OAuthToken | None = None
        self._client_info: OAuthClientInformationFull | None = None

    async def get_tokens(self) -> OAuthToken | None:
        return self._tokens

    async def set_tokens(self, tokens: OAuthToken) -> None:
        self._tokens = tokens

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        return self._client_info

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        self._client_info = client_info


class _OAuthCallbackServer:
    """A one-shot local HTTP listener for the browser's redirect back from
    /consent, the same pattern Drive's own auth_setup.py uses via
    google_auth_oauthlib's run_local_server -- except this is our own
    tiny implementation, since the code being captured here is this
    server's own authorization code, not Google's."""

    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = port
        self._code: str | None = None
        self._state: str | None = None
        self._received = asyncio.Event()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        request_line = await reader.readline()
        try:
            _, path, _ = request_line.decode().split(" ", 2)
        except ValueError:
            path = "/"
        while True:
            line = await reader.readline()
            if not line or line in (b"\r\n", b"\n"):
                break

        query = httpx.URL(f"http://ignored{path}").params
        self._code = query.get("code")
        self._state = query.get("state")

        body = b"<html><body>Authorized. You can close this tab and return to the terminal.</body></html>"
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: "
            + str(len(body)).encode()
            + b"\r\n\r\n"
            + body
        )
        await writer.drain()
        writer.close()
        self._received.set()

    async def wait_for_callback(self) -> tuple[str, str | None]:
        server = await asyncio.start_server(self._handle, self.host, self.port)
        async with server:
            await self._received.wait()
        if self._code is None:
            raise RuntimeError("OAuth callback completed with no authorization code")
        return self._code, self._state


async def _redirect_handler(authorization_url: str) -> None:
    print(f"\nOpening browser for consent: {authorization_url}\n")
    webbrowser.open(authorization_url)


def _make_oauth_auth() -> OAuthClientProvider:
    callback_server = _OAuthCallbackServer(CALLBACK_HOST, CALLBACK_PORT)

    return OAuthClientProvider(
        server_url=SERVER_URL,
        client_metadata=OAuthClientMetadata(
            client_name="inbox-guardian-step5-client",
            redirect_uris=[f"http://{CALLBACK_HOST}:{CALLBACK_PORT}/callback"],
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            scope="drive:read",
        ),
        storage=InMemoryTokenStorage(),
        redirect_handler=_redirect_handler,
        callback_handler=callback_server.wait_for_callback,
    )


# --- Demonstrations --------------------------------------------------------


def _print_progress(chunks_seen: list[tuple[float, float | None]]):
    async def callback(progress: float, total: float | None, message: str | None) -> None:
        chunks_seen.append((progress, total))
        pct = f"{progress / total * 100:.0f}%" if total else "?"
        print(f"  [progress] {progress:.0f}/{total if total else '?'} bytes ({pct})")

    return callback


async def demonstrate_progress(session: ClientSession, file_id: str) -> None:
    print("=== Progress notifications: full download, printed as they arrive ===")
    chunks_seen: list[tuple[float, float | None]] = []
    result = await session.call_tool(
        "download_drive_file",
        {"file_id": file_id},
        progress_callback=_print_progress(chunks_seen),
    )
    print(f"  final result: {result.content[0].text}")
    assert len(chunks_seen) > 1, "expected more than one progress notification for a chunked download"
    print(f"  OK: received {len(chunks_seen)} progress notifications before the final response.\n")


async def demonstrate_cancellation(session: ClientSession, file_id: str) -> None:
    print("=== Cancellation: start a download, cancel it after 2 chunks ===")

    # send_request() (which call_tool() calls internally) assigns the
    # JSON-RPC request id synchronously, before any await -- reading it here
    # is the only way to learn, ahead of time, which id notifications/cancelled
    # must reference. The public API has no other way to learn a call's
    # request id before it completes; this is reading one layer into the
    # SDK's session bookkeeping, not guessing.
    predicted_request_id = session._request_id
    progress_events = 0

    async def callback(progress: float, total: float | None, message: str | None) -> None:
        nonlocal progress_events
        progress_events += 1
        print(f"  [progress] {progress:.0f}/{total if total else '?'} bytes -- chunk #{progress_events}")
        if progress_events == 2:
            print(f"  sending notifications/cancelled for request id {predicted_request_id}")
            # Sent from inside the progress callback, which the session's
            # own receive loop awaits directly (see
            # mcp/shared/session.py's _receive_loop) -- so this reaches the
            # server while the download is still mid-flight, not after.
            await session.send_notification(
                types.ClientNotification(
                    types.CancelledNotification(
                        params=types.CancelledNotificationParams(
                            requestId=predicted_request_id,
                            reason="demonstrating cancellation (Step 5 build task 3)",
                        )
                    )
                )
            )

    try:
        await session.call_tool(
            "download_drive_file",
            {"file_id": file_id},
            progress_callback=callback,
        )
        print("  UNEXPECTED: download completed instead of being cancelled")
    except McpError as exc:
        # The SDK's own session logic answers a cancelled request with an
        # explicit error response as soon as notifications/cancelled
        # arrives (see mcp/shared/session.py's RequestResponder.cancel) --
        # that's what raises here. It is NOT, by itself, proof the
        # server-side handler actually stopped; see server.py's own log
        # for that (no "requesting chunk 3" line ever appears after this
        # notification is sent -- the loop never got there).
        print(f"  download call ended with the server's cancellation response, as expected: {exc}")

    print(
        "  See server.py's log for the real proof: no further "
        "'requesting chunk N' lines appear after the cancellation was "
        "sent, meaning the handler actually stopped issuing Drive API "
        "calls -- not just that this client stopped waiting for one.\n"
    )


async def main() -> None:
    auth = _make_oauth_auth()

    async with streamablehttp_client(SERVER_URL, auth=auth) as (read, write, _get_session_id):
        async with ClientSession(read, write) as session:
            await session.initialize()
            print("=== initialize: connected over Streamable HTTP, OAuth 2.1 token attached ===\n")

            tools = await session.list_tools()
            print("=== tools/list ===")
            for tool in tools.tools:
                print(f"  - {tool.name}")
            print()

            search_result = await session.call_tool("search_drive_files", {"query": "mimeType='application/pdf'"})
            import json

            matches = json.loads(search_result.content[0].text)
            if not matches:
                raise RuntimeError("no PDF file found in this Drive account to demo download progress against")
            file_id = matches[0]["file_id"]
            print(f"=== search_drive_files: found {matches[0]['name']!r} (file_id={file_id}) ===\n")

            await demonstrate_progress(session, file_id)
            await demonstrate_cancellation(session, file_id)


if __name__ == "__main__":
    asyncio.run(main())
