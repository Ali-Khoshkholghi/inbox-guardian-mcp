"""Composed Gmail + Drive MCP client -- Inbox Guardian Step 9.

The step brief assumes a single client already holds Gmail and Drive open
together with sampling/elicitation wired -- that combination doesn't
actually exist anywhere in Steps 1-8: Step 4's composition client held
Gmail+Drive together but neither `sampling_callback` nor
`elicitation_callback` set (so Step 8's `search_drive_files` would have no
way to rank candidates or ask a human); Step 8's client had both callbacks
wired but only ever talked to Drive. This module builds the missing
combination for real, by carrying each piece forward from where it was
actually built and proven, per this project's standing convention of each
step copying forward rather than importing across another step's folder:

  - Gmail: stdio subprocess (`npx -y @artymclabin/gmail-mcp`, the scope-
    narrowed fork switched to at this step -- see
    step3-real-servers/gmail/README.md), connection shape copied from
    step4-composition/client.py's `connect()`.
  - Drive: `streamablehttp_client` + OAuth 2.1 + `sampling_callback` +
    `elicitation_callback`, copied from step8-production/client.py's
    `InMemoryTokenStorage`/`_OAuthCallbackServer`/`_make_oauth_auth`/
    `sampling_handler`/`elicitation_handler`, unchanged in mechanism.
    Requires step8-production/server.py already running
    (`../step8-production/../.venv/bin/python server.py`) -- this module
    is a client, it does not spawn the Drive server itself, same as every
    prior step's client/server split.
  - Registry/dispatch: `"{server}::{tool_name}" -> (session, real_name)`,
    the exact pattern step4-composition/client.py's `build_registry`/
    `dispatch` established, so two servers exposing colliding bare tool
    names still can't shadow each other here either.

`dispatch()` here returns the raw `CallToolResult` rather than step4's
joined-text convenience string: Drive's tools (Step 8) publish schema-
validated `structuredContent` that graph nodes need to read directly,
while Gmail's tools only ever return text content -- one caller-visible
return shape that works for both, rather than pre-flattening away data
graph nodes need.
"""

import asyncio
import logging
import os
import sys
import webbrowser
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import anyio
import httpx
import mcp.types as types
from dotenv import load_dotenv
from mcp import ClientSession
from mcp.client.auth.oauth2 import OAuthClientProvider, TokenStorage
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamablehttp_client
from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata, OAuthToken
from mcp.shared.context import RequestContext
from openai import OpenAI
from pydantic import AnyUrl

STEP_DIR = Path(__file__).parent
REPO_ROOT = STEP_DIR.parent

DRIVE_SERVER_URL = "http://127.0.0.1:8780/mcp"
# Distinct from step8-production/client.py's 8771 -- both clients can be
# run independently without a port collision if someone still has Step 8's
# own demo client open in another terminal.
DRIVE_CALLBACK_HOST = "127.0.0.1"
DRIVE_CALLBACK_PORT = 8772
CEREBRAS_MODEL = "gpt-oss-120b"

GMAIL_PARAMS = StdioServerParameters(command="npx", args=["-y", "@artymclabin/gmail-mcp"])

_LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=_LOG_LEVEL,
    stream=sys.stderr,
    format="%(asctime)s [agents] %(levelname)s %(message)s",
)
logger = logging.getLogger("inbox-guardian.agents.mcp_client")

load_dotenv(REPO_ROOT / ".env")

Registry = dict[str, tuple[ClientSession, str]]


# --- OAuth plumbing for Drive -- copied forward from step8-production/client.py ---


class InMemoryTokenStorage(TokenStorage):
    """Tokens live only for this process's run, same as Step 8 -- there's no
    persisted credential file for the OAuth-protected Drive session the way
    Gmail's stdio server has one on disk."""

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
    """One-shot local HTTP listener for the browser's redirect back from
    Drive's /consent -- identical shape to step8-production/client.py's."""

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
    print(f"\nOpening browser for Drive consent: {authorization_url}\n")
    webbrowser.open(authorization_url)


def _make_drive_oauth_auth() -> OAuthClientProvider:
    callback_server = _OAuthCallbackServer(DRIVE_CALLBACK_HOST, DRIVE_CALLBACK_PORT)
    return OAuthClientProvider(
        server_url=DRIVE_SERVER_URL,
        client_metadata=OAuthClientMetadata(
            client_name="inbox-guardian-agents",
            redirect_uris=[f"http://{DRIVE_CALLBACK_HOST}:{DRIVE_CALLBACK_PORT}/callback"],
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            scope="drive:read",
        ),
        storage=InMemoryTokenStorage(),
        redirect_handler=_redirect_handler,
        callback_handler=callback_server.wait_for_callback,
    )


# --- Sampling + elicitation handlers -- copied forward from step8-production/client.py ---
# Unchanged in mechanism: this client still approves-then-calls-Cerebras for
# sampling, and still renders a schema-validated enum menu for elicitation.
# Drafter/Judge (graph.py) call Cerebras directly for their own LLM work --
# a separate client instance below, not this one -- since neither of them
# runs inside an MCP server and neither goes through sampling/createMessage.


