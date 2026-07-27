"""Streamable HTTP + OAuth 2.1 + sampling + elicitation client — Inbox
Guardian Step 7.

Carries Step 6's transport/OAuth/progress/cancellation/sampling client
forward unmodified (see demonstrate_progress/demonstrate_cancellation/
sampling_handler below) and adds the one thing genuinely new this step: a
handler for `elicitation/create` requests, initiated by the server when
`search_drive_files` hits a genuinely ambiguous match it refuses to
guess on.

`sampling_handler` (unchanged from Step 6) and `elicitation_handler`
(new) look superficially similar -- both are server-initiated, both run
mid-`tools/call` -- but they are not the same primitive, and this file
keeps them as two separate functions on purpose:

  - `sampling_handler` fulfills a request for a *model completion*: it
    prints a prompt, gets a yes/no approval to spend money on an LLM
    call, and on approval calls Cerebras and hands back unstructured
    text wrapped in `CreateMessageResult`.
  - `elicitation_handler` fulfills a request for a *human decision*: it
    prints a message plus the server's declared JSON-Schema-shaped
    `requestedSchema`, prompts the human to choose one of the schema's
    enum values (or explicitly decline/cancel), and hands back
    `ElicitResult` with an `action` of `"accept"`, `"decline"`, or
    `"cancel"` -- no model is called anywhere in this function.

Both the approval gate in `sampling_handler` and the real
accept/decline/cancel branches in `elicitation_handler` are tested, not
theoretical -- see README's evidence for each.
"""

import asyncio
import json
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


# --- Elicitation: this client fulfilling server-initiated human prompts ---


async def elicitation_handler(
    context: RequestContext["ClientSession", Any],
    params: types.ElicitRequestParams,
) -> types.ElicitResult | types.ErrorData:
    """Fulfill one elicitation/create request from the server.

    This is Step 7's inversion: the server, mid-way through
    search_drive_files, has found two or more candidates its own
    sampling-based ranking couldn't safely pick between, and is asking
    *this client* to put the question to an actual human. Passing a real
    function here (instead of leaving ClientSession's default, which
    always returns ErrorData) is what makes this client declare the
    `elicitation` capability during initialize -- the same mechanism
    Step 6's sampling_callback used for the `sampling` capability, just
    for a different one.

    Only form-mode elicitation is handled -- the only mode this project's
    server ever sends (see server.py's `_disambiguate_with_human`, which
    calls `session.elicit_form`, never `elicit_url`). `params.mode` is
    checked explicitly rather than assumed, so a future URL-mode request
    fails loudly instead of this handler misreading a URL field that
    isn't there.

    The schema is rendered to the human, not just the message text --
    printing only `params.message` and reading free-text stdin would be
    "elicitation in name only" (the exact pitfall this step calls out).
    Concretely: `requestedSchema["properties"]["chosen_file_id"]["enum"]`
    is turned into a numbered menu, and the human's choice is validated
    against that same enum before this function ever returns it -- an
    out-of-menu answer is re-prompted, not silently coerced.

    Three real, distinct return paths, not two: MCP's `ElicitResult.action`
    is `"accept"` | `"decline"` | `"cancel"`, and this handler lets the
    human pick any of the three explicitly (a numbered choice, "decline",
    or Ctrl-C / empty input for cancel) rather than collapsing decline and
    cancel into one "not accept" bucket the way a bool would.
    """
    print("\n=== Incoming elicitation/create request (server -> client) ===")
    print(f"  message: {params.message}")

    if params.mode != "form":
        print(f"  Unsupported elicitation mode {params.mode!r} -- this client only handles form mode.\n")
        return types.ErrorData(code=types.INVALID_REQUEST, message=f"Unsupported elicitation mode: {params.mode}")

    schema = params.requestedSchema
    # This project's only elicitation request shape (see
    # _disambiguate_with_human) is a single required "chosen_file_id"
    # property with an enum -- this handler is written against that
    # shape rather than a generic form renderer, since building a fully
    # generic JSON-Schema-to-terminal-form renderer is out of scope for
    # proving the elicitation primitive itself.
    props = schema.get("properties", {})
    field_name = next(iter(props), None)
    enum_values = props.get(field_name, {}).get("enum") if field_name else None

    if not field_name or not enum_values:
        print(f"  Don't know how to render this schema: {schema}\n")
        return types.ErrorData(code=types.INVALID_REQUEST, message="Client cannot render this requestedSchema")

    print(f"  requestedSchema: {json.dumps(schema, indent=2)}")
    print("\n  Options:")
    for i, value in enumerate(enum_values, start=1):
        print(f"    {i}. {value}")

    def _prompt() -> str:
        return input(
            f"\n  Enter a number (1-{len(enum_values)}), 'decline', or Ctrl-D/empty to cancel: "
        ).strip()

    while True:
        try:
            raw = await anyio.to_thread.run_sync(_prompt)
        except EOFError:
            raw = ""

        if not raw:
            print("  Cancelled -- no choice made.\n")
            return types.ElicitResult(action="cancel")

        if raw.lower() == "decline":
            print("  Declined -- server told neither candidate is right, will not resolve the ambiguity.\n")
            return types.ElicitResult(action="decline")

        if raw.isdigit() and 1 <= int(raw) <= len(enum_values):
            chosen = enum_values[int(raw) - 1]
            print(f"  Chosen: {chosen!r} -- validated against the schema's enum before returning.\n")
            return types.ElicitResult(action="accept", content={field_name: chosen})

        print(f"  Not a valid option (must be 1-{len(enum_values)}, 'decline', or empty) -- try again.")


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


