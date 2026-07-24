"""Multi-server composition client — Inbox Guardian Step 4.

Holds three independent MCP sessions open at once, in one process:

    gmail  -- @gongrzhe/server-gmail-autoauth-mcp, via npx (Step 3)
    drive  -- step3-real-servers/drive-server/server.py (Step 3)
    toy    -- step1-fundamentals/server.py (Step 1), reused here purely as
              a second, controllable server for the collision/disconnect
              tests below -- not rebuilt, not extended with new endpoints.

MCP has no cross-server namespacing: two servers can each expose a tool
called the same thing, and nothing in the protocol stops a client from
mixing them up. That's this client's job, not the spec's. Every tool is
addressed here as "{server}::{tool_name}" through a single registry dict
(see build_registry / dispatch below) -- never by bare name. Both
step1's server.py and the Drive server.py were given a real, genuinely
colliding "ping" tool (see their diffs) so this isn't namespacing code
that has never been exercised against an actual collision.

Uses the SDK's ClientSession + stdio_client (not the hand-rolled raw
client from Steps 1-2) since three concurrent hand-rolled lifecycles
would just be duplicated plumbing -- the interesting part of this step is
composition, not the wire protocol again. The exact JSON-RPC frames
crossing each session are still captured, by inserting the same
log-and-forward pump used in Step 3's drive-server.py on the client side
of all three sessions, tagged by server name, into one shared jsonrpc.log.
"""

import asyncio
import logging
import sys
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path

import anyio
import mcp.types as types
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.shared.message import SessionMessage

STEP_DIR = Path(__file__).parent
REPO_ROOT = STEP_DIR.parent
LOG_FILE = STEP_DIR / "jsonrpc.log"

logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,
    format="%(asctime)s [client] %(levelname)s %(message)s",
)
logger = logging.getLogger("inbox-guardian.step4.client")

_rpc_log = LOG_FILE.open("w", encoding="utf-8")


def _log_wire(server_tag: str, direction: str, raw: str) -> None:
    """Record one JSON-RPC frame, tagged by which server session it belongs
    to, into the single shared log file -- so the interleaving across
    servers is visible by reading one file, not three."""
    line = f"[{server_tag}] {direction} {raw}"
    logger.debug(line)
    _rpc_log.write(line + "\n")
    _rpc_log.flush()


async def _pump_incoming(
    server_tag: str,
    raw_read: anyio.streams.memory.MemoryObjectReceiveStream,
    logged_read_writer: anyio.streams.memory.MemoryObjectSendStream,
) -> None:
    """Forward raw_read -> logged_read_writer, logging each frame first.

    Same shape as Step 3's drive-server.py pump, just parameterized by tag
    and run on the client side instead of the server side.
    """
    try:
        async with logged_read_writer:
            async for item in raw_read:
                if isinstance(item, SessionMessage):
                    _log_wire(server_tag, "<<<", item.message.model_dump_json(by_alias=True, exclude_none=True))
                await logged_read_writer.send(item)
    except (anyio.ClosedResourceError, anyio.BrokenResourceError):
        await anyio.lowlevel.checkpoint()


async def _pump_outgoing(
    server_tag: str,
    logged_write_reader: anyio.streams.memory.MemoryObjectReceiveStream,
    raw_write: anyio.streams.memory.MemoryObjectSendStream,
) -> None:
    try:
        async with logged_write_reader, raw_write:
            async for session_message in logged_write_reader:
                _log_wire(server_tag, ">>>", session_message.message.model_dump_json(by_alias=True, exclude_none=True))
                await raw_write.send(session_message)
    except (anyio.ClosedResourceError, anyio.BrokenResourceError):
        await anyio.lowlevel.checkpoint()


