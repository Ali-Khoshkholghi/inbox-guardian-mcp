"""Toy MCP server — Inbox Guardian Step 1.

Exposes a single tool, get_ticket_summary, backed by an in-memory dict of
fake support tickets. The tool logic is intentionally trivial: this step
is about proving out the stdio/JSON-RPC transport, not tool content.

Uses the low-level mcp.server.lowlevel.Server directly (not a framework
wrapper like FastMCP) so the initialize -> tools/list -> tools/call
lifecycle is visible rather than hidden behind decorators-as-magic.
"""

import logging
import sys

import anyio
import mcp.types as types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

# stdio IS the transport here: stdout is reserved for JSON-RPC frames only.
# Any stray print() to stdout would corrupt the stream, so all diagnostic
# output goes to stderr via logging.
logging.basicConfig(
    level=logging.DEBUG,
    stream=sys.stderr,
    format="%(asctime)s [server] %(levelname)s %(message)s",
)
logger = logging.getLogger("inbox-guardian.step1.server")

TICKETS = {
    "T-1001": "Customer can't reset password; reset link sent, awaiting confirmation.",
    "T-1002": "Billing dispute over duplicate charge; refund issued, ticket closed.",
    "T-1003": "Feature request for CSV export; logged with product team, no ETA.",
}

server = Server("inbox-guardian-toy")


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
        # Raising a plain exception here is deliberate: the low-level
        # Server's call_tool wrapper catches it and returns a structured
        # CallToolResult(isError=True) to the client instead of letting it
        # propagate into a JSON-RPC protocol-level error or crashing the
        # process. That's the "proper MCP error response" the spec asks for.
        raise ValueError(f"Unknown ticket_id: {ticket_id!r}")

    return [types.TextContent(type="text", text=TICKETS[ticket_id])]


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
