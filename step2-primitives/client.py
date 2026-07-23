"""Raw MCP client — Inbox Guardian Step 2.

Same hand-rolled JSON-RPC-over-stdio approach as Step 1 (no
mcp.client.session.ClientSession), extended to exercise all three
primitives in one lifecycle:

    initialize (request)  -->
                           <-- initialize (response, server capabilities)
    notifications/initialized (notification, no response) -->
    resources/list (request)  -->
                           <-- resources/list (response, ticket resources)
    resources/read (request, ticket://T-1001)  -->
                           <-- resources/read (response, full ticket text)
    prompts/list (request)  -->
                           <-- prompts/list (response, draft_reply schema)
    prompts/get (request, draft_reply, ticket_id=T-1001)  -->
                           <-- prompts/get (response, composed prompt message)
    tools/list (request)  -->
                           <-- tools/list (response, tool schema)
    tools/call (request, get_ticket_summary)  -->
                           <-- tools/call (response, result)
"""

import asyncio
import itertools
import json
import logging
import sys
from pathlib import Path

import mcp.types as types

STEP_DIR = Path(__file__).parent
LOG_FILE = STEP_DIR / "jsonrpc.log"

# stderr is fine for the client to use freely -- it isn't part of the
# server's stdio transport, only the client's own subprocess pipes are.
logging.basicConfig(
    level=logging.DEBUG,
    stream=sys.stderr,
    format="%(asctime)s [client] %(levelname)s %(message)s",
)
logger = logging.getLogger("inbox-guardian.step2.client")

_rpc_log = LOG_FILE.open("w", encoding="utf-8")


def _log_wire(direction: str, raw: str) -> None:
    """Record the exact bytes that crossed the wire, in both directions."""
    line = f"{direction} {raw}"
    logger.debug(line)
    _rpc_log.write(line + "\n")
    _rpc_log.flush()


class RawMCPClient:
    """Minimal hand-rolled JSON-RPC-over-stdio client for one server subprocess."""

    def __init__(self, process: asyncio.subprocess.Process) -> None:
        self._process = process
        self._id_counter = itertools.count(1)

    async def _send(self, message: types.JSONRPCRequest | types.JSONRPCNotification) -> None:
        raw = message.model_dump_json(by_alias=True, exclude_none=True)
        _log_wire(">>>", raw)
        assert self._process.stdin is not None
        self._process.stdin.write((raw + "\n").encode("utf-8"))
        await self._process.stdin.drain()

    async def _recv(self) -> types.JSONRPCResponse | types.JSONRPCError:
        assert self._process.stdout is not None
        line = await self._process.stdout.readline()
        if not line:
            raise ConnectionError("server closed stdout (process exited?)")
        raw = line.decode("utf-8").rstrip("\n")
        _log_wire("<<<", raw)
        message = types.JSONRPCMessage.model_validate_json(raw).root
        if not isinstance(message, (types.JSONRPCResponse, types.JSONRPCError)):
            raise TypeError(f"expected a response, got {type(message).__name__}")
        return message

    async def request(self, method: str, params: dict | None = None) -> dict:
        request_id = next(self._id_counter)
        message = types.JSONRPCRequest(jsonrpc="2.0", id=request_id, method=method, params=params)
        await self._send(message)
        response = await self._recv()
        if isinstance(response, types.JSONRPCError):
            raise RuntimeError(f"server returned JSON-RPC error {response.error.code}: {response.error.message}")
        if response.id != request_id:
            raise RuntimeError(f"response id {response.id!r} did not match request id {request_id!r}")
        return response.result

    async def notify(self, method: str, params: dict | None = None) -> None:
        message = types.JSONRPCNotification(jsonrpc="2.0", method=method, params=params)
        await self._send(message)


async def run_lifecycle() -> None:
    logger.info("spawning server subprocess")
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        str(STEP_DIR / "server.py"),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=None,  # let the server's own stderr logging show in our terminal
    )
    client = RawMCPClient(process)

    try:
        # 1. initialize -- client goes first, declares what it supports;
        # server responds with its own capabilities and identity. This time
        # the response should include `resources` and `prompts` capability
        # objects alongside `tools`.
        init_result = await client.request(
            "initialize",
            {
                "protocolVersion": types.LATEST_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "inbox-guardian-step2-client", "version": "0.1.0"},
            },
        )
        print("=== initialize result (server capabilities) ===")
        print(json.dumps(init_result, indent=2))

        # 2. initialized -- a notification: no id, no response expected.
        await client.notify("notifications/initialized")

        # 3. resources/list -- addressable data the server offers, by URI.
        resources_result = await client.request("resources/list", {})
        print("\n=== resources/list result ===")
        print(json.dumps(resources_result, indent=2))

        # 4. resources/read -- read one ticket resource by URI (not a
        # parameterized function call, just "give me this addressable thing").
        read_result = await client.request("resources/read", {"uri": "ticket://T-1001"})
        print("\n=== resources/read result (uri=ticket://T-1001) ===")
        print(json.dumps(read_result, indent=2))

        # 5. prompts/list -- the reusable template(s) the server ships,
        # with their typed argument schema.
        prompts_result = await client.request("prompts/list", {})
        print("\n=== prompts/list result ===")
        print(json.dumps(prompts_result, indent=2))

        # 6. prompts/get -- ask the server to compose the draft_reply
        # template for a specific ticket. The returned message should embed
        # the actual ticket text (from step 4), proving the server composed
        # the Resource into the Prompt at request time, not a static stub.
        prompt_get_result = await client.request(
            "prompts/get",
            {"name": "draft_reply", "arguments": {"ticket_id": "T-1001"}},
        )
        print("\n=== prompts/get result (draft_reply, ticket_id=T-1001) ===")
        print(json.dumps(prompt_get_result, indent=2))
        prompt_text = prompt_get_result["messages"][0]["content"]["text"]
        ticket_text = read_result["contents"][0]["text"]
        assert ticket_text in prompt_text, "expected ticket text to be embedded in the composed prompt"

        # 7. tools/list -- Step 1's tool, unmodified, still present
        # alongside the new primitives.
        tools_result = await client.request("tools/list", {})
        print("\n=== tools/list result ===")
        print(json.dumps(tools_result, indent=2))

        # 8. tools/call -- Step 1's tool call, unmodified, run last to show
        # the log covers all three primitives in one lifecycle.
        call_result = await client.request(
            "tools/call",
            {"name": "get_ticket_summary", "arguments": {"ticket_id": "T-1001"}},
        )
        print("\n=== tools/call result (valid ticket_id=T-1001) ===")
        print(json.dumps(call_result, indent=2))

    finally:
        if process.stdin:
            process.stdin.close()
        await process.wait()
        logger.info("server subprocess exited with code %s", process.returncode)
        _rpc_log.close()


if __name__ == "__main__":
    asyncio.run(run_lifecycle())
