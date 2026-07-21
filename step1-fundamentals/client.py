"""Raw MCP client — Inbox Guardian Step 1.

Talks to server.py over stdio using hand-built JSON-RPC 2.0 messages.
Deliberately does NOT use mcp.client.session.ClientSession, which bundles
initialize + the "initialized" notification (and tools/list convenience
helpers) behind a couple of method calls. The point of this step is to
see each message on the wire separately, so this client sends and
receives one frame at a time and logs every one of them, in order:

    initialize (request)  -->
                           <-- initialize (response, server capabilities)
    notifications/initialized (notification, no response) -->
    tools/list (request)  -->
                           <-- tools/list (response, tool schemas)
    tools/call (request)  -->
                           <-- tools/call (response, result or isError)
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
logger = logging.getLogger("inbox-guardian.step1.client")

_rpc_log = LOG_FILE.open("w", encoding="utf-8")


def _log_wire(direction: str, raw: str) -> None:
    """Record the exact bytes that crossed the wire, in both directions.

    This is the artifact the step 1 spec asks for: not the SDK's parsed
    objects, but the literal JSON-RPC line that went over stdin/stdout.
    """
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
        # server responds with its own capabilities and identity.
        init_result = await client.request(
            "initialize",
            {
                "protocolVersion": types.LATEST_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "inbox-guardian-step1-client", "version": "0.1.0"},
            },
        )
        print("=== initialize result (server capabilities) ===")
        print(json.dumps(init_result, indent=2))

        # 2. initialized -- a notification: no id, no response expected.
        # This is the client confirming it accepted the negotiated session.
        await client.notify("notifications/initialized")

        # 3. tools/list -- ask what the server can do
        tools_result = await client.request("tools/list", {})
        print("\n=== tools/list result ===")
        print(json.dumps(tools_result, indent=2))

        # 4a. tools/call -- valid ticket id
        call_result = await client.request(
            "tools/call",
            {"name": "get_ticket_summary", "arguments": {"ticket_id": "T-1001"}},
        )
        print("\n=== tools/call result (valid ticket_id=T-1001) ===")
        print(json.dumps(call_result, indent=2))

        # 4b. tools/call -- invalid ticket id. This must come back as an
        # ordinary JSON-RPC *response* whose result has isError=True --
        # not a JSON-RPC protocol error, and not a dropped connection.
        error_call_result = await client.request(
            "tools/call",
            {"name": "get_ticket_summary", "arguments": {"ticket_id": "T-9999"}},
        )
        print("\n=== tools/call result (invalid ticket_id=T-9999) ===")
        print(json.dumps(error_call_result, indent=2))
        assert error_call_result.get("isError") is True, "expected isError=True for unknown ticket"

    finally:
        if process.stdin:
            process.stdin.close()
        await process.wait()
        logger.info("server subprocess exited with code %s", process.returncode)
        _rpc_log.close()


if __name__ == "__main__":
    asyncio.run(run_lifecycle())
