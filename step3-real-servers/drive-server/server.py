"""Drive MCP server — Inbox Guardian Step 3.

Exposes a live Google Drive account as MCP primitives:

- a Resource per Drive file (gdrive:///{file_id}), listed via files().list()
  and read via files().get_media() (regular files) or files().export()
  (Google-native Docs/Sheets/Slides).
- one Tool, search_drive_files, for locating files by Drive search query
  (names/ids/mimeTypes only -- content only ever comes from resources/read).

Uses the low-level mcp.server.lowlevel.Server directly, same as Steps 1-2,
so the request handlers stay visible rather than hidden behind a framework.

Auth happens once, up front in main(), before the stdio transport is even
opened: this server reads an already-issued token from
~/.drive-mcp/token.json (written by auth_setup.py) and refuses to start if
it's missing. It never runs the interactive OAuth flow itself and never
lazy-authenticates on first tool call -- doing that turned a broken Gmail
server into a confusing one, since a failure showed up as a mysterious
first-call error instead of an immediate, actionable startup message.
"""

import json
import logging
import sys
from pathlib import Path

import anyio
import mcp.types as types
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from mcp.server.lowlevel import Server
from mcp.server.lowlevel.helper_types import ReadResourceContents
from mcp.server.stdio import stdio_server
from mcp.shared.message import SessionMessage
from pydantic import AnyUrl

# stdio IS the transport here: stdout is reserved for JSON-RPC frames only.
# Any stray print() to stdout would corrupt the stream, so all diagnostic
# output goes to stderr via logging.
logging.basicConfig(
    level=logging.DEBUG,
    stream=sys.stderr,
    format="%(asctime)s [server] %(levelname)s %(message)s",
)
logger = logging.getLogger("inbox-guardian.step3.drive-server")

STEP_DIR = Path(__file__).parent
LOG_FILE = STEP_DIR / "jsonrpc.log"
_rpc_log = LOG_FILE.open("w", encoding="utf-8")

TOKEN_PATH = Path.home() / ".drive-mcp" / "token.json"
# Read-only, matching auth_setup.py. If you ever need a broader scope, that's
# a deliberate change to make in both places, not something to drift into.
SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]

GOOGLE_APPS_PREFIX = "application/vnd.google-apps."
GOOGLE_NATIVE_EXPORT_MIME_TYPES = {
    "application/vnd.google-apps.document": "text/plain",
    "application/vnd.google-apps.spreadsheet": "text/csv",
    "application/vnd.google-apps.presentation": "text/plain",
}
DEFAULT_EXPORT_MIME_TYPE = "text/plain"


def _log_wire(direction: str, raw: str) -> None:
    """Record the exact JSON-RPC frame crossing stdio, in both directions.

    stdio_server() only hands the server parsed SessionMessage objects, not
    the literal bytes off the wire, so this re-serializes each one the same
    way stdio_server's own stdout_writer does -- content-equivalent to what
    actually crossed, same as Step 1's raw client logged verbatim text.
    """
    line = f"{direction} {raw}"
    logger.debug(line)
    _rpc_log.write(line + "\n")
    _rpc_log.flush()


async def _pump_incoming(
    raw_read: anyio.streams.memory.MemoryObjectReceiveStream,
    logged_read_writer: anyio.streams.memory.MemoryObjectSendStream,
) -> None:
    try:
        async with logged_read_writer:
            async for item in raw_read:
                if isinstance(item, SessionMessage):
                    _log_wire("<<<", item.message.model_dump_json(by_alias=True, exclude_none=True))
                await logged_read_writer.send(item)
    except (anyio.ClosedResourceError, anyio.BrokenResourceError):
        await anyio.lowlevel.checkpoint()


async def _pump_outgoing(
    logged_write_reader: anyio.streams.memory.MemoryObjectReceiveStream,
    raw_write: anyio.streams.memory.MemoryObjectSendStream,
) -> None:
    try:
        # Must also close raw_write when this ends, not just
        # logged_write_reader: stdio_server()'s own stdout_writer task keeps
        # waiting on raw_write's paired receiver until raw_write is closed,
        # so forgetting this half deadlocks shutdown even though the
        # session-facing side looks fully torn down.
        async with logged_write_reader, raw_write:
            async for session_message in logged_write_reader:
                _log_wire(">>>", session_message.message.model_dump_json(by_alias=True, exclude_none=True))
                await raw_write.send(session_message)
    except (anyio.ClosedResourceError, anyio.BrokenResourceError):
        await anyio.lowlevel.checkpoint()


def _load_credentials() -> Credentials:
    if not TOKEN_PATH.exists():
        logger.error(
            "Drive credentials not found at %s. Please run auth_setup.py once "
            "to authenticate before starting this server:\n\n"
            "    ../../.venv/bin/python auth_setup.py\n",
            TOKEN_PATH,
        )
        sys.exit(1)

    creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
    if creds.expired and creds.refresh_token:
        logger.info("refreshing expired Drive access token")
        creds.refresh(GoogleAuthRequest())
        TOKEN_PATH.write_text(creds.to_json())
    return creds