def _make_cerebras_client() -> OpenAI:
    api_key = os.environ.get("CEREBRAS_API_KEY")
    if not api_key:
        raise RuntimeError("CEREBRAS_API_KEY not set -- check .env at the repo root")
    return OpenAI(api_key=api_key, base_url="https://api.cerebras.ai/v1")


_cerebras_client = _make_cerebras_client()

_FINISH_REASON_TO_STOP_REASON = {"stop": "endTurn", "length": "maxTokens"}

# Set by elicitation_handler at every return point, popped by Retriever
# (graph.py) right after a drive::search_drive_files call returns. This is
# how Retriever knows *for this specific call* whether elicitation actually
# fired and what the human chose -- search_drive_files' own response shape
# (Step 8's SearchDriveFilesResult) can't tell "matched after a human
# resolved a conflict" apart from "matched, never ambiguous" on its own
# (server.py folds the chosen file back into an ordinary status="matched"
# list either way). Safe as a plain module-level variable, not a lock-
# guarded one: this pipeline only ever has one search_drive_files call in
# flight at a time.
_last_elicitation: dict[str, Any] | None = None


def pop_last_elicitation() -> dict[str, Any] | None:
    """Consume and clear the most recent elicitation outcome recorded by
    `elicitation_handler` -- returns None if no elicitation fired since the
    last call to this function."""
    global _last_elicitation
    outcome = _last_elicitation
    _last_elicitation = None
    return outcome


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
    """Fulfill one sampling/createMessage request from Drive's server --
    same approval-gate mechanism as step8-production/client.py."""
    print("\n=== Incoming sampling/createMessage request (drive server -> client) ===")
    if params.systemPrompt:
        print(f"  system: {params.systemPrompt}")
    for message in params.messages:
        print(f"  [{message.role}] {_sampling_message_to_text(message)}")
    print(f"  max_tokens: {params.maxTokens}")

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

    return types.CreateMessageResult(
        role="assistant",
        content=types.TextContent(type="text", text=response_text),
        model=completion.model,
        stopReason=_FINISH_REASON_TO_STOP_REASON.get(choice.finish_reason, choice.finish_reason),
    )


async def elicitation_handler(
    context: RequestContext["ClientSession", Any],
    params: types.ElicitRequestParams,
) -> types.ElicitResult | types.ErrorData:
    """Fulfill one elicitation/create request from Drive's server -- same
    schema-rendering menu as step8-production/client.py's handler, plus
    recording the outcome into `_last_elicitation` (see above) so Retriever
    can surface what actually happened into graph state."""
    global _last_elicitation
    print("\n=== Incoming elicitation/create request (drive server -> client) ===")
    print(f"  message: {params.message}")

    if params.mode != "form":
        print(f"  Unsupported elicitation mode {params.mode!r} -- this client only handles form mode.\n")
        _last_elicitation = {"action": "error", "detail": f"unsupported mode {params.mode!r}"}
        return types.ErrorData(code=types.INVALID_REQUEST, message=f"Unsupported elicitation mode: {params.mode}")

    schema = params.requestedSchema
    props = schema.get("properties", {})
    field_name = next(iter(props), None)
    enum_values = props.get(field_name, {}).get("enum") if field_name else None

    if not field_name or not enum_values:
        print(f"  Don't know how to render this schema: {schema}\n")
        _last_elicitation = {"action": "error", "detail": "client cannot render this requestedSchema"}
        return types.ErrorData(code=types.INVALID_REQUEST, message="Client cannot render this requestedSchema")

    print(f"  requestedSchema: {schema}")
    print("\n  Options:")
    for i, value in enumerate(enum_values, start=1):
        print(f"    {i}. {value}")

    def _prompt() -> str:
        return input(f"\n  Enter a number (1-{len(enum_values)}), 'decline', or Ctrl-D/empty to cancel: ").strip()

    while True:
        try:
            raw = await anyio.to_thread.run_sync(_prompt)
        except EOFError:
            raw = ""

        if not raw:
            print("  Cancelled -- no choice made.\n")
            _last_elicitation = {"action": "cancel", "message": params.message}
            return types.ElicitResult(action="cancel")

        if raw.lower() == "decline":
            print("  Declined -- server told neither candidate is right, will not resolve the ambiguity.\n")
            _last_elicitation = {"action": "decline", "message": params.message}
            return types.ElicitResult(action="decline")

        if raw.isdigit() and 1 <= int(raw) <= len(enum_values):
            chosen = enum_values[int(raw) - 1]
            print(f"  Chosen: {chosen!r} -- validated against the schema's enum before returning.\n")
            _last_elicitation = {"action": "accept", "chosen_file_id": chosen, "message": params.message}
            return types.ElicitResult(action="accept", content={field_name: chosen})

        print(f"  Not a valid option (must be 1-{len(enum_values)}, 'decline', or empty) -- try again.")


