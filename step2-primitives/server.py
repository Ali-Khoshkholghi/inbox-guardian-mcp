"""Toy MCP server — Inbox Guardian Step 2.

Extends Step 1's server with two new primitives, side by side with the
existing Tool:

- a Resource per ticket (ticket://T-1001, etc.) — addressable data the
  client reads by URI, not a parameterized function call.
- a Prompt (draft_reply) — a server-owned template that composes a
  Resource into a prompt message at request time, not a static string.

Uses the low-level mcp.server.lowlevel.Server directly, same as Step 1, so
the list/read/get request handlers stay visible rather than hidden behind
decorator magic.
"""

import logging
import sys

import anyio
import mcp.types as types
from mcp.server.lowlevel import Server
from mcp.server.lowlevel.helper_types import ReadResourceContents
from mcp.server.stdio import stdio_server
from pydantic import AnyUrl

# stdio IS the transport here: stdout is reserved for JSON-RPC frames only.
# Any stray print() to stdout would corrupt the stream, so all diagnostic
# output goes to stderr via logging.
logging.basicConfig(
    level=logging.DEBUG,
    stream=sys.stderr,
    format="%(asctime)s [server] %(levelname)s %(message)s",
)
logger = logging.getLogger("inbox-guardian.step2.server")

TICKETS = {
    "T-1001": "Customer can't reset password; reset link sent, awaiting confirmation.",
    "T-1002": "Billing dispute over duplicate charge; refund issued, ticket closed.",
    "T-1003": "Feature request for CSV export; logged with product team, no ETA.",
}


def _ticket_uri(ticket_id: str) -> str:
    return f"ticket://{ticket_id}"


def _read_ticket_resource(ticket_id: str) -> str:
    """Single source of truth for ticket content.

    Both handle_read_resource (resources/read) and handle_get_prompt
    (prompts/get) must call this rather than indexing TICKETS directly.
    The Prompt is only a genuine composition of the Resource — not a
    second copy of the same lookup — if both primitives run through this
    one function. If you're adding a new call site that reads ticket
    content, it should call this, not TICKETS[...] directly.
    """
    if ticket_id not in TICKETS:
        raise ValueError(f"Unknown ticket_id: {ticket_id!r}")
    return TICKETS[ticket_id]


server = Server("inbox-guardian-toy")


# --- Tool (unchanged from Step 1) ------------------------------------------


@server.list_tools()
async def handle_list_tools() -> list[types.Tool]:
    logger.debug("handling tools/list")
    return [
        types.Tool(
            name="get_ticket_summary",
            description="Look up a one-line summary for a support ticket by ID.",
            inputSchema={
                "type": "object",
                "properties": {
                    "ticket_id": {
                        "type": "string",
                        "description": "Ticket identifier, e.g. 'T-1001'.",
                    }
                },
                "required": ["ticket_id"],
            },
        )
    ]


@server.call_tool()
async def handle_call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    logger.debug("handling tools/call: name=%s arguments=%s", name, arguments)

    if name != "get_ticket_summary":
        raise ValueError(f"Unknown tool: {name}")

    ticket_id = arguments["ticket_id"]
    if ticket_id not in TICKETS:
        raise ValueError(f"Unknown ticket_id: {ticket_id!r}")

    return [types.TextContent(type="text", text=TICKETS[ticket_id])]


# --- Resource: one per ticket, addressed by URI, not by function args -------


@server.list_resources()
async def handle_list_resources() -> list[types.Resource]:
    logger.debug("handling resources/list")
    return [
        types.Resource(
            uri=AnyUrl(_ticket_uri(ticket_id)),
            name=ticket_id,
            description=f"Full ticket text for {ticket_id}",
            mimeType="text/plain",
        )
        for ticket_id in TICKETS
    ]


@server.read_resource()
async def handle_read_resource(uri: AnyUrl) -> list[ReadResourceContents]:
    logger.debug("handling resources/read: uri=%s", uri)

    uri_str = str(uri)
    if not uri_str.startswith("ticket://"):
        raise ValueError(f"Unsupported resource URI scheme: {uri_str!r}")

    ticket_id = uri_str.removeprefix("ticket://")
    return [ReadResourceContents(content=_read_ticket_resource(ticket_id), mime_type="text/plain")]


# --- Prompt: composes a Resource into a message template at request time ---


@server.list_prompts()
async def handle_list_prompts() -> list[types.Prompt]:
    logger.debug("handling prompts/list")
    return [
        types.Prompt(
            name="draft_reply",
            description="Draft a support reply grounded in a single ticket's context.",
            arguments=[
                types.PromptArgument(
                    name="ticket_id",
                    description="Ticket identifier, e.g. 'T-1001'.",
                    required=True,
                )
            ],
        )
    ]


@server.get_prompt()
async def handle_get_prompt(name: str, arguments: dict[str, str] | None) -> types.GetPromptResult:
    logger.debug("handling prompts/get: name=%s arguments=%s", name, arguments)

    if name != "draft_reply":
        raise ValueError(f"Unknown prompt: {name}")

    ticket_id = (arguments or {}).get("ticket_id")

    # The point of this handler: it calls the same lookup the Resource
    # primitive uses, at request time, and embeds the actual ticket text
    # into the prompt message — it does not hand back a static string
    # referencing the ticket, and it does not keep its own copy of the
    # lookup logic.
    ticket_text = _read_ticket_resource(ticket_id)

    return types.GetPromptResult(
        description=f"Draft a reply for {ticket_id}, grounded only in its ticket context.",
        messages=[
            types.PromptMessage(
                role="user",
                content=types.TextContent(
                    type="text",
                    text=(
                        "Draft a reply using only the following ticket context. "
                        "Do not invent facts not present here.\n\n"
                        f"Ticket {ticket_id}:\n{ticket_text}"
                    ),
                ),
            )
        ],
    )


@server.completion()
async def handle_completion(
    ref: types.PromptReference | types.ResourceTemplateReference,
    argument: types.CompletionArgument,
    context: types.CompletionContext | None,
) -> types.Completion | None:
    logger.debug("handling completion/complete: ref=%s argument=%s", ref, argument)

    if isinstance(ref, types.PromptReference) and ref.name == "draft_reply" and argument.name == "ticket_id":
        return types.Completion(values=list(TICKETS), total=len(TICKETS), hasMore=False)

    return None


async def main() -> None:
    logger.info("starting stdio server: inbox-guardian-toy")
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )
    logger.info("server stopped")


if __name__ == "__main__":
    anyio.run(main)
