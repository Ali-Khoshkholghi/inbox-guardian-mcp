"""Drive MCP server — Inbox Guardian Step 5, Streamable HTTP + OAuth 2.1.

A new variant of step3-real-servers/drive-server/server.py, not a
replacement -- that copy stays stdio and stays verified as-is. This one
swaps the transport to Streamable HTTP (a single /mcp endpoint, POST for
requests, GET for the SSE stream back) and puts real MCP-level OAuth 2.1
in front of it. Same Drive resources/tools underneath; this file is
about transport and auth, not new Drive functionality.

Why HTTP needed OAuth 2.1 and stdio never did (Step 3's deferred
question, now answered with an implementation): stdio has no network
trust boundary -- the "client" is a subprocess *you* spawned, over a
pipe only your own process can reach. There is no one else on the other
end to authenticate. Streamable HTTP puts this server on a socket
anything on the network can open a connection to; "is this caller
allowed to act as this resource owner" becomes a real question the
transport itself can't answer, which is exactly what OAuth 2.1 (RFC 9728
protected-resource discovery, PKCE-based auth code flow, bearer tokens
validated per-request) is for.

Three layers, built and verified in order (see README's "what to check
first" for why this order matters -- transport bugs and auth bugs look
identical from the client's side if you don't isolate them):

  1. Transport: StreamableHTTPSessionManager wraps the same low-level
     `Server` and the same Drive resource/tool handlers Step 3 used --
     nothing about handling resources/read or tools/call changed, only
     how bytes reach this process. Runnable with --no-auth to prove the
     transport swap alone doesn't break anything, before layering auth
     on top of it.
  2. Progress + cancellation: `download_drive_file` chunks a real Drive
     download via MediaIoBaseDownload, reporting real byte progress per
     chunk and stopping mid-download (not completing silently) if the
     client cancels.
  3. OAuth 2.1: oauth/provider.py + oauth/consent.py implement the
     authorization-server side; this file wires RFC 9728 protected-
     resource metadata, the SDK's own auth routes, and
     RequireAuthMiddleware around the /mcp endpoint so every request --
     including initialize -- needs a valid bearer token, no exceptions.

Uses the low-level mcp.server.lowlevel.Server directly for the MCP
protocol handlers, same as Steps 1-3, and assembles the Starlette/ASGI
app by hand (rather than mcp.server.fastmcp.FastMCP's higher-level
wrapper) using the same SDK building blocks FastMCP itself uses
internally (StreamableHTTPSessionManager, create_auth_routes,
create_protected_resource_routes, RequireAuthMiddleware) -- so that
wiring stays visible here rather than hidden behind a framework.
"""

import argparse
import io
import json
import logging
import secrets
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path

import anyio
import mcp.types as types
import uvicorn
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseDownload
from mcp.server.auth.middleware.auth_context import AuthContextMiddleware
from mcp.server.auth.middleware.bearer_auth import BearerAuthBackend, RequireAuthMiddleware
from mcp.server.auth.provider import ProviderTokenVerifier
from mcp.server.auth.routes import (
    build_resource_metadata_url,
    create_auth_routes,
    create_protected_resource_routes,
)
from mcp.server.auth.settings import ClientRegistrationOptions
from mcp.server.lowlevel import Server
from mcp.server.lowlevel.helper_types import ReadResourceContents
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from pydantic import AnyHttpUrl, AnyUrl
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.authentication import AuthenticationMiddleware
from starlette.routing import Route
from starlette.types import Receive, Scope, Send

from oauth.consent import create_consent_routes
from oauth.provider import DriveOAuthProvider

logging.basicConfig(
    level=logging.DEBUG,
    stream=sys.stderr,
    format="%(asctime)s [server] %(levelname)s %(message)s",
)
logger = logging.getLogger("inbox-guardian.step5.drive-server")

STEP_DIR = Path(__file__).parent
LOG_FILE = STEP_DIR / "jsonrpc.log"
_rpc_log = LOG_FILE.open("w", encoding="utf-8")

TOKEN_PATH = Path.home() / ".drive-mcp" / "token.json"
SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]

GOOGLE_APPS_PREFIX = "application/vnd.google-apps."
GOOGLE_NATIVE_EXPORT_MIME_TYPES = {
    "application/vnd.google-apps.document": "text/plain",
    "application/vnd.google-apps.spreadsheet": "text/csv",
    "application/vnd.google-apps.presentation": "text/plain",
}
DEFAULT_EXPORT_MIME_TYPE = "text/plain"

