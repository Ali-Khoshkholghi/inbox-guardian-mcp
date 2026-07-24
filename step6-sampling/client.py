"""Streamable HTTP + OAuth 2.1 + sampling client — Inbox Guardian Step 6.

Carries Step 5's transport/OAuth/progress/cancellation client forward
unmodified (see demonstrate_progress/demonstrate_cancellation below) and
adds the one thing genuinely new this step: a handler for
sampling/createMessage requests initiated by the *server*, mid-tool-call.
Every previous request in this project has been client-initiated; this is
the first time this client has to receive a request rather than only
send one.

Two things make sampling real here, not a toy demo:

  1. Human-in-the-loop approval, spec-required, actually blocking.
     `sampling_handler` below prints the request's full content and
     blocks on an explicit approve/deny before ever touching a model.
     Tested both ways -- see README's denial-path evidence. A server
     must not get free, unsupervised LLM calls through this client just
     because it declared the sampling capability.
  2. A real LLM call on approval -- via Cerebras's OpenAI-compatible
     endpoint (`openai` SDK, `base_url=https://api.cerebras.ai/v1`,
     `CEREBRAS_API_KEY` from .env), not Anthropic. The response is
     mapped explicitly into MCP's `CreateMessageResult` shape
     (role/content/model/stopReason), not passed through raw -- the
     server only ever sees MCP-shaped data, never an OpenAI response
     object.
"""

import asyncio
import logging
import os
import sys
import webbrowser
from pathlib import Path
from typing import Any

import anyio
import httpx
import mcp.types as types
from dotenv import load_dotenv
from mcp import ClientSession
from mcp.client.auth.oauth2 import OAuthClientProvider, TokenStorage
from mcp.client.streamable_http import streamablehttp_client
from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata, OAuthToken
from mcp.shared.context import RequestContext
from mcp.shared.exceptions import McpError
from openai import OpenAI

STEP_DIR = Path(__file__).parent
REPO_ROOT = STEP_DIR.parent
SERVER_URL = "http://127.0.0.1:8780/mcp"
CALLBACK_HOST = "127.0.0.1"
CALLBACK_PORT = 8771
CEREBRAS_MODEL = "gpt-oss-120b"

logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,
    format="%(asctime)s [client] %(levelname)s %(message)s",
)
logger = logging.getLogger("inbox-guardian.step6.client")

load_dotenv(REPO_ROOT / ".env")


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


# --- Sampling: this client fulfilling server-initiated LLM requests -------


def _make_cerebras_client() -> OpenAI:
    api_key = os.environ.get("CEREBRAS_API_KEY")
    if not api_key:
        raise RuntimeError("CEREBRAS_API_KEY not set -- check .env at the repo root")
    return OpenAI(api_key=api_key, base_url="https://api.cerebras.ai/v1")


_cerebras_client = _make_cerebras_client()

# OpenAI's chat-completion finish_reason strings -> MCP's StopReason. MCP
# allows any string here (StopReason = Literal[...] | str), but mapping the
# ones we actually expect keeps the server-facing value meaningful rather
# than leaking an OpenAI-specific term into an MCP-typed field.
_FINISH_REASON_TO_STOP_REASON = {
    "stop": "endTurn",
    "length": "maxTokens",
}


def _sampling_message_to_text(message: types.SamplingMessage) -> str:
    content = message.content
    if isinstance(content, types.TextContent):
        return content.text
    if isinstance(content, list):
        return "\n".join(c.text for c in content if isinstance(c, types.TextContent))
    return str(content)