@asynccontextmanager
async def connect(server_tag: str, params: StdioServerParameters):
    """Spawn one server subprocess, wrap its stdio in a logging pump tagged
    `server_tag`, and yield an initialized ClientSession for it.

    Each call to connect() owns one subprocess end-to-end: opening it,
    initializing the session, and (on exit) cancelling the pumps and
    letting stdio_client tear the subprocess down. Nothing here is shared
    across servers -- three calls to connect() are three fully independent
    subprocesses and sessions, which is what "genuinely concurrent, not
    sequential connect-use-disconnect" requires.
    """
    logger.info("[%s] spawning subprocess: %s %s", server_tag, params.command, " ".join(params.args))
    async with stdio_client(params) as (raw_read, raw_write):
        logged_read_writer, logged_read = anyio.create_memory_object_stream(0)
        logged_write, logged_write_reader = anyio.create_memory_object_stream(0)

        async with anyio.create_task_group() as tg:
            tg.start_soon(_pump_incoming, server_tag, raw_read, logged_read_writer)
            tg.start_soon(_pump_outgoing, server_tag, logged_write_reader, raw_write)

            async with ClientSession(
                logged_read,
                logged_write,
                client_info=types.Implementation(name=f"inbox-guardian-step4-client[{server_tag}]", version="0.1.0"),
            ) as session:
                await session.initialize()
                logger.info("[%s] session initialized", server_tag)
                yield session

            tg.cancel_scope.cancel()
    logger.info("[%s] subprocess torn down", server_tag)


# --- Registry: the actual deliverable of this step --------------------------

Registry = dict[str, tuple[ClientSession, str]]


async def build_registry(sessions: dict[str, ClientSession]) -> Registry:
    """Namespace every tool as "{server}::{tool_name}" -> (session, real_name).

    Never index by bare tool name. If two servers expose the same bare
    name, that's detected and logged below, but it does NOT cause an
    error or a dropped entry -- both stay reachable, correctly routed,
    under their own prefixed key. That's the whole fix: the collision is
    real, but the client's own keys are still unique.
    """
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
                "tool name %r is exposed by multiple servers (%s) -- "
                "a bare-name lookup would silently pick one; the client "
                "only ever calls through the %r-prefixed keys below",
                bare_name,
                ", ".join(owners),
                "server::tool",
            )

    return registry


async def dispatch(registry: Registry, namespaced_name: str, arguments: dict) -> str:
    """Call a tool by its namespaced key and return its text content joined.

    KeyError on an unknown namespaced name and whatever call_tool/session
    errors occur are allowed to propagate -- this deliberately does not
    catch-and-continue, per the pitfall about hiding a dead server's
    failure behind an apparently-fine run.
    """
    session, real_name = registry[namespaced_name]
    result = await session.call_tool(real_name, arguments)
    return "\n".join(c.text for c in result.content if isinstance(c, types.TextContent))


def print_registry(registry: Registry) -> None:
    print("=== Combined tool registry (namespaced across all connected servers) ===")
    for namespaced_name in sorted(registry):
        print(f"  {namespaced_name}")
    print()


# --- Demonstrations -----------------------------------------------------


async def demonstrate_collision(registry: Registry) -> None:
    """Prove the namespacing actually routes correctly, not just 'no crash'.

    Both drive-server and step1's toy server were given a genuinely
    identical tool name, 'ping'. If the client's registry had merged them
    under a bare name instead of "{server}::ping", one call would win and
    silently shadow the other. Calling both here and asserting the
    responses are distinct and each names its own server is the actual
    proof, not just that both keys exist.
    """
    print("=== Collision test: drive::ping vs toy::ping (identical tool name on two servers) ===")
    drive_pong = await dispatch(registry, "drive::ping", {})
    toy_pong = await dispatch(registry, "toy::ping", {})
    print(f"  drive::ping -> {drive_pong!r}")
    print(f"  toy::ping   -> {toy_pong!r}")

    assert drive_pong != toy_pong, "expected the two servers' ping responses to differ"
    assert "drive" in drive_pong.lower(), "drive::ping did not identify itself as the drive server"
    assert "toy" in toy_pong.lower(), "toy::ping did not identify itself as the toy server"
    print("  OK: each namespaced call reached the correct server, not the other one.\n")