MCP_PATH = "/mcp"
REQUIRED_SCOPE = "drive:read"
DOWNLOAD_CHUNK_SIZE = 131072  # 128 KiB -- small on purpose, so a ~2.5MB file yields ~20 progress steps
DOWNLOAD_CHUNK_PACING_SECONDS = 0.35  # artificial; see README's "what's synthetic here" note


def _log_wire(direction: str, raw: str) -> None:
    """Record one line crossing the /mcp HTTP boundary, either direction.

    Unlike Steps 1-3's stdio pump (which re-serialized parsed
    SessionMessage objects), this logs the literal bytes Starlette
    reads off/writes to the socket -- POST request bodies (JSON-RPC
    requests) and each SSE `data: ...` write (JSON-RPC responses/
    notifications) -- via ASGI-level receive/send wrapping in
    _WireLoggingMiddleware below.
    """
    line = f"{direction} {raw}"
    logger.debug(line)
    _rpc_log.write(line + "\n")
    _rpc_log.flush()


class _WireLoggingMiddleware:
    """ASGI middleware logging every HTTP request/response body byte range
    that crosses this server's boundary into one jsonrpc.log -- MCP
    JSON-RPC traffic on /mcp, and the OAuth handshake requests
    (/authorize, /token, /register, /consent, the metadata documents)
    on every other path, all in one place so the two can be read
    together during a debugging session instead of guessing which log
    a given request landed in.
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        method = scope["method"]
        path = scope["path"]

        async def logged_receive():
            message = await receive()
            if message["type"] == "http.request":
                body = message.get("body", b"")
                if body:
                    _log_wire(">>>", f"{method} {path} {body.decode('utf-8', 'replace')}")
            return message

        async def logged_send(message):
            if message["type"] == "http.response.body":
                body = message.get("body", b"")
                if body:
                    for line in body.decode("utf-8", "replace").splitlines():
                        if line.strip():
                            _log_wire("<<<", f"{path} {line}")
            await send(message)

        await self.app(scope, logged_receive, logged_send)


def _load_credentials() -> Credentials:
    if not TOKEN_PATH.exists():
        logger.error(
            "Drive credentials not found at %s. Run step3-real-servers/drive-server/auth_setup.py "
            "once to authenticate before starting this server.",
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


server = Server("inbox-guardian-drive-http")
drive_service = None  # set in main(), after auth succeeds


# --- Resources: unchanged from Step 3 -- transport doesn't touch these -----


@server.list_resources()
async def handle_list_resources() -> list[types.Resource]:
    logger.debug("handling resources/list")
    response = drive_service.files().list(pageSize=20, fields="files(id, name, mimeType)").execute()
    return [
        types.Resource(
            uri=AnyUrl(_gdrive_uri(file["id"])),
            name=file["name"],
            mimeType=file.get("mimeType"),
        )
        for file in response.get("files", [])
    ]


async def _fetch_drive_file_content(
    file_id: str,
    *,
    report_progress: Callable[[float, float | None], Awaitable[None]] | None = None,
) -> tuple[bytes | str, str, str]:
    """Fetch a Drive file's full content, branching on mimeType -- the one
    implementation both resources/read and the download_drive_file tool
    call into, so the two can't silently drift the way independent copies
    of the same lookup did in Step 2 (see test_shared_download.py, which
    proves this by spying on this function, not just comparing output).

    Google-native files (Docs/Sheets/Slides) only support `export()`, not
    `get_media()` -- calling get_media on one raises exactly the 403 this
    fix addresses ("Only files with binary content can be downloaded. Use
    Export with Docs Editors files."). Drive's export API has no chunked/
    paginated equivalent to MediaIoBaseDownload, so that branch is one
    blocking call (still real work, still off the event loop, still
    cancellable) with no intermediate progress to report -- `report_progress`
    is only ever invoked from the regular-file branch below.

    Returns (content, mime_type, name). `content` is `str` for a
    Google-native export (always text) or a UTF-8-decodable regular file,
    `bytes` for a binary regular file.
    """
    metadata = await anyio.to_thread.run_sync(
        lambda: drive_service.files().get(fileId=file_id, fields="name, mimeType").execute()
    )
    mime_type = metadata["mimeType"]
    name = metadata["name"]

    if mime_type.startswith(GOOGLE_APPS_PREFIX):
        export_mime_type = GOOGLE_NATIVE_EXPORT_MIME_TYPES.get(mime_type, DEFAULT_EXPORT_MIME_TYPE)
        raw = await anyio.to_thread.run_sync(
            lambda: drive_service.files().export(fileId=file_id, mimeType=export_mime_type).execute(),
            abandon_on_cancel=True,
        )
        return raw.decode("utf-8"), export_mime_type, name

    buffer = io.BytesIO()
    request = drive_service.files().get_media(fileId=file_id)
    downloader = MediaIoBaseDownload(buffer, request, chunksize=DOWNLOAD_CHUNK_SIZE)

    done = False
    chunk_index = 0
    while not done:
        chunk_index += 1
        logger.info("_fetch_drive_file_content: requesting chunk %d of %r", chunk_index, name)

        # abandon_on_cancel=True: if notifications/cancelled arrives while
        # this chunk's network call is in flight, the await raises
        # immediately instead of waiting for the (unkillable) worker
        # thread to finish -- we stop issuing further chunk requests and
        # never build/return a result, which is the real proof cancellation
        # actually stopped work rather than just the caller giving up on
        # waiting for a response that was coming anyway.
        status, done = await anyio.to_thread.run_sync(downloader.next_chunk, abandon_on_cancel=True)

        logger.info(
            "_fetch_drive_file_content: chunk %d received (%d / %s bytes, %.0f%%)",
            chunk_index,
            status.resumable_progress,
            status.total_size,
            status.progress() * 100,
        )

        if report_progress is not None:
            await report_progress(status.resumable_progress, status.total_size)
            # Artificial pacing -- see README's "what's synthetic here" note.
            # Only applied when a caller is actually listening for progress
            # (i.e. the download_drive_file tool, not resources/read): a
            # 128KiB chunk of this file arrives fast enough on a real
            # connection that there's no reliable window to send
            # notifications/cancelled mid-flight without it. The Drive API
            # call itself (MediaIoBaseDownload.next_chunk) is entirely real.
            await anyio.sleep(DOWNLOAD_CHUNK_PACING_SECONDS)

    raw = buffer.getvalue()
    try:
        return raw.decode("utf-8"), mime_type, name
    except UnicodeDecodeError:
        return raw, mime_type, name


@server.read_resource()
async def handle_read_resource(uri: AnyUrl) -> list[ReadResourceContents]:
    logger.debug("handling resources/read: uri=%s", uri)
    file_id = _file_id_from_uri(str(uri))

    try:
        content, mime_type, _name = await _fetch_drive_file_content(file_id)
        return [ReadResourceContents(content=content, mime_type=mime_type)]
    except HttpError as exc:
        raise ValueError(f"Failed to read Drive file {file_id!r}: {exc}") from exc


# --- Tools: search (unchanged) + download_drive_file (new: progress + cancel) --


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
        ),
        types.Tool(
            name="download_drive_file",
            description=(
                "Download a Drive file's content in chunks, reporting real "
                "progress as it goes. Supply _meta.progressToken on the "
                "request to receive notifications/progress per chunk. The "
                "slowest real operation this server has -- built to "
                "demonstrate progress notifications and cancellation, not "
                "just search."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "file_id": {
                        "type": "string",
                        "description": "Drive file ID (as returned by search_drive_files).",
                    }
                },
                "required": ["file_id"],
            },
        ),
    ]


async def _download_drive_file(arguments: dict) -> list[types.TextContent]:
    file_id = arguments["file_id"]
    ctx = server.request_context
    progress_token = ctx.meta.progressToken if ctx.meta is not None else None

    async def report_progress(progress: float, total: float | None) -> None:
        if progress_token is not None:
            await ctx.session.send_progress_notification(progress_token, progress=progress, total=total)

    logger.info("download_drive_file: starting file_id=%s", file_id)
    content, mime_type, name = await _fetch_drive_file_content(file_id, report_progress=report_progress)

    size = len(content.encode("utf-8")) if isinstance(content, str) else len(content)
    logger.info("download_drive_file: complete, %r, %d bytes total", name, size)
    return [types.TextContent(type="text", text=f"Downloaded {name!r}: {size} bytes ({mime_type})")]


@server.call_tool()
async def handle_call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    logger.debug("handling tools/call: name=%s arguments=%s", name, arguments)

    if name == "download_drive_file":
        return await _download_drive_file(arguments)

    if name != "search_drive_files":
        raise ValueError(f"Unknown tool: {name}")

    query = arguments["query"]
    response = drive_service.files().list(q=query, pageSize=20, fields="files(id, name, mimeType)").execute()
    matches = [
        {"file_id": file["id"], "name": file["name"], "mimeType": file.get("mimeType")}
        for file in response.get("files", [])
    ]
    return [types.TextContent(type="text", text=json.dumps(matches, indent=2))]


# --- ASGI app assembly: transport, optionally wrapped in OAuth 2.1 ---------


class _StreamableHTTPEndpoint:
    """Thin ASGI-callable wrapper around the session manager.

    Deliberately a class instance, not a bare `async def` function:
    Starlette's Route treats a plain function endpoint as `func(request)
    -> response` and defaults its allowed methods to GET-only, which
    would silently 405 every POST (the actual JSON-RPC request path).
    A class instance with __call__ is instead treated as a raw ASGI app
    (scope, receive, send) with no method restriction -- the same
    distinction the SDK's own StreamableHTTPASGIApp relies on.
    """

    def __init__(self, session_manager: StreamableHTTPSessionManager) -> None:
        self.session_manager = session_manager

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await self.session_manager.handle_request(scope, receive, send)


def build_app(*, host: str, port: int, require_auth: bool) -> Starlette:
    session_manager = StreamableHTTPSessionManager(app=server, json_response=False, stateless=False)
    streamable_http_endpoint = _StreamableHTTPEndpoint(session_manager)

    routes: list[Route] = []
    middleware: list[Middleware] = []

    if require_auth:
        base_url = AnyHttpUrl(f"http://{host}:{port}")
        resource_url = AnyHttpUrl(f"{base_url}".rstrip("/") + MCP_PATH)
        signing_secret = secrets.token_urlsafe(48)
        logger.info(
            "OAuth enabled: issuer=%s resource=%s (in-memory signing key, regenerated each run)",
            base_url,
            resource_url,
        )

        # This server acts as its own resource owner (see oauth/provider.py's
        # module docstring for why) -- fetch the authenticated Drive
        # account's own address to use as the token's `sub` claim, rather
        # than a placeholder string.
        try:
            about = drive_service.about().get(fields="user").execute()
            resource_owner_subject = about["user"]["emailAddress"]
        except HttpError:
            resource_owner_subject = "resource-owner"

        provider = DriveOAuthProvider(
            issuer_url=str(base_url),
            resource_url=str(resource_url),
            signing_secret=signing_secret,
            consent_url=f"{str(base_url).rstrip('/')}/consent",
        )
        token_verifier = ProviderTokenVerifier(provider)

        middleware = [
            Middleware(AuthenticationMiddleware, backend=BearerAuthBackend(token_verifier)),
            Middleware(AuthContextMiddleware),
        ]

        routes.extend(
            create_auth_routes(
                provider=provider,
                issuer_url=base_url,
                client_registration_options=ClientRegistrationOptions(
                    enabled=True,
                    valid_scopes=[REQUIRED_SCOPE],
                    default_scopes=[REQUIRED_SCOPE],
                ),
            )
        )
        routes.extend(create_consent_routes(provider, resource_owner_subject))

        resource_metadata_url = build_resource_metadata_url(resource_url)
        routes.append(
            Route(
                MCP_PATH,
                endpoint=RequireAuthMiddleware(streamable_http_endpoint, [REQUIRED_SCOPE], resource_metadata_url),
            )
        )
        routes.extend(
            create_protected_resource_routes(
                resource_url=resource_url,
                authorization_servers=[base_url],
                scopes_supported=[REQUIRED_SCOPE],
                resource_name="Inbox Guardian Drive MCP server (Step 5)",
            )
        )
    else:
        logger.warning(
            "OAuth DISABLED (--no-auth) -- transport-only mode for isolating transport bugs from "
            "auth bugs. Not how this server ships; see README."
        )
        routes.append(Route(MCP_PATH, endpoint=streamable_http_endpoint))

    app = Starlette(routes=routes, middleware=middleware, lifespan=lambda _app: session_manager.run())
    return _WireLoggingMiddleware(app)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8770)
    parser.add_argument(
        "--no-auth",
        action="store_true",
        help="Serve the /mcp endpoint with no OAuth layer at all -- for isolating transport bugs "
        "from auth bugs during development only. Never how this server is meant to run.",
    )
    args = parser.parse_args()

    global drive_service
    creds = _load_credentials()
    drive_service = build("drive", "v3", credentials=creds)
    logger.info("authenticated with Drive; starting Streamable HTTP server on %s:%d", args.host, args.port)

    app = build_app(host=args.host, port=args.port, require_auth=not args.no_auth)
    config = uvicorn.Config(app, host=args.host, port=args.port, log_level="warning")
    uv_server = uvicorn.Server(config)
    try:
        await uv_server.serve()
    finally:
        logger.info("server stopped")
        _rpc_log.close()


if __name__ == "__main__":
    anyio.run(main)