def _gdrive_uri(file_id: str) -> str:
    return f"gdrive:///{file_id}"


def _file_id_from_uri(uri: str) -> str:
    if not uri.startswith("gdrive:///"):
        raise ValueError(f"Unsupported resource URI scheme: {uri!r}")
    return uri.removeprefix("gdrive:///")


server = Server("inbox-guardian-drive")
drive_service = None  # set in main(), after auth succeeds


# --- Resources: one per Drive file, addressed by gdrive:///{file_id} -------


@server.list_resources()
async def handle_list_resources() -> list[types.Resource]:
    logger.debug("handling resources/list")
    response = (
        drive_service.files()
        .list(pageSize=20, fields="files(id, name, mimeType)")
        .execute()
    )
    return [
        types.Resource(
            uri=AnyUrl(_gdrive_uri(file["id"])),
            name=file["name"],
            mimeType=file.get("mimeType"),
        )
        for file in response.get("files", [])
    ]


@server.read_resource()
async def handle_read_resource(uri: AnyUrl) -> list[ReadResourceContents]:
    logger.debug("handling resources/read: uri=%s", uri)
    file_id = _file_id_from_uri(str(uri))

    try:
        metadata = drive_service.files().get(fileId=file_id, fields="name, mimeType").execute()
        mime_type = metadata["mimeType"]

        if mime_type.startswith(GOOGLE_APPS_PREFIX):
            export_mime_type = GOOGLE_NATIVE_EXPORT_MIME_TYPES.get(mime_type, DEFAULT_EXPORT_MIME_TYPE)
            raw = drive_service.files().export(fileId=file_id, mimeType=export_mime_type).execute()
            return [ReadResourceContents(content=raw.decode("utf-8"), mime_type=export_mime_type)]

        raw = drive_service.files().get_media(fileId=file_id).execute()
        try:
            return [ReadResourceContents(content=raw.decode("utf-8"), mime_type=mime_type)]
        except UnicodeDecodeError:
            # Binary regular file (image, pdf, etc.) -- ReadResourceContents
            # hands bytes straight through and the SDK base64-blobs it.
            return [ReadResourceContents(content=raw, mime_type=mime_type)]

    except HttpError as exc:
        # ReadResourceResult has no isError field the way CallToolResult
        # does, so there's no "soft" error slot in the result payload
        # itself. Raising here is the resources/read equivalent: the
        # low-level Server catches it and turns it into a proper JSON-RPC
        # error response instead of an unhandled exception crossing the
        # wire or crashing the process.
        raise ValueError(f"Failed to read Drive file {file_id!r}: {exc}") from exc


# --- Tool: search only -- names/ids/mimeTypes, never content ---------------


@server.list_tools()
async def handle_list_tools() -> list[types.Tool]:
    logger.debug("handling tools/list")
    return [
        types.Tool(
            name="search_drive_files",
            description=(
                "Search Drive using Drive's search query syntax "
                "(e.g. \"name contains 'invoice'\"). Returns matching "
                "file_id/name/mimeType only -- no content. Fetch content "
                "via resources/read on the returned gdrive:///{file_id} URI."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Drive search query, e.g. \"name contains 'report'\".",
                    }
                },
                "required": ["query"],
            },
        )
    ]


@server.call_tool()
async def handle_call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    logger.debug("handling tools/call: name=%s arguments=%s", name, arguments)

    if name != "search_drive_files":
        raise ValueError(f"Unknown tool: {name}")

    query = arguments["query"]
    response = (
        drive_service.files()
        .list(q=query, pageSize=20, fields="files(id, name, mimeType)")
        .execute()
    )
    matches = [
        {"file_id": file["id"], "name": file["name"], "mimeType": file.get("mimeType")}
        for file in response.get("files", [])
    ]
    return [types.TextContent(type="text", text=json.dumps(matches, indent=2))]


async def main() -> None:
    global drive_service

    creds = _load_credentials()
    drive_service = build("drive", "v3", credentials=creds)
    logger.info("authenticated with Drive; starting stdio server: inbox-guardian-drive")

    async with stdio_server() as (raw_read, raw_write):
        logged_read_writer, logged_read = anyio.create_memory_object_stream(0)
        logged_write, logged_write_reader = anyio.create_memory_object_stream(0)

        async with anyio.create_task_group() as tg:
            tg.start_soon(_pump_incoming, raw_read, logged_read_writer)
            tg.start_soon(_pump_outgoing, logged_write_reader, raw_write)
            await server.run(logged_read, logged_write, server.create_initialization_options())
            tg.cancel_scope.cancel()

    logger.info("server stopped")
    _rpc_log.close()


if __name__ == "__main__":
    anyio.run(main)