async def demonstrate_combined_operation(registry: Registry) -> None:
    """One logical task that genuinely needs both real servers.

    Not the real Retriever/Judge pipeline (that's Step 5+, once LangGraph
    is back) -- just proof this client can hold and use both sessions
    coherently in a single script. The two queries below are independently
    real (the Gmail account really has messages matching "account"; Drive
    really has a file matching "Refund"); they aren't presented as
    semantically linked, since this account's test data doesn't happen to
    have a message and a doc about the same topic.
    """
    print("=== Combined operation: Gmail search + Drive search, one script run ===")

    gmail_query = "account"
    gmail_result = await dispatch(registry, "gmail::search_emails", {"query": gmail_query, "maxResults": 5})
    print(f"gmail::search_emails(query={gmail_query!r}) ->\n{gmail_result}\n")

    drive_query = "name contains 'Refund'"
    drive_result = await dispatch(registry, "drive::search_drive_files", {"query": drive_query})
    print(f"drive::search_drive_files(query={drive_query!r}) ->\n{drive_result}\n")

    assert gmail_result.strip(), "expected at least one real Gmail search result"
    assert drive_result.strip(), "expected at least one real Drive search result"


async def demonstrate_disconnect_is_not_hidden(registry: Registry, toy_stack: AsyncExitStack) -> None:
    """Close the toy server's subprocess mid-run, then prove a call against
    it surfaces loudly instead of being swallowed and silently ignored.

    This is what the "if Gmail's subprocess dies mid-run" pitfall is
    about: the fix isn't to wrap every dispatch() in try/except and move
    on -- it's to let the failure propagate to whoever's watching, which
    here means we deliberately catch it exactly once, print it, and
    re-raise the point that a real caller must not treat this as fine.
    """
    print("=== Disconnect handling: closing toy server mid-run ===")
    await toy_stack.aclose()
    logger.info("[toy] subprocess deliberately closed by the client")

    try:
        await dispatch(registry, "toy::ping", {})
    except Exception as exc:
        print(f"  toy::ping failed loudly after disconnect, as it must: {type(exc).__name__}: {exc}")
    else:
        raise AssertionError("expected calling a tool on a disconnected server to raise, not succeed")

    print("  Drive and Gmail sessions are untouched by toy's disconnect -- confirming below:")
    drive_pong = await dispatch(registry, "drive::ping", {})
    print(f"  drive::ping -> {drive_pong!r}\n")


# --- Server launch parameters ---------------------------------------------

GMAIL_PARAMS = StdioServerParameters(command="npx", args=["-y", "@gongrzhe/server-gmail-autoauth-mcp"])
DRIVE_PARAMS = StdioServerParameters(
    command=str(REPO_ROOT / ".venv" / "bin" / "python"),
    args=[str(REPO_ROOT / "step3-real-servers" / "drive-server" / "server.py")],
)
TOY_PARAMS = StdioServerParameters(
    command=str(REPO_ROOT / ".venv" / "bin" / "python"),
    args=[str(REPO_ROOT / "step1-fundamentals" / "server.py")],
)


async def main() -> None:
    async with AsyncExitStack() as stack:
        # All three subprocesses are launched and initialized here, and stay
        # alive together through everything below -- genuinely concurrent,
        # not connect-use-disconnect-reconnect per operation.
        gmail_session = await stack.enter_async_context(connect("gmail", GMAIL_PARAMS))
        drive_session = await stack.enter_async_context(connect("drive", DRIVE_PARAMS))

        # toy gets its own exit stack (not the shared one) so it can be torn
        # down independently, mid-run, for the disconnect demonstration below
        # -- without also tearing down gmail/drive.
        toy_stack = AsyncExitStack()
        toy_session = await toy_stack.enter_async_context(connect("toy", TOY_PARAMS))

        registry = await build_registry({"gmail": gmail_session, "drive": drive_session, "toy": toy_session})
        print_registry(registry)

        await demonstrate_collision(registry)
        await demonstrate_combined_operation(registry)
        await demonstrate_disconnect_is_not_hidden(registry, toy_stack)

    _rpc_log.close()
    logger.info("all sessions closed, jsonrpc.log written")


if __name__ == "__main__":
    asyncio.run(main())