async def sampling_handler(
    context: RequestContext["ClientSession", Any],
    params: types.CreateMessageRequestParams,
) -> types.CreateMessageResult | types.ErrorData:
    """Fulfill one sampling/createMessage request from the server.

    This is the direction inversion Step 6 is about: the server, mid-way
    through handling search_drive_files, is asking *this client* to run
    an LLM completion on its behalf. Passing a real function here (as
    opposed to leaving ClientSession's default, which always returns
    ErrorData) is also what makes this client declare the `sampling`
    capability during initialize -- see ClientSession.initialize()'s own
    logic for exactly that condition.

    Human-in-the-loop approval is not optional per spec, and is not a
    no-op here: the request's content is printed and an explicit
    approve/deny is required before any model call happens. Both paths
    are real -- denial returns ErrorData and the server has to cope with
    getting nothing back (see README for that proof), not just the happy
    path always being taken.
    """
    print("\n=== Incoming sampling/createMessage request (server -> client) ===")
    if params.systemPrompt:
        print(f"  system: {params.systemPrompt}")
    for message in params.messages:
        print(f"  [{message.role}] {_sampling_message_to_text(message)}")
    print(f"  max_tokens: {params.maxTokens}")

    # input() and the OpenAI SDK call below are both blocking -- run them
    # off the event loop so this coroutine doesn't freeze the whole
    # session (which is still servicing the SSE stream concurrently).
    decision = await anyio.to_thread.run_sync(lambda: input("\nApprove this sampling request? [y/N] ").strip().lower())
    if decision != "y":
        print("  Denied -- server gets an error response, not a completion.\n")
        return types.ErrorData(code=types.INVALID_REQUEST, message="User denied the sampling request")

    print(f"  Approved -- calling Cerebras ({CEREBRAS_MODEL})...")
    openai_messages: list[dict] = []
    if params.systemPrompt:
        openai_messages.append({"role": "system", "content": params.systemPrompt})
    openai_messages.extend({"role": m.role, "content": _sampling_message_to_text(m)} for m in params.messages)

    completion = await anyio.to_thread.run_sync(
        lambda: _cerebras_client.chat.completions.create(
            model=CEREBRAS_MODEL,
            messages=openai_messages,
            max_tokens=params.maxTokens,
        )
    )
    choice = completion.choices[0]
    response_text = choice.message.content or ""
    print(f"  Cerebras response: {response_text}\n")

    # Explicit mapping into MCP's CreateMessageResult -- not a raw pass-
    # through of the OpenAI response object, which has no `role`/`model`/
    # `stopReason` fields shaped the way MCP expects.
    return types.CreateMessageResult(
        role="assistant",
        content=types.TextContent(type="text", text=response_text),
        model=completion.model,
        stopReason=_FINISH_REASON_TO_STOP_REASON.get(choice.finish_reason, choice.finish_reason),
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


async def _search_drive_files(session: ClientSession, query: str) -> list[dict] | None:
    """Call search_drive_files and return the ranked matches, or None if
    the call came back as a tool-level error (isError=True) -- which is
    exactly what happens when the human denies the sampling request the
    tool depends on (see server.py's _rank_candidates_by_relevance: the
    McpError raised by a denied create_message() propagates up and the
    low-level Server's call_tool wrapper turns it into
    CallToolResult(isError=True), same conversion Step 1 relies on for
    invalid input). This is NOT dead code exercised only in theory --
    denying really does take this path; see README's denial-path
    evidence.
    """
    result = await session.call_tool("search_drive_files", {"query": query})
    if result.isError:
        print(f"  search_drive_files returned an error (isError=True): {result.content[0].text}\n")
        return None

    import json

    return json.loads(result.content[0].text)


async def demonstrate_sampling_ranking(session: ClientSession) -> list[dict] | None:
    """Search with a query where naive Drive order and relevance order
    actually differ: none of this account's file *names* literally
    contain "money" or "refund" wording from the query, so there's no
    keyword overlap for a naive match to even key off of -- but the
    account's real "Refund Policy" doc is obviously the relevant one to
    a human, and to an LLM. This is the case sampling is for: Drive's own
    query matching can't make this connection, ranking by actual intent
    can.
    """
    print("=== search_drive_files: sampling-ranked search ===")
    query = "how do I get my money back for something I bought"
    print(f"  query: {query!r}\n")

    ranked = await _search_drive_files(session, query)
    if ranked is None:
        return None

    print("=== Ranked result (server -> client, after the sampling round-trip) ===")
    for i, match in enumerate(ranked, start=1):
        print(f"  {i}. {match['name']} ({match['mimeType']})")
    print(
        "  See server.py's log for the raw (unranked) Drive order logged just "
        "before the sampling call -- comparing the two is the proof ranking "
        "actually changed the output, not just that a call happened.\n"
    )
    return ranked


async def main() -> None:
    auth = _make_oauth_auth()

    async with streamablehttp_client(SERVER_URL, auth=auth) as (read, write, _get_session_id):
        async with ClientSession(read, write, sampling_callback=sampling_handler) as session:
            await session.initialize()
            print("=== initialize: connected over Streamable HTTP, OAuth 2.1 token attached ===")
            print("    (sampling_callback set -> this client declared the 'sampling' capability)\n")

            tools = await session.list_tools()
            print("=== tools/list ===")
            for tool in tools.tools:
                print(f"  - {tool.name}")
            print()

            await demonstrate_sampling_ranking(session)

            print("=== search_drive_files: second sampling-ranked search, to find a PDF for the progress demo ===")
            pdf_matches = await _search_drive_files(session, "a PDF file")
            if not pdf_matches:
                raise RuntimeError(
                    "no PDF file found (either none exists in this Drive account, or the "
                    "sampling request was denied) -- can't demo download progress without one"
                )
            file_id = pdf_matches[0]["file_id"]
            print(f"  found {pdf_matches[0]['name']!r} (file_id={file_id})\n")

            await demonstrate_progress(session, file_id)
            await demonstrate_cancellation(session, file_id)


if __name__ == "__main__":
    asyncio.run(main())