# --- Connections: gmail (stdio) + drive (http/oauth/sampling/elicitation) ---


@asynccontextmanager
async def connect_gmail():
    """Spawn the scope-narrowed Gmail server as a subprocess and yield an
    initialized session -- same shape as step4-composition/client.py's
    `connect()`, minus the wire-logging pump (this step's interesting
    surface is the graph/Judge logic, not new transport plumbing)."""
    logger.info("[gmail] spawning subprocess: %s %s", GMAIL_PARAMS.command, " ".join(GMAIL_PARAMS.args))
    async with stdio_client(GMAIL_PARAMS) as (read, write):
        async with ClientSession(
            read,
            write,
            client_info=types.Implementation(name="inbox-guardian-agents[gmail]", version="0.1.0"),
        ) as session:
            await session.initialize()
            logger.info("[gmail] session initialized")
            yield session
    logger.info("[gmail] subprocess torn down")


@asynccontextmanager
async def connect_drive():
    """Connect to the already-running Drive server (step8-production/server.py)
    over Streamable HTTP + OAuth 2.1, with sampling + elicitation callbacks
    wired -- required for `drive::search_drive_files` to work at all (it
    checks the client's declared capabilities before calling either)."""
    auth = _make_drive_oauth_auth()
    async with streamablehttp_client(DRIVE_SERVER_URL, auth=auth) as (read, write, _get_session_id):
        async with ClientSession(
            read,
            write,
            sampling_callback=sampling_handler,
            elicitation_callback=elicitation_handler,
        ) as session:
            init_result = await session.initialize()
            logger.info(
                "[drive] session initialized, serverInfo=%r version=%r",
                init_result.serverInfo.name,
                init_result.serverInfo.version,
            )
            yield session
    logger.info("[drive] session closed")


async def build_registry(sessions: dict[str, ClientSession]) -> Registry:
    """`"{server}::{tool_name}" -> (session, real_name)` -- identical logic
    to step4-composition/client.py's `build_registry`."""
    registry: Registry = {}
    bare_name_owners: dict[str, list[str]] = {}

    for server_tag, session in sessions.items():
        result = await session.list_tools()
        for tool in result.tools:
            namespaced = f"{server_tag}::{tool.name}"
            registry[namespaced] = (session, tool.name)
            bare_name_owners.setdefault(tool.name, []).append(server_tag)

    for bare_name, owners in bare_name_owners.items():
        if len(owners) > 1:
            logger.warning(
                "tool name %r is exposed by multiple servers (%s) -- only reachable here via its "
                "'server::tool' prefixed key",
                bare_name,
                ", ".join(owners),
            )

    return registry


async def dispatch(registry: Registry, namespaced_name: str, arguments: dict) -> types.CallToolResult:
    """Call a tool by its namespaced key, returning the raw `CallToolResult`
    (not pre-flattened to text, unlike step4's `dispatch`) so a caller can
    read `structuredContent` when the tool publishes one (Drive's do; Gmail's
    don't) or fall back to `content` text either way.

    `KeyError` on an unknown namespaced name and whatever `call_tool`/session
    errors occur propagate uncaught -- same "don't hide a dead server behind
    an apparently-fine run" discipline step4-composition/client.py
    established."""
    session, real_name = registry[namespaced_name]
    return await session.call_tool(real_name, arguments)


def result_text(result: types.CallToolResult) -> str:
    """Join a CallToolResult's text content -- the shape Gmail's tools
    always return (no structuredContent)."""
    return "\n".join(c.text for c in result.content if isinstance(c, types.TextContent))


def get_session(registry: Registry, server_tag: str) -> ClientSession:
    """Find a live session for `server_tag` (e.g. "drive") from the
    registry -- needed for calls that aren't tools/call, like
    resources/read, which the "{server}::{tool}" registry doesn't index."""
    for namespaced_name, (session, _real_name) in registry.items():
        if namespaced_name.startswith(f"{server_tag}::"):
            return session
    raise KeyError(f"no session found for server_tag={server_tag!r}")


async def read_drive_resource(registry: Registry, file_id: str) -> str:
    """Fetch a Drive file's actual text content via resources/read on its
    `gdrive:///{file_id}` URI. `drive::search_drive_files` deliberately
    returns file_id/name/mimeType only -- "Fetch content via resources/read
    on the returned gdrive:///{file_id} URI" per its own tool description
    (server.py) -- and `drive::download_drive_file` also returns metadata
    only (file_id/name/mimeType/size_bytes, no content; it exists to
    demonstrate chunked-download progress/cancellation, not to hand back
    text). This is the one real path to actual grounding content."""
    session = get_session(registry, "drive")
    result = await session.read_resource(AnyUrl(f"gdrive:///{file_id}"))
    texts = [c.text for c in result.contents if isinstance(c, types.TextResourceContents)]
    if not texts:
        raise RuntimeError(f"gdrive:///{file_id} returned no text content (binary file?)")
    return "\n".join(texts)