async def _search_drive_files(session: ClientSession, query: str) -> list[dict] | dict | None:
    """Call search_drive_files and return its parsed JSON, or None if the
    call came back as a tool-level error (isError=True) -- which is
    exactly what happens when the human denies the sampling request the
    tool depends on (see server.py's _rank_candidates_by_relevance: the
    McpError raised by a denied create_message() propagates up and the
    low-level Server's call_tool wrapper turns it into
    CallToolResult(isError=True), same conversion Step 1 relies on for
    invalid input). This is NOT dead code exercised only in theory --
    denying really does take this path; see README's denial-path
    evidence.

    The parsed result is a `list` of matches in the normal case, or a
    `dict` with `"ambiguous": True` when the server hit a genuinely
    ambiguous match and the human declined/cancelled disambiguation (see
    server.py's handle_call_tool) -- that shape difference is itself part
    of the proof the decline path isn't silently defaulting to a match.
    """
    result = await session.call_tool("search_drive_files", {"query": query})
    if result.isError:
        print(f"  search_drive_files returned an error (isError=True): {result.content[0].text}\n")
        return None

    return json.loads(result.content[0].text)


async def demonstrate_sampling_ranking(session: ClientSession) -> list[dict] | None:
    """Search with a query where naive Drive order and relevance order
    actually differ, but where there's exactly one right answer -- no
    ambiguity, just ranking. None of this account's file *names* contain
    "PDF" wording (the only clue is the lowercase `.pdf` extension), so
    there's no keyword overlap for a naive match to even key off of, but
    exactly one candidate is a PDF. This is Step 6's primitive on its
    own, still exercised here as a clean baseline before the ambiguous
    case below: sampling ranks, no elicitation fires, one match comes back.
    """
    print("=== search_drive_files: sampling-ranked search (no ambiguity expected) ===")
    query = "a PDF file"
    print(f"  query: {query!r}\n")

    outcome = await _search_drive_files(session, query)
    if outcome is None:
        return None
    if isinstance(outcome, dict):
        raise RuntimeError(f"expected an unambiguous match for {query!r}, got an ambiguity marker: {outcome}")

    print("=== Ranked result (server -> client, after the sampling round-trip) ===")
    for i, match in enumerate(outcome, start=1):
        print(f"  {i}. {match['name']} ({match['mimeType']})")
    print(
        "  See server.py's log for the raw (unranked) Drive order logged just "
        "before the sampling call -- comparing the two is the proof ranking "
        "actually changed the output, not just that a call happened.\n"
    )
    return outcome


async def demonstrate_elicitation_ambiguity(session: ClientSession) -> None:
    """Search with the query this account's two refund-policy docs were
    deliberately set up to conflict on: "Refund Policy" (refund to the
    original payment method, 30-day window) and "Return & Refund Policy"
    (store credit only, 14-day window) are both plausibly what this
    query means, but acting on the wrong one hands the user the wrong
    remedy and the wrong deadline -- a materially different, genuinely
    ambiguous case, not an invented toy one.

    This is what Step 7 exists for: sampling alone (Step 6) can rank
    both docs as relevant, but ranking can't decide which one the user
    actually meant. Instead of guessing, the server pauses mid-tool-call
    and sends elicitation/create, handled here by `elicitation_handler`
    -- printed prompts below are real, not simulated; run this twice to
    see both the accept path and the decline/cancel path (see README).
    """
    print("=== search_drive_files: query that hits a genuine content conflict ===")
    query = "how do I get my money back for something I bought"
    print(f"  query: {query!r}")
    print(
        "  (this account has two refund-policy docs with conflicting terms -- expect an "
        "elicitation/create prompt below, not just a ranked list)\n"
    )

    outcome = await _search_drive_files(session, query)
    if outcome is None:
        return

    if isinstance(outcome, dict) and outcome.get("ambiguous"):
        print("=== Ambiguity NOT resolved (server -> client) -- abstained, did not guess ===")
        print(f"  {outcome['message']}")
        for candidate in outcome["candidates"]:
            print(f"    - {candidate['name']} ({candidate['file_id']})")
        print()
        return

    print("=== Result (server -> client), disambiguated by a human via elicitation ===")
    for i, match in enumerate(outcome, start=1):
        print(f"  {i}. {match['name']} ({match['mimeType']})")
    print()


async def main() -> None:
    auth = _make_oauth_auth()

    async with streamablehttp_client(SERVER_URL, auth=auth) as (read, write, _get_session_id):
        async with ClientSession(
            read,
            write,
            sampling_callback=sampling_handler,
            elicitation_callback=elicitation_handler,
        ) as session:
            await session.initialize()
            print("=== initialize: connected over Streamable HTTP, OAuth 2.1 token attached ===")
            print("    (sampling_callback set -> 'sampling' capability declared)")
            print("    (elicitation_callback set -> 'elicitation' capability declared)\n")

            tools = await session.list_tools()
            print("=== tools/list ===")
            for tool in tools.tools:
                print(f"  - {tool.name}")
            print()

            pdf_matches = await demonstrate_sampling_ranking(session)

            await demonstrate_elicitation_ambiguity(session)

            if not pdf_matches:
                raise RuntimeError(
                    "no PDF file found (either none exists in this Drive account, or the "
                    "sampling request was denied) -- can't demo download progress without one"
                )
            file_id = pdf_matches[0]["file_id"]

            await demonstrate_progress(session, file_id)
            await demonstrate_cancellation(session, file_id)


if __name__ == "__main__":
    asyncio.run(main())
